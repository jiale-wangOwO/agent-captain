"""Private loopback client and lazy launcher for the independent PTY owner."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse, quote

ROOT = Path(__file__).resolve().parent


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("Terminal broker redirects are not allowed")


class BrokerError(RuntimeError):
    def __init__(self, message, code=None, status=None):
        super().__init__(message)
        self.code, self.status = code, status


def descriptor_path():
    override = os.environ.get("CEO_HUB_TERMINAL_FILE")
    if override:
        return Path(override)
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "CEOHub/terminal-broker.json"
    return Path.home() / ".local/state/ceo-hub/terminal-broker.json"


class TerminalClient:
    def __init__(self, descriptor_file=None, runtime=None):
        self.lock = threading.RLock()
        self.descriptor_file = Path(descriptor_file) if descriptor_file is not None else None
        self.runtime = Path(runtime) if runtime is not None else ROOT / ".runtime"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def descriptor(self):
        data = json.loads((self.descriptor_file or descriptor_path()).read_text(encoding="utf-8"))
        url = data.get("url", "")
        parsed = urlparse(url)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError("Invalid local terminal broker descriptor")
        if not isinstance(data.get("token"), str) or len(data["token"]) < 16:
            raise ValueError("Invalid terminal capability")
        return data

    def request(self, path, body=None, timeout=20):
        data = self.descriptor()
        headers = {"Authorization": "Bearer " + data["token"]}
        payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        if payload is not None:
            headers["Content-Type"] = "application/json"
        try:
            with self.opener.open(urllib.request.Request(data["url"].rstrip("/") + path, data=payload, headers=headers), timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            try:
                reply = json.loads(error.read())
            except Exception:
                reply = {}
            raise BrokerError(reply.get("message", "Terminal broker request failed"), reply.get("error"), error.code) from None

    def running(self):
        try:
            data = self.descriptor()
            health = self.request("/health", timeout=2)
            return health.get("pid") == data.get("pid") and health.get("version") == "0.3.0"
        except Exception:
            return False

    def ensure_started(self):
        with self.lock:
            if self.running():
                return
            node = shutil.which("node")
            if not node:
                raise RuntimeError("需要 Node.js；请安装 Node.js 后执行 npm ci。")
            if not (ROOT / "node_modules/node-pty").exists():
                raise RuntimeError("终端依赖未安装；请在 PRON Hub 目录执行 npm ci。")
            logs = self.runtime
            logs.mkdir(parents=True, exist_ok=True)
            environment = os.environ.copy()
            if self.descriptor_file is not None:
                environment["CEO_HUB_TERMINAL_FILE"] = str(self.descriptor_file.resolve())
            with (logs / "terminal.out.log").open("ab") as output, (logs / "terminal.err.log").open("ab") as error:
                process = subprocess.Popen([node, str(ROOT / "terminal_broker.cjs")], cwd=ROOT,
                    stdin=subprocess.DEVNULL, stdout=output, stderr=error,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), start_new_session=os.name != "nt", env=environment)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("终端服务启动失败；请查看 .runtime/terminal.err.log。")
                if self.running():
                    return
                time.sleep(.15)
            raise RuntimeError("终端服务尚未就绪；请查看 .runtime/terminal.err.log。")

    def sessions(self):
        # Reading registry/state never launches the broker or SSH.
        try:
            return self.request("/sessions", timeout=3).get("sessions", [])
        except Exception:
            return []

    def create(self, body):
        self.ensure_started()
        return self.request("/sessions/create", body)

    def snapshot(self, terminal_id, lines=60):
        return self.request(f"/sessions/{quote(terminal_id, safe='')}/snapshot?lines={min(5000, max(1, int(lines)))}")

    def output(self, terminal_id, after=0):
        if int(after) == 0:
            snapshot = self.snapshot(terminal_id, 5000)
            return {"data": "", "reset": True, "ansi": snapshot["ansi"], "seq": snapshot["current_seq"],
                    "connected": snapshot["connected"], "state": snapshot["state"], "status": snapshot["state"],
                    "command": snapshot.get("command", ""), "agent_ready": snapshot.get("agent_ready", False),
                    "blocked_reason": snapshot.get("blocked_reason"), "human_busy": snapshot.get("human_busy", False),
                    "cols": snapshot["cols"], "rows": snapshot["rows"]}
        return self.request(f"/sessions/{quote(terminal_id, safe='')}/output?after={max(0, int(after))}")

    def write(self, terminal_id, action, body):
        if action not in ("input", "prompt", "resize", "close"):
            raise ValueError("Unknown terminal action")
        return self.request(f"/sessions/{quote(terminal_id, safe='')}/{action}", body)
