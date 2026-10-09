"""Terminal-first management service; the CEO model lives in the user's frontend."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import shlex
import subprocess
import threading
import time
import uuid
from pathlib import Path

import catalog
from directory_browser import DirectoryBrowser
from store import Store
from terminal_client import BrokerError, TerminalClient
from controllers import ControllerService, default_workspace_root

ROOT = Path(__file__).resolve().parent
VERSION = "0.5.0"
RETRYABLE = ("queued", "waiting_for_agent", "human_busy", "awaiting_connection", "blocked")
MANAGEMENT_EVENTS = ("host_configured", "host_connected", "host_unavailable", "agent_attached",
                     "agent_created", "agent_activated", "agent_deactivated", "message_submitted",
                     "terminal_input_submitted", "terminal_state", "terminal_output", "terminal_closed")


class Hub:
    def __init__(self, runtime=None, terminals=None, directories=None):
        self.runtime = Path(runtime or ROOT / ".runtime")
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.runtime / "hub.sqlite")
        filename = ROOT / "config.local.json"
        self.config = json.loads((filename if filename.exists() else ROOT / "config.example.json").read_text(encoding="utf-8"))
        self.hosts = self.store.setting("hosts") or self.config["hosts"]
        self.connections = {"local": {"connected": True, "history_available": True}}
        self.terminals = terminals if terminals is not None else TerminalClient()
        self.directories = directories if directories is not None else DirectoryBrowser()
        self.registry_lock = threading.RLock()
        self.event_condition = threading.Condition()
        self.stopping = False
        self._stop = threading.Event()
        # Real management workspaces live outside this checkout: Codex folder
        # trust must not implicitly grant access to the entire Hub repository.
        # Explicit runtimes (including tests) keep all state in that namespace.
        self.controllers = ControllerService(self, workspace_root=default_workspace_root(runtime))
        self._watcher = threading.Thread(target=self._watch_terminals, name="ceo-terminal-events", daemon=True)
        # Metadata reads never start the broker, SSH, a model, or a conversation.
        self._watcher.start()

    @staticmethod
    def agent_id(host, thread_id):
        return f"{host}::{thread_id}"

    def host(self, name):
        if name not in self.hosts:
            raise ValueError("未知机器；请先在 UI 配置机器。")
        return self.hosts[name]

    def record_event(self, kind, **data):
        self.store.event(kind, **data)
        with self.event_condition:
            self.event_condition.notify_all()

    def upsert_host(self, data):
        name = data.get("id", "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", name):
            raise ValueError("机器 ID 只能包含字母、数字、短横线和下划线。")
        transport, target = data.get("transport", "ssh"), data.get("target", "").strip()
        if transport not in ("local", "ssh") or name == "local" and transport != "local":
            raise ValueError("transport 必须为 local 或 ssh，本机入口不能改为 SSH。")
        if transport == "ssh" and (not target or target.startswith("-") or any(c.isspace() or ord(c) < 32 for c in target)):
            raise ValueError("请输入有效的 SSH alias 或 user@host。")
        config = {"label": data.get("label", name).strip() or name, "transport": transport, "target": target}
        with self.registry_lock:
            old = self.hosts.get(name)
            if old and (old.get("transport"), old.get("target", "")) != (transport, target) and any(a["host"] == name for a in self.store.agents()):
                raise ValueError("此机器已有工作终端；请以新 ID 添加新地址。")
            self.hosts[name] = config
            self.store.setting("hosts", self.hosts)
            self.connections.pop(name, None)
        self.record_event("host_configured", host=name)
        return {"id": name, **config}

    def history(self, host, request):
        config = self.host(host)
        if config["transport"] == "local":
            return catalog.main(request)
        target = config["target"]
        if target.startswith("-") or any(c.isspace() or ord(c) < 32 for c in target):
            raise ValueError("Invalid SSH target")
        payload = base64.urlsafe_b64encode(json.dumps(request, ensure_ascii=False).encode()).decode()
        # Send portable, read-only modules through stdin. No remote installation,
        # provider startup or authentication/configuration file reads are needed.
        modules = ("session_binding", "conversation_output") if request["op"] in ("bind_output", "resolve_output", "read_output") else ()
        source = "import sys,types\n"
        for name in modules:
            code = (ROOT / (name + ".py")).read_text(encoding="utf-8")
            source += f"_module=types.ModuleType({name!r});sys.modules[{name!r}]=_module;exec(compile({code!r},{name!r},'exec'),_module.__dict__)\n"
        source += (ROOT / "catalog.py").read_text(encoding="utf-8").replace("from __future__ import annotations\n", "", 1)
        reply = subprocess.run(["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", target, "python3 - " + payload],
            input=source, text=True, encoding="utf-8", errors="replace",
            capture_output=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if reply.returncode:
            raise RuntimeError(f"{host} SSH 失败：" + reply.stderr[-1500:])
        result = json.loads(reply.stdout)
        if not result["ok"]:
            raise RuntimeError(result["error"])
        return result["result"]

    def connect_host(self, host):
        self.host(host)
        try:
            result = self.history(host, {"op": "probe"})
            state = {"connected": True, "history_available": result.get("history_exists", False), **result}
        except Exception as error:
            state = {"connected": False, "error": str(error)}
        self.connections[host] = state
        self.record_event("host_connected" if state["connected"] else "host_unavailable", host=host, **state)
        return {"host": host, **state}

    def discover(self, host, query="", cwd="", limit=20):
        if self.controllers.is_controller_reference(host, query, cwd=cwd or None):
            raise PermissionError("管理会话不能作为工作 agent 登记。")
        result = self.history(host, {"op": "find", "query": query, "cwd": cwd, "limit": limit})
        result["threads"] = [row for row in result.get("threads", [])
                             if not self.controllers.is_controller_reference(host, row.get("id", ""), cwd=row.get("cwd"))]
        return {"host": host, **result}

    def list_directories(self, host, path=""):
        return {"host": host, **self.directories.list(self.host(host), path)}

    @staticmethod
    def _closed_status(row, runtime):
        mode = row.get("mode")
        if mode == "closed":
            return {"connected": False, "status": "closed", "state": "closed", "agent_ready": False,
                    "blocked_reason": None, "closed": True, "close_state": "closed"}
        if mode in ("closing", "close_unknown"):
            return {"connected": bool(runtime.get("connected")), "status": mode, "state": mode,
                    "agent_ready": False, "closed": False, "close_state": "unknown" if mode == "close_unknown" else "closing"}
        return {}

    @staticmethod
    def _require_open(row):
        if row.get("mode") in ("closed", "closing", "close_unknown"):
            raise ValueError("此终端已关闭或关闭结果尚未确认，不能再输入或重新连接。请新建终端；需要原对话时可在新终端运行 codex resume。")

    def get_agent(self, host, thread_id, plugin=False, sessions=None):
        if self.controllers.is_controller_reference(host, thread_id):
            raise PermissionError("管理会话不是工作 agent；请使用主控专用入口。")
        row = self.store.agent(host, thread_id)
        if not row:
            raise ValueError("未登记的工作终端；请先在 UI 打开或连接。")
        if self.controllers.is_controller_reference(host, thread_id, cwd=row.get("cwd")):
            raise PermissionError("管理工作区不能作为工作 agent 读取。")
        if plugin and not row["active"]:
            raise PermissionError("该终端未激活；请在 UI 纳入 CEO 管理范围。")
        safe = {key: value for key, value in row.items() if key not in ("access", "owned")}
        runtime = next((s for s in (self.terminals.sessions() if sessions is None else sessions) if s["id"] == row.get("terminal_id")), {})
        binding = self.store.setting(self._output_key(host, row.get("terminal_id"))) if row.get("terminal_id") else None
        return {**safe, "agent_id": self.agent_id(host, thread_id), "source": "terminal" if row.get("terminal_id") else "history",
                "output_binding": {"state": "recorded", "session_id": binding["session_id"], "method": binding.get("method"),
                                   "note": "上次关联；读取时重新核验当前进程。"} if binding else {"state": "unbound"},
                "source_thread_id": None if thread_id.startswith("ceohub-") else thread_id,
                "connected": bool(runtime.get("connected")), "status": runtime.get("state", "not_connected"),
                "state": runtime.get("state", "not_connected"), "command": runtime.get("command", ""),
                "agent_ready": bool(runtime.get("agent_ready")), "blocked_reason": runtime.get("blocked_reason"),
                **self._closed_status(row, runtime)}

    def split_agent_id(self, agent_id):
        parts = agent_id.split("::", 1)
        if len(parts) != 2 or not all(parts):
            raise ValueError("请使用 list_active_agents 返回的精确 agent_id。")
        self.host(parts[0])
        return parts

    def attached_agents(self, active_only=False):
        sessions = self.terminals.sessions()
        return [self.get_agent(a["host"], a["thread_id"], sessions=sessions) for a in self.store.agents() if not active_only or a["active"]]

    def connected_hosts(self, agents=None):
        agents = self.attached_agents() if agents is None else agents
        connected = {host for host, state in self.connections.items() if state.get("connected")}
        connected.update(a["host"] for a in agents if a["connected"] and a["state"] in ("shell", "agent"))
        return connected

    def _directory(self, config, cwd):
        if not isinstance(cwd, str) or not cwd or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in cwd):
            raise ValueError("请输入绝对工作目录，不接受终端控制字符。")
        absolute = bool(re.match(r"^[A-Za-z]:[\\/]", cwd)) or Path(cwd).is_absolute()
        if not (absolute if config["transport"] == "local" else cwd.startswith("/")):
            raise ValueError("工作目录必须是该机器上的绝对路径。")

    @staticmethod
    def _request_key(value):
        if value is not None and (not isinstance(value, str) or not value or len(value) > 200):
            raise ValueError("request_key 必须为 1 至 200 字符的稳定请求键。")

    @staticmethod
    def _child_key(request_key, suffix):
        combined = request_key + suffix
        return combined if len(combined) <= 200 else hashlib.sha256(request_key.encode()).hexdigest() + suffix

    def create_terminal(self, host, cwd, title, thread_id=None, cols=100, rows=30, active=False, request_key=None, controller_id=None):
        config = self.host(host)
        if self.controllers.is_controller_reference(host, thread_id, cwd=cwd or None):
            raise PermissionError("管理会话不能作为工作终端创建或连接。")
        self._request_key(request_key)
        if not isinstance(cwd, str):
            raise ValueError("工作目录必须为路径字符串；留空可使用机器主目录。")
        # Resolve only the chosen machine's home, outside the registry lock.
        # Existing connections keep their recorded cwd without another SSH call.
        previous_hint = self.store.agent(host, thread_id) if thread_id else None
        if previous_hint:
            self._require_open(previous_hint)
        if thread_id and not thread_id.startswith("ceohub-") and (previous_hint is None or not cwd):
            if not previous_hint or not previous_hint.get("cwd"):
                metadata = self.history(host, {"op": "read", "thread_id": thread_id, "limit": 1})
                cwd, title = cwd or metadata["cwd"], title or metadata["title"]
        if not cwd:
            cwd = (previous_hint or {}).get("cwd") or self.directories.home(config)
        with self.registry_lock:
            previous = self.store.agent(host, thread_id) if thread_id else None
            if previous:
                self._require_open(previous)
            if previous and previous.get("terminal_id") and previous.get("cwd"):
                if cwd and cwd != previous["cwd"]:
                    raise ValueError("已有终端仍在原工作目录；请新建终端使用其他目录。")
                cwd = previous["cwd"]
            self._directory(config, cwd)
            if self.controllers.is_controller_reference(host, thread_id, cwd=cwd):
                raise PermissionError("管理工作区不能登记为工作 agent。")
            if controller_id is not None:
                self._require_creation_scope(controller_id, host, cwd)
            if not isinstance(title, str) or not title.strip() or len(title.strip()) > 200 or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in title):
                raise ValueError("工作名称不能为空，最长 200 字符。")
            title = title.strip()
            if type(cols) is not int or not 20 <= cols <= 500 or type(rows) is not int or not 5 <= rows <= 200:
                raise ValueError("终端尺寸必须为 20 至 500 列、5 至 200 行。")
            operation_key = "terminal:" + request_key if request_key else None
            if operation_key:
                fingerprint = hashlib.sha256(json.dumps([host, cwd, title, thread_id, cols, rows, bool(active)], ensure_ascii=False).encode()).hexdigest()
                cached = self.store.begin_operation(operation_key, fingerprint)
                if cached is not None:
                    return cached
            terminal_id = previous.get("terminal_id") if previous else None
            terminal_id = terminal_id or "ceohub-" + (uuid.uuid5(uuid.NAMESPACE_URL, "ceo-hub:" + request_key).hex if request_key else uuid.uuid4().hex)
            thread_id = thread_id or terminal_id
            # Register first, so an interrupted create remains visible and can
            # reconnect the same owned terminal rather than silently duplicating.
            self.store.remember(host, thread_id, title or thread_id, active=active if previous is None else None,
                                mode="terminal", terminal_id=terminal_id, cwd=cwd)
            try:
                if controller_id is not None:
                    self._require_creation_scope(controller_id, host, cwd)
                runtime = self.terminals.create({"session_id": terminal_id, "host": host, "transport": config["transport"],
                    "target": config.get("target", ""), "cwd": cwd, "title": title, "cols": cols, "rows": rows})
                result = {"created": True, **self.get_agent(host, thread_id, sessions=[runtime]), **runtime,
                          "agent_id": self.agent_id(host, thread_id), "terminal_id": terminal_id, "thread_id": thread_id}
            except Exception as error:
                result = {"created": False, **self.get_agent(host, thread_id, sessions=[]), "delivery": {"state": "unknown", "error": str(error)}}
            if not thread_id.startswith("ceohub-"):
                quoted = "'" + thread_id.replace("'", "''") + "'" if config["transport"] == "local" else shlex.quote(thread_id)
                result["resume_command"] = "codex resume " + quoted
            if operation_key:
                self.store.finish_operation(operation_key, result)
            if result["created"]:
                self.record_event("agent_attached", host=host, thread_id=thread_id, terminal_id=terminal_id)
            return result

    def connect_terminal(self, host, thread_id, cwd="", title="", cols=100, rows=30):
        previous = self.store.agent(host, thread_id)
        cwd, title = cwd or (previous or {}).get("cwd", ""), title or (previous or {}).get("alias", "")
        return self.create_terminal(host, cwd, title, thread_id, cols, rows)

    def attach_agent(self, host, thread_id, alias=""):
        return self.connect_terminal(host, thread_id, title=alias)

    def activate_agent(self, host, thread_id, active):
        with self.registry_lock:
            row = self.get_agent(host, thread_id)
            if active:
                self._require_open(row)
            self.store.set_active(host, thread_id, active)
            if not active:
                self.store.revoke_pending(host, thread_id, RETRYABLE)
        self.record_event("agent_activated" if active else "agent_deactivated", host=host, thread_id=thread_id)
        return self.get_agent(host, thread_id)

    @staticmethod
    def _output_key(host, terminal_id):
        return "output_binding:" + host + ":" + terminal_id

    def _probe_output(self, host, agent, view):
        request = {"op": "bind_output"}
        if self.host(host)["transport"] == "ssh":
            request["terminal_id"] = agent["terminal_id"]
        else:
            if not view.get("connected"):
                return {"state": "unbound", "reason": "terminal_disconnected"}
            request["root_pid"] = view.get("pid")
        return self.history(host, request)

    def bind_output(self, host, thread_id, session_id):
        """Human-selected exact session, bound to this terminal's live process.

        This is a fallback for a Codex resume picker that exposes no session ID
        in argv or open handles. It never sends /status or other worker input.
        """
        if not isinstance(session_id, str) or not re.fullmatch(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}", session_id):
            raise ValueError("请输入当前 Codex /status 显示的完整 Session ID。")
        session_id = session_id.lower()
        if self.controllers.is_controller_reference(host, session_id):
            raise PermissionError("不能将管理会话输出关联为工作 agent。")
        agent = self.get_agent(host, thread_id)
        self._require_open(agent)
        if not agent.get("terminal_id"):
            raise ValueError("请先选择工作终端。")
        view = self.terminals.snapshot(agent["terminal_id"], 1)
        probe = self._probe_output(host, agent, view)
        if not probe.get("process_identity") or not probe.get("codex_home"):
            raise ValueError("无法核验这个终端中的 Codex 进程，未建立关联。")
        if probe.get("state") == "unbound" and probe.get("reason") != "no_exact_session_evidence":
            raise ValueError("会话关联证据冲突或失效，未建立关联：" + probe.get("reason", "unknown"))
        if probe.get("state") == "bound" and probe.get("binding_evidence") == "held_rollout" and probe["session_id"] != session_id:
            raise ValueError("输入 ID 与当前 Codex 进程的会话证据不同，未建立关联。")
        resolved = self.history(host, {"op": "resolve_output", "session_id": session_id, "codex_home": probe["codex_home"]})
        if resolved.get("state") not in ("bound", "resolved"):
            raise ValueError(resolved.get("reason", "未找到这个精确会话的输出记录。"))
        verify = self._probe_output(host, agent, view)
        if verify.get("process_identity") != probe["process_identity"]:
            raise ValueError("关联期间 Codex 进程已变化，请重新确认当前 Session ID。")
        if verify.get("state") == "bound" and verify.get("binding_evidence") == "held_rollout" and verify.get("session_id") != session_id:
            raise ValueError("关联期间 Codex 会话已变化，请重新确认 Session ID。")
        binding = {**resolved, "process_identity": probe["process_identity"], "method": "human_selected_session",
                   "terminal_id": agent["terminal_id"]}
        self.store.setting(self._output_key(host, agent["terminal_id"]), binding)
        return {"agent_id": agent["agent_id"], "output_binding": {"state": "bound", "session_id": session_id,
                 "method": binding["method"]}, "note": "已关联所选会话记录；没有重启或输入到工作 agent。"}

    def _conversation_output(self, host, agent, view, limit, cursor=None, after_cursor=None, include_tools=False):
        key = self._output_key(host, agent["terminal_id"])
        saved = self.store.setting(key)
        try:
            probe = self._probe_output(host, agent, view)
            if probe.get("state") == "bound" and probe.get("binding_evidence") == "held_rollout":
                # argv's initial resume selector survives in-TUI /new /resume.
                # Only a held current rollout is direct live-session evidence.
                binding = {**probe, "terminal_id": agent["terminal_id"],
                           "live_session_verified": probe.get("binding_evidence") == "held_rollout"}
            elif saved and (probe.get("reason") == "no_exact_session_evidence" or probe.get("binding_evidence") == "explicit_resume") and probe.get("process_identity") and probe["process_identity"] == saved.get("process_identity"):
                binding = {**saved, "live_session_verified": False}
            elif probe.get("state") == "bound":
                binding = {**probe, "terminal_id": agent["terminal_id"], "live_session_verified": False}
            elif saved and (agent.get("mode") in ("closed", "closing", "close_unknown") or not view.get("connected")
                            or probe.get("reason") == "foreground_shell"):
                # Detached/closed terminals may still read their last VERIFIED
                # association, but this is explicitly historical, not live state.
                binding = {**saved, "historical": True, "live_session_verified": False}
            else:
                return {"output_binding": {"state": "unbound", "reason": probe.get("reason", "session_identity_unavailable")},
                        "messages": [], "content_complete": False,
                        "note": "尚未准确关联 Codex 会话输出。请用当前终端 /status 的 Session ID 在 UI「关联会话」。屏幕不是完整回复。"}
            if self.controllers.is_controller_reference(host, binding["session_id"]):
                raise PermissionError("管理会话输出不能作为工作 agent 读取。")
            self.store.setting(key, binding)
            output = self.history(host, {"op": "read_output", "session_id": binding["session_id"],
                "rollout_path": binding["rollout_path"], "codex_home": binding["codex_home"], "limit": limit,
                "include_tools": include_tools,
                **({"cursor": cursor} if cursor else {}), **({"after_cursor": after_cursor} if after_cursor else {})})
            # A new process or a deactivated scope during I/O must not return a
            # different session under an earlier authorization/cursor.
            if not binding.get("historical"):
                verify = self._probe_output(host, agent, view)
                if verify.get("process_identity") != binding.get("process_identity"):
                    raise ValueError("读取期间 Codex 进程已变化，未返回旧会话输出；请重新读取。")
                if verify.get("state") == "bound" and verify.get("binding_evidence") == "held_rollout":
                    if verify.get("session_id") != binding["session_id"]:
                        raise ValueError("读取期间 Codex 会话已切换，未返回旧会话输出；请重新读取。")
                    binding["live_session_verified"] = True
                elif verify.get("state") == "unbound" and verify.get("reason") != "no_exact_session_evidence":
                    raise ValueError("读取期间会话关联证据冲突或失效，未返回记录；请重新核验。")
                else:
                    binding["live_session_verified"] = False
            pinned_note = " 当前进程未暴露确切会话 ID，这是用户已选或上次核验的会话记录；如果在 Codex 内使用 /new 或 /resume 切换过会话，请重新关联，不能把旧记录当新会话结果。" if not binding.get("live_session_verified") else ""
            return {**output, "note": output.get("note", "") + pinned_note,
                    "output_binding": {"state": "bound", "session_id": binding["session_id"],
                    "method": binding.get("method", "process_evidence" if binding.get("live_session_verified") else "last_verified_session"),
                    "historical": bool(binding.get("historical")), "live_session_verified": bool(binding.get("live_session_verified"))}}
        except Exception as error:
            if cursor or after_cursor:
                raise ValueError("会话输出分页无法继续，请重新读取最新记录：" + str(error)) from None
            return {"output_binding": {"state": "unavailable", "reason": str(error)}, "messages": [],
                    "content_complete": False, "note": "真实会话输出当前不可用；以下屏幕仅作状态参考，不是完整回复。"}

    def read_agent(self, host, thread_id, limit=18, plugin=False, cursor=None, after_cursor=None, include_tools=False):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit 必须为 1 至 100 的消息数。")
        if cursor and after_cursor:
            raise ValueError("cursor 和 after_cursor 不能同时使用。")
        if type(include_tools) is not bool:
            raise ValueError("include_tools 必须为布尔值。")
        agent = self.get_agent(host, thread_id, plugin=plugin)
        if not agent.get("terminal_id"):
            return {**agent, **self.history(host, {"op": "read", "thread_id": thread_id, "limit": limit}), "source": "persisted_history"}
        try:
            view = self.terminals.snapshot(agent["terminal_id"], min(500, max(1, int(limit)) * 8))
        except Exception:
            if agent.get("mode") not in ("closed", "closing", "close_unknown"):
                raise
            view = {"screen": "", "ansi": "", "connected": False, "output_unavailable": True}
        output = self._conversation_output(host, agent, view, limit, cursor, after_cursor, include_tools)
        # Keep the screen for foreground/input diagnosis. Semantic messages come
        # from the selected Codex rollout, never reconstructed from ANSI frames.
        result = {**agent, **view, "agent_id": agent["agent_id"], "source": "terminal",
                  "history_complete": False, **output, "conversation_status": output.get("status"),
                  "status": view.get("state", agent.get("state", "unknown")), **self._closed_status(agent, view)}
        self.get_agent(host, thread_id, plugin=plugin)  # recheck scope after I/O
        return result

    @staticmethod
    def _delivery(row):
        return {**row, "request_id": row["id"], "delivery": row["state"]}

    def _submit(self, host, thread_id, data, kind, origin, request_key, controller_id=None):
        self._request_key(request_key)
        if origin not in ("ui", "plugin"):
            raise ValueError("输入来源必须为 ui 或 plugin。")
        with self.registry_lock:
            agent = self.get_agent(host, thread_id, plugin=origin == "plugin")
            if controller_id is not None:
                self._require_controller_scope(controller_id, host, thread_id)
            self._require_open(agent)
            request_id = self.store.enqueue(host, thread_id, data, origin=origin, request_key=request_key, kind=kind)
            row = self.store.delivery(request_id)
            if row["state"] not in RETRYABLE:
                return self._delivery(row)
            if not agent.get("terminal_id"):
                self.store.update_outbox(request_id, "awaiting_connection", error="请先在 UI 连接共享终端。")
                return self._delivery(self.store.delivery(request_id))
            try:
                # Last check under the same lock used by scope revocation; an
                # earlier queued/human_busy delivery is never a future grant.
                self.get_agent(host, thread_id, plugin=origin == "plugin")
                if controller_id is not None:
                    self._require_controller_scope(controller_id, host, thread_id)
                self.store.update_outbox(request_id, "submitting")
                body = {"text" if kind == "prompt" else "data": data, "origin": origin, "request_key": request_key or request_id}
                reply = self.terminals.write(agent["terminal_id"], "prompt" if kind == "prompt" else "input", body)
                state = reply.get("state", "submitted" if reply.get("accepted") else "unknown")
                self.store.update_outbox(request_id, state)
                if state == "submitted":
                    self.record_event("message_submitted" if kind == "prompt" else "terminal_input_submitted",
                                      host=host, thread_id=thread_id, request_id=request_id, origin=origin)
            except PermissionError:
                self.store.update_outbox(request_id, "scope_revoked", error="输入提交前授权已失效；未提交输入不会执行。")
            except BrokerError as error:
                state = {"human_busy": "human_busy", "waiting_for_agent": "waiting_for_agent", "agent_not_started": "waiting_for_agent",
                         "agent_not_confirmed": "waiting_for_agent", "terminal_exited": "awaiting_connection", "terminal_not_found": "awaiting_connection",
                         # This broker fence runs before PTY.write: delivery is
                         # definitely blocked, not uncertain, and the SAME key
                         # may retry after a verified geometry repair.
                         "geometry_unknown": "blocked",
                         "invalid_prompt": "not_sent", "invalid_input": "not_sent", "request_conflict": "not_sent"}.get(error.code, "unknown")
                self.store.update_outbox(request_id, state, error=str(error))
            except Exception as error:
                self.store.update_outbox(request_id, "unknown", error=str(error))
            return self._delivery(self.store.delivery(request_id))

    def send(self, host, thread_id, text, origin="ui", request_key=None, controller_id=None):
        if not isinstance(text, str) or not text.strip() or len(text) > 30000 or any((ord(c) < 32 and c not in "\r\n\t") or 127 <= ord(c) <= 159 for c in text):
            raise ValueError("消息不能为空，最长 30000 字符，不接受终端控制字符。")
        return self._submit(host, thread_id, text, "prompt", origin, request_key, controller_id=controller_id)

    def terminal_input(self, host, thread_id, data, origin="ui", request_key=None, controller_id=None):
        if not isinstance(data, str) or not data or len(data) > 30000:
            raise ValueError("终端输入不能为空，最长 30000 字符。")
        if origin == "plugin":
            if not request_key:
                raise ValueError("插件输入必须提供稳定 request_key。")
            return self._submit(host, thread_id, data, "input", origin, request_key, controller_id=controller_id)
        # User keystrokes can contain login secrets: forward without persisting.
        if origin != "ui":
            raise ValueError("输入来源必须为 ui 或 plugin。")
        # No registry lock or slow process probe here: user keystrokes must reach
        # the broker during a CEO readiness probe and acquire its typing lease.
        agent = self.store.agent(host, thread_id)
        if not agent or not agent.get("terminal_id"):
            raise ValueError("请先连接共享终端。")
        self._require_open(agent)
        return self.terminals.write(agent["terminal_id"], "input", {"data": data, "origin": "ui"})

    def terminal_output(self, host, thread_id, after=0):
        agent = self.get_agent(host, thread_id)
        try:
            output = self.terminals.output(agent["terminal_id"], after)
        except Exception:
            if agent.get("mode") not in ("closed", "closing", "close_unknown"):
                raise
            return {"data": "", "seq": max(0, int(after)), "reset": False, "output_unavailable": True,
                    **self._closed_status(agent, {}), "note": "终端缓存暂时不可读取；登记记录仍保留，不会重新启动或连接。"}
        return {**output, **self._closed_status(agent, output)}

    def terminal_resize(self, host, thread_id, cols=100, rows=30):
        agent = self.get_agent(host, thread_id)
        self._require_open(agent)
        return self.terminals.write(agent["terminal_id"], "resize", {"cols": cols, "rows": rows})

    def terminal_close(self, host, thread_id, confirm=False):
        if confirm is not True:
            raise ValueError("关闭会终止这个终端及其中的进程，请明确确认关闭。")
        with self.registry_lock:
            agent = self.get_agent(host, thread_id)
            terminal_id = agent.get("terminal_id")
            if not terminal_id or not re.fullmatch(r"ceohub-[a-f0-9]{16,48}", terminal_id):
                raise ValueError("此记录没有可关闭的共享终端。")
            operation = "terminal-close:" + terminal_id
            fingerprint = hashlib.sha256(json.dumps([host, thread_id, terminal_id]).encode()).hexdigest()
            try:
                cached = self.store.begin_operation(operation, fingerprint)
            except RuntimeError:
                cached = {"closed": False, "close_state": "unknown", "state": "close_unknown", "status": "close_unknown",
                          "host": host, "thread_id": thread_id, "terminal_id": terminal_id,
                          "error": "原关闭请求结果不确定；为避免误关后续进程，不会自动重试。"}
                self.store.remember(host, thread_id, agent["alias"], active=False, mode="close_unknown")
                self.store.finish_operation(operation, cached)
            if cached is not None:
                return cached
            # Persist the closing fence before talking to the broker. Stale UI
            # keyboard/resize requests cannot operate or reconnect this ID.
            self.store.remember(host, thread_id, agent["alias"], active=False, mode="closing")
            self.store.revoke_pending(host, thread_id, RETRYABLE)
            try:
                reply = self.terminals.write(terminal_id, "close", {})
                if reply.get("closed") is not True or reply.get("id") != terminal_id:
                    raise RuntimeError("终端服务未确认关闭精确目标。")
                self.store.remember(host, thread_id, agent["alias"], active=False, mode="closed")
                result = {"closed": True, "close_state": "closed", "state": "closed", "status": "closed", "connected": False,
                          "active": False, "host": host, "thread_id": thread_id, "terminal_id": terminal_id, "id": terminal_id}
                self.record_event("terminal_closed", host=host, thread_id=thread_id, terminal_id=terminal_id, state="closed")
            except Exception as error:
                self.store.remember(host, thread_id, agent["alias"], active=False, mode="close_unknown")
                result = {"closed": False, "close_state": "unknown", "state": "close_unknown", "status": "close_unknown",
                          "active": False, "host": host, "thread_id": thread_id, "terminal_id": terminal_id, "error": str(error)}
                self.record_event("terminal_state", host=host, thread_id=thread_id, terminal_id=terminal_id, state="close_unknown", error=str(error))
            self.store.finish_operation(operation, result)
        return result

    def create_agent(self, host, cwd, title, text="", active=True, request_key=None, origin="ui", command="", controller_id=None):
        config = self.host(host)
        if not isinstance(text, str) or len(text) > 30000 or any((ord(c) < 32 and c not in "\r\n\t") or 127 <= ord(c) <= 159 for c in text):
            raise ValueError("任务文本最长 30000 字符，不接受终端控制字符。")
        if origin not in ("ui", "plugin"):
            raise ValueError("输入来源必须为 ui 或 plugin。")
        if origin == "plugin" and host not in self.connected_hosts():
            raise PermissionError("该机器尚未在 UI 连接。")
        self._directory(config, cwd)
        if not isinstance(command, str) or len(command) > 30000 or "\0" in command:
            raise ValueError("启动指令最长 30000 字符，不能含空字符。")
        if controller_id is not None:
            if command:
                raise PermissionError("管理会话创建只打开 shell；启动命令需用户在终端自行执行。")
            self._require_creation_scope(controller_id, host, cwd)
            # Canonicalize on the selected host before creating, then check the
            # resolved path as well. A symlink/.. cannot escape granted roots.
            try:
                cwd = self.directories.list(config, cwd)["path"]
            except Exception:
                raise PermissionError("无法核验所选授权目录，未创建终端。") from None
            self._require_creation_scope(controller_id, host, cwd)
        request_key = request_key or "ui-create-" + str(uuid.uuid4())
        self._request_key(request_key)
        fingerprint = hashlib.sha256(json.dumps([host, cwd, title, text, bool(active), command, origin], ensure_ascii=False).encode()).hexdigest()
        with self.registry_lock:
            if controller_id is not None:
                self._require_creation_scope(controller_id, host, cwd)
            previous = self.store.begin_operation(request_key, fingerprint)
            if previous is None:
                try:
                    result = {"created": True, **self.create_terminal(host, cwd, title, active=active, request_key=request_key, controller_id=controller_id)}
                    if result["created"]:
                        if controller_id is not None:
                            self.controllers.add_created_agent(controller_id, host, result["thread_id"])
                        self.record_event("agent_created", host=host, thread_id=result["thread_id"], origin=origin)
                    else:
                        self.store.finish_operation(request_key, result)
                        return result
                except Exception as error:
                    result = {"created": False, "delivery": {"state": "unknown", "error": str(error)}, "request_key": request_key}
                    self.store.finish_operation(request_key, result)
                    return result
            else:
                result = previous
                if not result.get("created"):
                    return result
                self.get_agent(host, result["thread_id"], plugin=origin == "plugin")
                if controller_id is not None:
                    self._require_controller_scope(controller_id, host, result["thread_id"])
            try:
                if command:
                    result["startup_delivery"] = self._submit(host, result["thread_id"], command.rstrip("\r\n") + "\r", "input", origin, self._child_key(request_key, ":startup"), controller_id=controller_id)
                if text.strip() and (not command or result["startup_delivery"]["state"] == "submitted"):
                    result["delivery"] = self.send(host, result["thread_id"], text, origin=origin, request_key=self._child_key(request_key, ":message"), controller_id=controller_id)
                elif command:
                    result["delivery"] = result["startup_delivery"]
            except Exception as error:
                result["delivery"] = {"state": "unknown", "error": str(error)}
            self.store.finish_operation(request_key, result)
            return result

    def _require_controller_scope(self, controller_id, host, thread_id):
        row = self.store.agent(host, thread_id)
        if not row or not row["active"] or not self.controllers.authorize(controller_id, host, thread_id):
            raise PermissionError("此工作终端不在该管理会话的当前授权范围内。")
        if self.controllers.is_controller_reference(host, thread_id, cwd=row.get("cwd")):
            raise PermissionError("管理会话不能管理自身或其他管理会话。")

    def _require_creation_scope(self, controller_id, host, cwd):
        if not self.controllers.authorize_creation(controller_id, host, cwd):
            raise PermissionError("该管理会话未获得此机器和项目目录的新建授权。")
        if self.controllers.is_controller_reference(host, None, cwd=cwd):
            raise PermissionError("管理工作区不能作为工作 agent 新建。")

    @staticmethod
    def _controller_prefix(controller_id):
        return "controller:" + controller_id + ":"

    def _controller_key(self, controller_id, request_key):
        self._request_key(request_key)
        if not request_key:
            raise ValueError("管理操作必须提供稳定 request_key。")
        return self._controller_prefix(controller_id) + hashlib.sha256(request_key.encode("utf-8")).hexdigest()

    def scope_revoking(self, controller_id, removed_agent_ids):
        """Called under registry_lock; revoke only this controller's pending work."""
        prefix = self._controller_prefix(controller_id)
        with self.registry_lock, self.store.lock, self.store.db:
            for agent_id in removed_agent_ids:
                host, thread_id = self.split_agent_id(agent_id)
                placeholders = ",".join("?" for _ in RETRYABLE)
                self.store.db.execute("update outbox set state='scope_revoked',error=? where host=? and thread_id=? "
                    "and origin='plugin' and substr(request_key,1,?)=? and state in (" + placeholders + ")",
                    ("已撤销此管理会话的授权；未提交输入不会执行。", host, thread_id, len(prefix), prefix, *RETRYABLE))

    def plugin_events(self, after=0, timeout_seconds=0, controller_id=None):
        deadline, cursor = time.monotonic() + min(30, max(0, float(timeout_seconds))), max(0, int(after))
        while True:
            selected = []
            for event in self.store.events(cursor, kinds=MANAGEMENT_EVENTS):
                cursor = event["id"]
                data = event["data"]
                agent = self.store.agent(data.get("host"), data.get("thread_id"))
                if agent and agent["active"] and (controller_id is None or self.controllers.authorize(controller_id, agent["host"], agent["thread_id"])):
                    selected.append(event)
            if selected or time.monotonic() >= deadline:
                return {"events": selected, "cursor": cursor}
            with self.event_condition:
                self.event_condition.wait(min(1, max(0, deadline - time.monotonic())))

    def plugin_call(self, tool, arguments, controller_id=None):
        if not isinstance(arguments, dict):
            raise ValueError("工具参数必须为对象。")
        if controller_id is not None:
            # Authoritative identity comes from HTTP authentication, never from
            # arguments or a descriptor ID repeated by the model.
            context = self.controllers.view_context(controller_id)
            if tool != "get_controller_context":
                controller = context.get("controller", {})
                if (controller.get("state") in ("prepared", "bootstrapping", "initializing", "attaching", "unknown", "closed")
                        or not controller.get("session_id") or not controller.get("tools_ready")):
                    raise PermissionError("管理初始化仅允许核验自身工具；会话身份与工具就绪后才能访问工作 agent。")
        if tool == "get_controller_context":
            if controller_id is None:
                return {"mode": "legacy", "controller_id": None, "legacy_scope_shared": True,
                        "scope_note": "旧插件使用全局 UI 激活范围，不是独立管理会话身份。"}
            self.controllers.mark_tool_seen(controller_id)
            return self.controllers.view_context(controller_id)
        if tool == "list_active_agents":
            if controller_id is None:
                agents = self.attached_agents()
            else:
                with self.registry_lock:
                    sessions = self.terminals.sessions()
                    agents = []
                    for agent_id in self.controllers.allowed_agents(controller_id):
                        host, thread_id = self.split_agent_id(agent_id)
                        row = self.store.agent(host, thread_id)
                        if row and row["active"] and self.controllers.authorize(controller_id, host, thread_id):
                            agents.append(self.get_agent(host, thread_id, plugin=True, sessions=sessions))
            connected = self.connected_hosts(agents)
            if controller_id is not None:
                connected &= set(self.controllers.allowed_hosts(controller_id)) | {a["host"] for a in agents}
            return {"agents": [a for a in agents if a["active"]], "controller": "current_codex_conversation",
                    "connected_hosts": sorted(connected), "legacy_scope_shared": controller_id is None,
                    "scope_note": "只列本管理会话已获授权且 UI 激活的终端。" if controller_id is not None else "只列 UI 已激活共享终端，无全盘扫描或独立 CEO 模型。"}
        if tool == "poll_events":
            return self.plugin_events(arguments.get("after", 0), arguments.get("timeout_seconds", 0), controller_id=controller_id)
        if tool == "create_agent":
            if not arguments.get("request_key"):
                raise ValueError("创建必须提供稳定 request_key。")
            return self.create_agent(arguments["host"], arguments["cwd"], arguments["title"], arguments.get("text", ""),
                active=True, request_key=self._controller_key(controller_id, arguments["request_key"]) if controller_id is not None else arguments["request_key"],
                origin="plugin", command=arguments.get("command", ""), controller_id=controller_id)
        if tool == "get_delivery":
            row = self.store.delivery(arguments["request_id"])
            if not row:
                raise ValueError("没有此送达记录。")
            self.get_agent(row["host"], row["thread_id"], plugin=True)
            if controller_id is not None:
                self._require_controller_scope(controller_id, row["host"], row["thread_id"])
                if row["origin"] != "plugin" or not str(row.get("request_key") or "").startswith(self._controller_prefix(controller_id)):
                    raise PermissionError("此送达记录不属于当前管理会话。")
            return self._delivery(row)
        host, thread_id = self.split_agent_id(arguments["agent_id"])
        if controller_id is not None:
            self._require_controller_scope(controller_id, host, thread_id)
        if tool == "get_agent":
            result = self.get_agent(host, thread_id, plugin=True)
            if controller_id is not None:
                self._require_controller_scope(controller_id, host, thread_id)
            return result
        if tool == "read_agent":
            result = self.read_agent(host, thread_id, arguments.get("limit", 18), plugin=True,
                                   cursor=arguments.get("cursor"), after_cursor=arguments.get("after_cursor"),
                                   include_tools=arguments.get("include_tools", False))
            if controller_id is not None:
                self._require_controller_scope(controller_id, host, thread_id)
            return result
        if tool in ("send_agent_message", "send_terminal_input"):
            if not arguments.get("request_key"):
                raise ValueError("发送必须提供稳定 request_key。")
            if tool == "send_agent_message":
                return self.send(host, thread_id, arguments["text"], origin="plugin", controller_id=controller_id,
                    request_key=self._controller_key(controller_id, arguments["request_key"]) if controller_id is not None else arguments["request_key"])
            return self.terminal_input(host, thread_id, arguments["data"], origin="plugin", controller_id=controller_id,
                request_key=self._controller_key(controller_id, arguments["request_key"]) if controller_id is not None else arguments["request_key"])
        raise ValueError("Unsupported plugin tool")

    def _watch_terminals(self):
        seen = {}
        while not self._stop.wait(1.5):
            try:
                with self.registry_lock:
                    if self.stopping:
                        return
                    sessions = {s["id"]: s for s in self.terminals.sessions()}
                    for row in self.store.agents():
                        view = sessions.get(row.get("terminal_id"))
                        key = (row["host"], row["thread_id"])
                        if not view and key not in seen:
                            continue
                        status = self._closed_status(row, view or {})
                        now = (status.get("connected", view.get("connected")), status.get("state", view.get("state")), view.get("command"), view.get("current_seq")) if view else (False, status.get("state", "not_connected"), "", seen[key][3])
                        old = seen.get(key)
                        if now != old:
                            self.record_event("terminal_output" if old and now[:3] == old[:3] else "terminal_state", host=key[0], thread_id=key[1],
                                terminal_id=row["terminal_id"], connected=now[0], state=now[1], command=now[2], current_seq=now[3])
                        seen[key] = now
            except Exception:
                continue  # Broker absence is normal; never launch or reconnect it.

    def state(self, after=0):
        agents = self.attached_agents()
        connected = self.connected_hosts(agents)
        return {"version": VERSION, "controller": "codex-plugin", "legacy_scope_shared": True, "hosts": [{"id": h, **c, **self.connections.get(h, {}), "connected": h in connected} for h, c in self.hosts.items()],
                "agents": agents, "events": self.store.events(after, kinds=MANAGEMENT_EVENTS), "outbox": self.store.outbox(), "approvals": []}

    def close(self):
        if self.stopping:
            return
        self.stopping = True
        self._stop.set()
        self._watcher.join(timeout=5)
        self.controllers.shutdown()
        with self.registry_lock:
            self.store.close()
        # Deliberately leave independent broker/PTY/tmux processes running.
