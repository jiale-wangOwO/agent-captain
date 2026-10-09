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


def legacy_descriptor_path():
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "CEOHub/terminal-broker.json"
    return Path.home() / ".local/state/ceo-hub/terminal-broker.json"


def worker_reference_path():
    return ROOT / ".runtime" / "worker-terminal-reference.json"


def descriptor_path():
    override = os.environ.get("CEO_HUB_TERMINAL_FILE")
    if override:
        return Path(override).expanduser()
    reference = worker_reference_path()
    if not os.path.lexists(reference):
        return legacy_descriptor_path()
    # A small, private pointer preserves the existing PTY owner; it contains no
    # bearer token. Invalid pinned references must never silently choose another
    # user's/namespace's descriptor or manufacture a replacement owner.
    try:
        with reference.open("rb") as stream:
            raw = stream.read(8193)
        if len(raw) > 8192:
            raise ValueError()
        value = json.loads(raw)
        pointer = value.get("descriptor_file") if isinstance(value, dict) else None
        if (not isinstance(value, dict) or set(value) != {"descriptor_file"}
                or not isinstance(pointer, str) or not pointer or len(pointer) > 4096
                or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in pointer)
                or pointer.startswith(("\\\\", "//"))):
            raise ValueError()
        target = Path(pointer)
        if not target.is_absolute() or not target.is_file() or target == reference:
            raise ValueError()
        return target
    except (OSError, ValueError, TypeError, UnicodeError):
        raise ValueError("Invalid pinned worker terminal reference; repair the private reference without restarting work terminals") from None


class TerminalClient:
    def __init__(self, descriptor_file=None, runtime=None, launch_env=None):
        self.lock = threading.RLock()
        self.descriptor_file = Path(descriptor_file) if descriptor_file is not None else None
        self.runtime = Path(runtime) if runtime is not None else ROOT / ".runtime"
        # Optional manager-only launch context. Laziness keeps metadata reads
        # free of registry/network activity; worker clients retain the original
        # plain environment-copy behavior.
        self.launch_env = launch_env
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    @staticmethod
    def _read_descriptor_file(filename):
        with Path(filename).open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError("Invalid terminal descriptor size")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("Invalid local terminal broker descriptor")
        url = data.get("url", "")
        if not isinstance(url, str) or len(url) > 4096 or any(char.isspace() for char in url):
            raise ValueError("Invalid local terminal broker descriptor")
        parsed = urlparse(url)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError("Invalid local terminal broker descriptor")
        if not isinstance(data.get("token"), str) or not 16 <= len(data["token"]) <= 4096 or any(ord(char) < 33 or ord(char) > 126 for char in data["token"]):
            raise ValueError("Invalid terminal capability")
        if type(data.get("pid")) is not int or data["pid"] <= 0:
            raise ValueError("Invalid local terminal broker identity")
        return data

    def descriptor(self):
        return self._read_descriptor_file(self.descriptor_file or descriptor_path())

    def _anchor_default_worker(self, chosen):
        if self.descriptor_file is not None or os.environ.get("CEO_HUB_TERMINAL_FILE"):
            return
        try:
            physical = Path(chosen).resolve(strict=True)
            self._read_descriptor_file(physical)
            reference = worker_reference_path()
            if os.path.lexists(reference):
                if descriptor_path().resolve(strict=True) != physical:
                    raise ValueError()
                return
            reference.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                with reference.open("x", encoding="utf-8", newline="\n") as stream:
                    json.dump({"descriptor_file": str(physical)}, stream)
                    stream.write("\n")
                reference.chmod(0o600)
            except FileExistsError:
                # Another verified workflow may have established the same owner.
                # Never replace an unknown or competing reference.
                if descriptor_path().resolve(strict=True) != physical:
                    raise ValueError()
        except (OSError, ValueError):
            raise BrokerError("Could not safely anchor the verified work-terminal owner; inspect and repair the private reference. Existing references and work terminals were not replaced.",
                              "bound_transport_unavailable") from None

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
            pinned = self.descriptor_file is None and not os.environ.get("CEO_HUB_TERMINAL_FILE") and os.path.lexists(worker_reference_path())
            try:
                chosen = (self.descriptor_file or descriptor_path()).resolve()
            except (OSError, ValueError):
                if pinned:
                    raise BrokerError("The bound work-terminal reference is invalid or missing its target; repair the private reference to the original broker. No replacement broker was started.",
                                      "bound_transport_unavailable") from None
                raise
            if self.running():
                self._anchor_default_worker(chosen)
                return
            if pinned:
                raise BrokerError("The bound work-terminal owner is unavailable; verify the original broker and repair the private reference. No replacement broker was started and existing work terminals were not restarted.",
                                  "bound_transport_unavailable")
            node = shutil.which("node")
            if not node:
                raise RuntimeError("需要 Node.js；请安装 Node.js 后执行 npm ci。")
            if not (ROOT / "node_modules/node-pty").exists():
                raise RuntimeError("终端依赖未安装；请在 PRON Hub 目录执行 npm ci。")
            logs = self.runtime
            logs.mkdir(parents=True, exist_ok=True)
            inherited = self.launch_env() if callable(self.launch_env) else self.launch_env
            environment = os.environ.copy() if inherited is None else dict(inherited)
            environment["CEO_HUB_TERMINAL_FILE"] = str(chosen)
            with (logs / "terminal.out.log").open("ab") as output, (logs / "terminal.err.log").open("ab") as error:
                process = subprocess.Popen([node, str(ROOT / "terminal_broker.cjs")], cwd=ROOT,
                    stdin=subprocess.DEVNULL, stdout=output, stderr=error,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), start_new_session=os.name != "nt", env=environment)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("终端服务启动失败；请查看 .runtime/terminal.err.log。")
                if self.running():
                    self._anchor_default_worker(chosen)
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
