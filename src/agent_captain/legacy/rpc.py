"""Small bidirectional JSONL client; credentials stay with the Codex process."""
from __future__ import annotations

import json
import base64
import hashlib
import os
import queue
import shutil
import subprocess
import struct
import threading
from collections import deque
from pathlib import Path


def codex_command() -> list[str]:
    override = os.environ.get("CEO_CODEX_EXECUTABLE")
    if override:
        return [override]
    if os.name == "nt":
        # Avoid cmd.exe argument quoting and npm's PowerShell execution policy.
        script = Path(os.environ.get("APPDATA", "")) / "npm/node_modules/@openai/codex/bin/codex.js"
        if script.is_file() and shutil.which("node"):
            return [shutil.which("node"), str(script)]
    executable = shutil.which("codex")
    if not executable:
        raise RuntimeError("找不到 Codex CLI。请安装 Codex 并登录。")
    return [executable]


def server_command(host: dict, *, live: bool = False) -> list[str]:
    args = ["app-server", "proxy"] if live else ["app-server", "--stdio"]
    if host["transport"] == "local":
        return codex_command() + args
    # Target and executable are administrator configuration, never model text.
    target = host["target"]
    if not target or target.startswith("-") or any(c.isspace() for c in target):
        raise ValueError("Invalid SSH target")
    import shlex
    remote = "exec codex " + " ".join(shlex.quote(a) for a in args)
    return ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", target,
            "bash -lc " + shlex.quote(remote)]


class Rpc:
    def __init__(self, command: list[str], on_event=None, on_request=None, websocket=False):
        self.on_event = on_event or (lambda *_: None)
        self.on_request = on_request or self._unsupported
        self.pending: dict[int, queue.Queue] = {}
        self.sequence = 0
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.errors = deque(maxlen=20)
        self.closed = False
        self.events = queue.Queue()
        self.event_thread = threading.Thread(target=self._event_loop, daemon=True)
        self.event_thread.start()
        self.websocket = websocket
        self.ws_ready = queue.Queue()
        self.ws_key = base64.b64encode(os.urandom(16)).decode()
        child_env = os.environ.copy()
        if os.name == "nt" and not any(child_env.get(k) for k in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")):
            # Respect the existing Windows proxy; do not mutate registry or
            # machine-wide environment settings. Never log proxy credentials.
            import winreg
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as key:
                    enabled = winreg.QueryValueEx(key, "ProxyEnable")[0]
                    proxy = winreg.QueryValueEx(key, "ProxyServer")[0]
                if enabled and proxy:
                    entries = dict(part.split("=", 1) for part in proxy.split(";") if "=" in part)
                    value = entries.get("https") or entries.get("http") or (proxy if "=" not in proxy else None)
                    if value:
                        child_env["HTTPS_PROXY"] = value if "://" in value else "http://" + value
                        child_env["HTTP_PROXY"] = child_env["HTTPS_PROXY"]
                        child_env.setdefault("NO_PROXY", "localhost,127.0.0.1,::1")
            except OSError:
                pass
        self.process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            **({"bufsize": 0} if websocket else {"text": True, "encoding": "utf-8", "errors": "replace", "bufsize": 1}),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            env=child_env,
        )
        self.stderr_thread = threading.Thread(target=self._stderr, daemon=True)
        self.reader_thread = threading.Thread(target=self._read, daemon=True)
        self.stderr_thread.start()
        self.reader_thread.start()
        try:
            if websocket:
                # `app-server proxy` is a raw byte proxy to a WebSocket-over-Unix
                # control socket, not a JSONL-to-WebSocket protocol converter.
                handshake = ("GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                             f"Sec-WebSocket-Key: {self.ws_key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
                self.process.stdin.write(handshake.encode())
                self.process.stdin.flush()
                ready = self.ws_ready.get(timeout=20)
                if isinstance(ready, Exception):
                    raise ready
            self.call("initialize", {
                "clientInfo": {"name": "ceo_hub", "title": "AgentCaptain", "version": "0.2.0"},
                "capabilities": {"experimentalApi": True},
            }, timeout=30)
            self.notify("initialized", {})
        except Exception:
            self.close()
            raise

    def _stderr(self):
        for line in self.process.stderr:
            if isinstance(line, bytes):
                line = line.decode("utf-8", errors="replace")
            self.errors.append(line.strip())

    def _exact(self, count):
        data = bytearray()
        while len(data) < count:
            packet = self.process.stdout.read(count - len(data))
            if not packet:
                raise EOFError("WebSocket connection closed")
            data.extend(packet)
        return bytes(data)

    def _ws_send(self, payload, opcode=1):
        mask = os.urandom(4)
        length = len(payload)
        header = bytes([0x80 | opcode])
        if length < 126:
            header += bytes([0x80 | length])
        elif length < 65536:
            header += bytes([0x80 | 126]) + struct.pack("!H", length)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", length)
        masked = bytes(value ^ mask[i % 4] for i, value in enumerate(payload))
        self.process.stdin.write(header + mask + masked)
        self.process.stdin.flush()

    def _lines(self):
        if not self.websocket:
            yield from self.process.stdout
            return
        try:
            header = bytearray()
            while not header.endswith(b"\r\n\r\n"):
                header.extend(self._exact(1))
                if len(header) > 16384:
                    raise ValueError("WebSocket handshake too large")
            expected = base64.b64encode(hashlib.sha1((self.ws_key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
            if not header.startswith(b"HTTP/1.1 101") or expected.lower() not in header.lower():
                raise RuntimeError("WebSocket handshake rejected: " + header.decode(errors="replace")[:1000])
            self.ws_ready.put(True)
        except Exception as error:
            self.ws_ready.put(error)
            raise
        fragmented = bytearray()
        while True:
            first, second = self._exact(2)
            opcode, final = first & 15, bool(first & 128)
            length = second & 127
            if length == 126:
                length = struct.unpack("!H", self._exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._exact(8))[0]
            if length > 32 * 1024 * 1024:
                raise ValueError("WebSocket message too large")
            mask = self._exact(4) if second & 128 else None
            payload = self._exact(length)
            if mask:
                payload = bytes(value ^ mask[i % 4] for i, value in enumerate(payload))
            if opcode == 8:
                return
            if opcode == 9:
                with self.write_lock:
                    self._ws_send(payload, opcode=10)
                continue
            if opcode == 10:
                continue
            if opcode not in (0, 1):
                continue
            fragmented.extend(payload)
            if len(fragmented) > 32 * 1024 * 1024:
                raise ValueError("WebSocket fragmented message too large")
            if final:
                yield fragmented.decode("utf-8")
                fragmented.clear()

    def _read(self):
        try:
            for line in self._lines():
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if "method" in message:
                    if "id" in message:
                        threading.Thread(target=self.on_request, args=(self, message), daemon=True).start()
                    else:
                        self.events.put(message)
                elif "id" in message:
                    with self.lock:
                        waiter = self.pending.get(message["id"])
                    if waiter:
                        waiter.put(message)
        except Exception as error:
            self.errors.append(str(error))
        finally:
            self.closed = True
            with self.lock:
                waiters = list(self.pending.values())
            for waiter in waiters:
                waiter.put({"error": {"message": "Codex 连接已断开：" + "\n".join(self.errors)[-2000:]}})
            self.events.put(None)

    def _event_loop(self):
        # Do not block the response reader while an event submits a follow-up
        # RPC. Keep event ordering while allowing independent responses through.
        while True:
            message = self.events.get()
            if message is None:
                return
            try:
                self.on_event(self, message)
            except Exception as error:
                self.errors.append(str(error))

    def _write(self, message):
        with self.write_lock:
            if self.closed:
                raise RuntimeError("Codex 连接已关闭")
            data = json.dumps(message, ensure_ascii=False)
            if self.websocket:
                self._ws_send(data.encode("utf-8"))
            else:
                self.process.stdin.write(data + "\n")
                self.process.stdin.flush()

    def notify(self, method, params):
        self._write({"method": method, "params": params})

    def reply(self, request_id, result):
        self._write({"id": request_id, "result": result})

    def _unsupported(self, rpc, message):
        rpc._write({"id": message["id"], "error": {"code": -32601, "message": "Unsupported client request"}})

    def call(self, method, params, timeout=60):
        waiter = queue.Queue()
        with self.lock:
            self.sequence += 1
            request_id = self.sequence
            self.pending[request_id] = waiter
        try:
            self._write({"id": request_id, "method": method, "params": params})
            try:
                message = waiter.get(timeout=timeout)
            except queue.Empty:
                raise TimeoutError(f"{method} 超时；写操作可能已执行，请先核对状态，勿自动重试。") from None
            if "error" in message:
                raise RuntimeError(str(message["error"].get("message", message["error"])))
            return message.get("result", {})
        finally:
            with self.lock:
                self.pending.pop(request_id, None)

    def close(self):
        self.closed = True
        with self.write_lock:
            if not self.process.stdin.closed:
                self.process.stdin.close()
        if self.process.poll() is None:
            # Only this client's owned subprocess, never a shared daemon.
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        self.reader_thread.join(timeout=2)
        self.stderr_thread.join(timeout=2)
        self.event_thread.join(timeout=2)
        if not self.reader_thread.is_alive():
            self.process.stdout.close()
        if not self.stderr_thread.is_alive():
            self.process.stderr.close()
