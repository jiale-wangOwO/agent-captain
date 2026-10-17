"""Agent connection service. The CEO model lives in the user's Codex frontend."""
from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import json
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path

from agent_captain.sessions import catalog
from agent_captain.legacy.rpc import Rpc, server_command
from agent_captain.infrastructure.store import Store

from agent_captain.paths import ROOT, source_path
VERSION = "0.2.0"
MANAGEMENT_EVENTS = ("host_configured", "host_connected", "host_unavailable", "agent_attached", "agent_activated", "agent_deactivated",
                     "agent_created", "message_submitted", "worker_completed", "approval_needed", "approval_resolved")


class LegacyAppServerHub:
    def __init__(self, runtime=None):
        self.runtime = Path(runtime or ROOT / ".runtime")
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.runtime / "hub.sqlite")
        path = ROOT / "config.local.json"
        self.config = json.loads((path if path.exists() else ROOT / "config.example.json").read_text(encoding="utf-8"))
        self.hosts = self.store.setting("hosts") or self.config["hosts"]
        self.connections = {"local": {"connected": True, "history_available": True}}
        self.servers, self.live_servers, self.owners, self.active = {}, {}, {}, {}
        self.loaded = set()
        self.runtime_versions = {}
        self.server_lock = threading.RLock()
        self.registry_lock = threading.RLock()
        self.event_condition = threading.Condition()
        self.pending_approvals = {}
        self.stopping = False
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)
        # No discovery, model process, automatic resume, or work on startup.

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
        transport = data.get("transport", "ssh")
        if transport not in ("local", "ssh"):
            raise ValueError("transport 必须是 local 或 ssh。")
        if name == "local" and transport != "local":
            raise ValueError("本机入口不能改为 SSH。")
        target = data.get("target", "").strip()
        if transport == "ssh" and (not target or target.startswith("-") or any(c.isspace() for c in target)):
            raise ValueError("请输入有效的 SSH alias 或 user@host。")
        config = {"label": data.get("label", name).strip() or name, "transport": transport,
                  "target": target, "live": bool(data.get("live", transport == "ssh"))}
        with self.registry_lock:
            previous = self.hosts.get(name)
            route_changed = previous and (previous.get("transport") != config["transport"] or previous.get("target", "") != config["target"])
            if route_changed and any(a["host"] == name for a in self.store.agents()):
                raise ValueError("此机器已有登记对话；请以新 ID 添加新地址，避免把旧对话路由到另一台机器。")
            self.hosts[name] = config
            self.store.setting("hosts", self.hosts)
            self.connections.pop(name, None)
        self.record_event("host_configured", host=name)
        return {"id": name, **config}

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

    def history(self, host, request):
        config = self.host(host)
        if config["transport"] == "local":
            return catalog.main(request)
        target = config["target"]
        if target.startswith("-") or any(c.isspace() for c in target):
            raise ValueError("Invalid SSH target")
        payload = base64.urlsafe_b64encode(json.dumps(request, ensure_ascii=False).encode()).decode()
        result = subprocess.run(
            ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", target, "python3 - " + payload],
            input=source_path("sessions", "catalog.py").read_text(encoding="utf-8"), text=True, encoding="utf-8",
            errors="replace", capture_output=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode:
            raise RuntimeError(f"{host} SSH 失败：" + result.stderr[-1500:])
        reply = json.loads(result.stdout)
        if not reply["ok"]:
            raise RuntimeError(reply["error"])
        return reply["result"]

    def discover(self, host, query="", cwd="", limit=20):
        return {"host": host, **self.history(host, {"op": "find", "query": query, "cwd": cwd, "limit": limit})}

    def server(self, host, live=False):
        servers = self.live_servers if live else self.servers
        with self.server_lock:
            if host in servers and not servers[host].closed:
                return servers[host]
            rpc = Rpc(server_command(self.host(host), live=live),
                      on_event=lambda rpc, event: self.worker_event(host, rpc, event),
                      on_request=lambda rpc, request: self.worker_request(host, rpc, request), websocket=live)
            servers[host] = rpc
            return rpc

    def get_agent(self, host, thread_id, plugin=False):
        row = self.store.agent(host, thread_id)
        if not row:
            raise ValueError("未登记的 agent；请先在 UI 选择并连接该对话。")
        if plugin and not row["active"]:
            raise PermissionError("该 agent 未激活；当前 Codex Captain 无权读取或控制。请在 UI 纳入管理范围。")
        owner = self.owners.get((host, thread_id))
        connected = bool(owner and not owner.closed)
        blocked = any(message.get("params", {}).get("threadId") == thread_id for _, message in self.pending_approvals.values())
        return {**row, "agent_id": self.agent_id(host, thread_id), "connected": connected,
                "status": ("blocked" if blocked else "working" if (host, thread_id) in self.active else "idle") if connected else "not_connected"}

    def split_agent_id(self, agent_id):
        parts = agent_id.split("::", 1)
        if len(parts) != 2 or not all(parts):
            raise ValueError("请使用 list_active_agents 返回的精确 agent_id。")
        self.host(parts[0])
        return parts

    def attached_agents(self, active_only=False):
        return [self.get_agent(a["host"], a["thread_id"]) for a in self.store.agents() if not active_only or a["active"]]

    def attach_agent(self, host, thread_id, alias="", access="observe", confirmed_stopped=False):
        if access not in ("observe", "control"):
            raise ValueError("access 必须为 observe 或 control。")
        metadata = self.history(host, {"op": "read", "thread_id": thread_id, "limit": 1})
        previous = self.store.agent(host, thread_id)
        label = alias or metadata["title"]
        self.store.remember(host, thread_id, label, access="observe" if not previous else previous["access"])
        rpc, reason = None, None
        if access == "control":
            try:
                rpc = self.connected_owner(host, thread_id)
            except Exception as error:
                reason = str(error)
            if not rpc and confirmed_stopped:
                rpc = self.server(host, live=False)
                response = rpc.call("thread/resume", {"threadId": thread_id, "excludeTurns": True})
                self.bind_owner(host, thread_id, rpc, response["thread"])
                self.store.remember(host, thread_id, label, owned=True)
            if rpc:
                self.store.remember(host, thread_id, label, access="control")
        else:
            self.store.remember(host, thread_id, label, access="observe")
        row = self.get_agent(host, thread_id)
        self.record_event("agent_attached", host=host, thread_id=thread_id, access=row["access"])
        return {**row, "control_available": bool(rpc) if access == "control" else False,
                "reason": reason or ("原运行服务未接入；已登记为观察。仅在原进程已停止时选择恢复控制。" if access == "control" and not rpc else None)}

    def activate_agent(self, host, thread_id, active):
        with self.registry_lock:
            self.get_agent(host, thread_id)
            self.store.set_active(host, thread_id, active)
            if not active:
                for row in self.store.outbox("queued"):
                    if row["host"] == host and row["thread_id"] == thread_id and row["origin"] == "plugin":
                        self.store.update_outbox(row["id"], "scope_revoked", error="已移出 Captain 管理范围；尚未提交的消息不会执行。")
        self.record_event("agent_activated" if active else "agent_deactivated", host=host, thread_id=thread_id)
        return self.get_agent(host, thread_id)

    def bind_owner(self, host, thread_id, rpc, thread, expected_version=None):
        key = (host, thread_id)
        with self.server_lock:
            self.loaded.add(key)
            self.owners[key] = rpc
            if expected_version is not None and self.runtime_versions.get(key, 0) != expected_version:
                return  # Notifications received during resume are newer than its snapshot.
            self.active.pop(key, None)
            for turn in thread.get("turns", []):
                if turn.get("status") == "inProgress":
                    self.active[key] = turn["id"]
            if thread.get("status", {}).get("type") == "active" and key not in self.active:
                self.active[key] = "external-active"

    def connected_owner(self, host, thread_id):
        owner = self.owners.get((host, thread_id))
        if owner and not owner.closed:
            return owner
        row = self.store.agent(host, thread_id)
        if not row or not self.host(host).get("live"):
            return None
        rpc = self.server(host, live=True)
        loaded = rpc.call("thread/loaded/list", {}).get("data", [])
        if thread_id not in loaded and not row["owned"]:
            return None
        version = self.runtime_versions.get((host, thread_id), 0)
        result = rpc.call("thread/resume", {"threadId": thread_id, "excludeTurns": True})
        self.bind_owner(host, thread_id, rpc, result["thread"], expected_version=version)
        return rpc

    def read_agent(self, host, thread_id, limit=18, plugin=False):
        agent = self.get_agent(host, thread_id, plugin=plugin)
        data = self.history(host, {"op": "read", "thread_id": thread_id, "limit": limit})
        if agent["connected"]:
            data["live_status"] = agent["status"]
            data["live_source"] = "connected_owner_app_server"
        elif self.host(host).get("live"):
            try:
                runtime = self.server(host, live=True).call("thread/read", {"threadId": thread_id, "includeTurns": False})
                data["runtime_status"] = runtime["thread"].get("status", {"type": "unknown"})
                data["runtime_note"] = "notLoaded 不能证明独立 CLI 进程已停止。此次读取没有恢复对话。"
            except Exception as error:
                data["runtime_unavailable"] = str(error)
        return {"host": host, "agent_id": agent["agent_id"], "access": agent["access"], **data}

    def send(self, host, thread_id, text, origin="ui", request_key=None):
        if not isinstance(text, str) or not text.strip() or len(text) > 30000:
            raise ValueError("消息不能为空，最长 30000 字符。")
        with self.registry_lock:
            agent = self.get_agent(host, thread_id, plugin=origin == "plugin")
            if agent["access"] != "control":
                raise PermissionError("该对话为只读观察；请在 UI 显式接入控制后再发送。")
            request_id = self.store.enqueue(host, thread_id, text, origin=origin, request_key=request_key)
            row = self.store.delivery(request_id)
            if row["state"] not in ("queued", "awaiting_connection"):
                return row
            try:
                rpc = self.connected_owner(host, thread_id)
                if not rpc:
                    self.store.update_outbox(request_id, "awaiting_connection", error="原运行服务未接入；未发送。")
                else:
                    if row["state"] == "awaiting_connection":
                        self.store.update_outbox(request_id, "queued")
                    self.flush(host, thread_id, rpc)
            except Exception as error:
                if self.store.delivery(request_id)["state"] == "queued":
                    self.store.update_outbox(request_id, "awaiting_connection", error=str(error))
                raise
            return self.store.delivery(request_id)

    def flush(self, host, thread_id, rpc):
        with self.registry_lock, self.server_lock:
            if (host, thread_id) in self.active:
                return
            pending = [row for row in self.store.outbox("queued") if row["host"] == host and row["thread_id"] == thread_id]
            for row in pending:
                agent = self.get_agent(host, thread_id)
                if agent["access"] != "control" or (row["origin"] == "plugin" and not agent["active"]):
                    self.store.update_outbox(row["id"], "scope_revoked", error="控制权限或 Captain 管理范围已撤销。")
                    continue
                self.store.update_outbox(row["id"], "submitting")
                self.active[(host, thread_id)] = "submitting"
                try:
                    response = rpc.call("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": row["text"]}]})
                    turn_id = response["turn"]["id"]
                    self.active[(host, thread_id)] = turn_id
                    self.store.update_outbox(row["id"], "submitted", turn_id)
                    self.record_event("message_submitted", host=host, thread_id=thread_id, request_id=row["id"], turn_id=turn_id, origin=row["origin"])
                except Exception as error:
                    self.store.update_outbox(row["id"], "unknown", error=str(error))
                    self.active.pop((host, thread_id), None)
                    raise
                break

    def create_agent(self, host, cwd, title, text="", active=True, request_key=None, origin="ui"):
        config = self.host(host)
        if origin == "plugin" and not self.connections.get(host, {}).get("connected"):
            raise PermissionError("该机器尚未在 UI 连接；请先选择并连接机器。")
        local_absolute = config["transport"] == "local" and (bool(re.match(r"^[A-Za-z]:[\\/]", cwd)) or Path(cwd).is_absolute())
        if not local_absolute and not (config["transport"] == "ssh" and cwd.startswith("/")):
            raise ValueError("项目目录必须是该机器上的绝对路径。")
        request_key = request_key or "ui-create-" + str(uuid.uuid4())
        fingerprint = hashlib.sha256(json.dumps([host, cwd, title, text, bool(active)], ensure_ascii=False).encode()).hexdigest()
        previous = self.store.begin_operation(request_key, fingerprint)
        if previous is not None:
            return previous
        rpc = self.server(host, live=bool(config.get("live")))
        models = rpc.call("model/list", {}).get("data", [])
        model = next((m.get("model", m["id"]) for m in models if m.get("isDefault")), None)
        if not model:
            raise RuntimeError("机器未返回可用默认模型。")
        response = rpc.call("thread/start", {"cwd": cwd, "model": model, "approvalPolicy": "on-request", "sandbox": "workspace-write"})
        thread_id = response["thread"]["id"]
        self.bind_owner(host, thread_id, rpc, response["thread"])
        self.store.remember(host, thread_id, title, owned=True, active=active, access="control")
        rpc.call("thread/name/set", {"threadId": thread_id, "name": title})
        result = {"created": True, **self.get_agent(host, thread_id)}
        self.store.finish_operation(request_key, result)
        self.record_event("agent_created", host=host, thread_id=thread_id, alias=title, origin=origin)
        if text.strip():
            try:
                result["delivery"] = self.send(host, thread_id, text, origin=origin, request_key=request_key + ":message")
            except Exception as error:
                result["delivery"] = next((row for row in self.store.outbox() if row.get("request_key") == request_key + ":message"),
                                           {"state": "not_sent", "error": str(error)})
                self.store.finish_operation(request_key, result)
                raise
            else:
                self.store.finish_operation(request_key, result)
        return result

    def worker_event(self, host, rpc, event):
        params = event.get("params", {})
        thread_id = params.get("threadId")
        if not thread_id or not self.store.agent(host, thread_id):
            return
        with self.server_lock:
            if event["method"] == "turn/started":
                self.runtime_versions[(host, thread_id)] = self.runtime_versions.get((host, thread_id), 0) + 1
                self.active[(host, thread_id)] = params["turn"]["id"]
            elif event["method"] == "turn/completed":
                self.runtime_versions[(host, thread_id)] = self.runtime_versions.get((host, thread_id), 0) + 1
                turn = params.get("turn", {})
                if self.active.get((host, thread_id)) in (None, "external-active", turn.get("id")):
                    self.active.pop((host, thread_id), None)
                self.store.complete_turn(host, thread_id, turn.get("id"), turn.get("status", "unknown"),
                                         json.dumps(turn.get("error")) if turn.get("error") else None)
                self.record_event("worker_completed", host=host, thread_id=thread_id, turn=turn)
                if not self.stopping:
                    self.pool.submit(self.flush, host, thread_id, rpc)

    def worker_request(self, host, rpc, message):
        key = f"{host}:{message['id']}:{time.time_ns()}"
        self.pending_approvals[key] = (rpc, message)
        params = message.get("params", {})
        self.record_event("approval_needed", key=key, host=host, thread_id=params.get("threadId"), method=message["method"], params=params)

    def approve(self, key, decision, answers=None):
        if key not in self.pending_approvals:
            raise ValueError("请求已处理或连接已失效。")
        rpc, message = self.pending_approvals[key]
        if message["method"] in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            result = {"decision": "accept" if decision == "accept" else "decline"}
        elif message["method"] == "tool/requestUserInput":
            result = {"answers": answers or {}}
        else:
            if decision == "accept":
                raise ValueError("此类型请在原生 Codex 中处理。")
            rpc._write({"id": message["id"], "error": {"code": -32000, "message": "User declined"}})
            self.pending_approvals.pop(key)
            return
        rpc.reply(message["id"], result)
        self.pending_approvals.pop(key)
        self.record_event("approval_resolved", key=key, decision=decision)

    def plugin_events(self, after=0, timeout_seconds=0):
        deadline = time.monotonic() + min(30, max(0, float(timeout_seconds)))
        cursor = max(0, int(after))
        while True:
            rows = self.store.events(cursor, kinds=MANAGEMENT_EVENTS)
            selected = []
            for event in rows:
                cursor = event["id"]
                data = event["data"]
                agent = self.store.agent(data.get("host"), data.get("thread_id"))
                if agent and agent["active"]:
                    selected.append(event)
            if selected or time.monotonic() >= deadline:
                return {"events": selected, "cursor": cursor}
            with self.event_condition:
                self.event_condition.wait(min(1, max(0, deadline - time.monotonic())))

    def plugin_call(self, tool, arguments):
        if tool == "list_active_agents":
            return {"agents": self.attached_agents(active_only=True), "controller": "current_codex_conversation",
                    "connected_hosts": [host for host, state in self.connections.items() if state.get("connected")],
                    "scope_note": "只列 UI 已激活的 agent。没有全盘扫描，没有独立 Captain 模型。"}
        if tool == "poll_events":
            return self.plugin_events(arguments.get("after", 0), arguments.get("timeout_seconds", 0))
        if tool == "create_agent":
            if not arguments.get("request_key"):
                raise ValueError("创建必须提供唯一 request_key；重试使用同一个值。")
            return self.create_agent(arguments["host"], arguments["cwd"], arguments["title"], arguments.get("text", ""),
                                     active=True, request_key=arguments["request_key"], origin="plugin")
        if tool == "get_delivery":
            row = self.store.delivery(arguments["request_id"])
            if not row:
                raise ValueError("没有此消息。")
            self.get_agent(row["host"], row["thread_id"], plugin=True)
            return row
        host, thread_id = self.split_agent_id(arguments["agent_id"])
        if tool == "get_agent":
            return self.get_agent(host, thread_id, plugin=True)
        if tool == "read_agent":
            return self.read_agent(host, thread_id, arguments.get("limit", 18), plugin=True)
        if tool == "send_agent_message":
            if not arguments.get("request_key"):
                raise ValueError("发送必须提供唯一 request_key；重试使用同一个值。")
            return self.send(host, thread_id, arguments["text"], origin="plugin", request_key=arguments["request_key"])
        raise ValueError("Unsupported plugin tool")

    def state(self, after=0):
        return {"version": VERSION, "controller": "codex-plugin",
                "hosts": [{"id": key, **config, **self.connections.get(key, {"connected": False})} for key, config in self.hosts.items()],
                "agents": self.attached_agents(), "events": self.store.events(after, kinds=MANAGEMENT_EVENTS), "outbox": self.store.outbox(),
                "approvals": [{"key": key, "method": message["method"], "params": message.get("params", {})}
                              for key, (_, message) in self.pending_approvals.copy().items()]}

    def close(self):
        if self.stopping:
            return
        self.stopping = True
        for rpc in list(self.servers.values()) + list(self.live_servers.values()):
            rpc.close()
        self.pool.shutdown(wait=True)
        self.store.close()
