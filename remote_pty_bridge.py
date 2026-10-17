"""Own one SSH display attachment and resize its already-open remote PTY.

The SSH server can leave the slave device root-owned. The SSH process still
inherits a usable fd 0, but a later SSH connection cannot reopen that device.
This wrapper retains that fd, never reads task input, and exposes only resize
over a private per-connection Unix socket. It never kills a tmux pane/session.
"""
from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import re
import signal
import socket
import stat
import struct
import subprocess
import sys
import termios
import threading
import time
from pathlib import Path

SESSION = re.compile(r"ceohub-[a-f0-9]{16,48}\Z")
BRIDGE = re.compile(r"[a-f0-9]{24,48}\Z")
MAX_REQUEST = 4096
RESULT_MARKER = "CEO_HUB_PTY_RESULT="
EXISTING_MARKER = "/.__pronhub_existing__/"


class BridgeError(RuntimeError):
    pass


class ResizeFailure(BridgeError):
    def __init__(self, result):
        super().__init__(result["error"])
        self.result = result


def attach_directory(cwd, session_id):
    """Backwards-compatible strict-attach hint carried by old broker's cwd arg.

    The reserved absolute namespace is never used as a directory. Normal first
    creation keeps its historical cwd behavior; exact marker means attach-only.
    Malformed markers fail closed, never creating a replacement shell.
    """
    if isinstance(cwd, str) and cwd.startswith("/.__pronhub_existing"):
        if not SESSION.fullmatch(session_id):
            raise BridgeError("Invalid existing-session attachment marker")
        base = EXISTING_MARKER + session_id
        if cwd == base:
            return "", True, None
        if not cwd.startswith(base + "/"):
            raise BridgeError("Invalid existing-session attachment marker")
        encoded = cwd[len(base) + 1:]
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,2048}", encoded):
            raise BridgeError("Invalid existing-session attachment marker")
        try:
            expected = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        except (ValueError, UnicodeError):
            raise BridgeError("Invalid existing-session attachment marker") from None
        if not valid_session_identity(expected, session_id):
            raise BridgeError("Invalid existing-session attachment marker")
        return "", True, expected
    return cwd, False, None


def valid_session_identity(value, session_id):
    return (isinstance(value, dict) and set(value) == {"name", "session", "created", "pane", "pane_pid", "pane_start_ticks"}
        and value["name"] == session_id and isinstance(value["session"], str) and re.fullmatch(r"\$[0-9]+", value["session"])
        and isinstance(value["pane"], str) and re.fullmatch(r"%[0-9]+", value["pane"])
        and all(type(value[key]) is int and value[key] > 0 for key in ("created", "pane_pid", "pane_start_ticks")))


def probe_existing_session(session_id):
    """Read one exact tmux/pane identity; no stdin, cwd, argv or task text."""
    if not isinstance(session_id, str) or not SESSION.fullmatch(session_id):
        raise BridgeError("Invalid owned terminal session ID")
    target = "=" + session_id
    try:
        exists = subprocess.run(["tmux", "has-session", "-t", target], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return {"ok": False, "exists": False, "reason": "probe_unavailable"}
    if exists.returncode:
        return {"ok": True, "exists": False, "reason": "session_missing"}
    # display-message resolves a target-pane, not a target-session. The colon
    # makes the exact session component explicit and selects its current pane;
    # bare '=name' can succeed with an empty format context on real tmux.
    fields = tmux("display-message", "-p", "-t", target + ":",
        "#{session_name}\t#{session_id}\t#{session_created}\t#{pane_id}\t#{pane_pid}").split("\t")
    if len(fields) != 5 or fields[0] != session_id or not re.fullmatch(r"\$[0-9]+", fields[1]) or not fields[2].isdigit() or not re.fullmatch(r"%[0-9]+", fields[3]) or not fields[4].isdigit():
        raise BridgeError("Owned session identity was not confirmed")
    pid = int(fields[4])
    if pid <= 0:
        raise BridgeError("Owned pane identity was not confirmed")
    path = Path("/proc") / str(pid)
    try:
        with (path / "stat").open("rb") as stream:
            raw = stream.read(8193)
        if len(raw) > 8192 or path.stat().st_uid != os.geteuid():
            raise ValueError()
        prefix, separator, suffix = raw.decode("utf-8").rpartition(")")
        values = suffix.split()
        if not separator or int(prefix.split(" ", 1)[0]) != pid or len(values) < 20:
            raise ValueError()
        ticks = int(values[19])
        if ticks <= 0:
            raise ValueError()
    except (OSError, ValueError, UnicodeError):
        raise BridgeError("Owned pane identity was not confirmed") from None
    return {"ok": True, "exists": True, "identity": {"name": fields[0], "session": fields[1],
            "created": int(fields[2]), "pane": fields[3], "pane_pid": pid, "pane_start_ticks": ticks}}


def size(cols, rows):
    if type(cols) is not int or type(rows) is not int or not 20 <= cols <= 500 or not 5 <= rows <= 200:
        raise BridgeError("Resize requires 20-500 columns and 5-200 rows")
    return cols, rows


def identity(session_id, bridge_id):
    if not isinstance(session_id, str) or not SESSION.fullmatch(session_id):
        raise BridgeError("Invalid owned terminal session ID")
    if not isinstance(bridge_id, str) or not BRIDGE.fullmatch(bridge_id):
        raise BridgeError("Invalid connection capability")


def private_directory(create=False):
    home = Path.home()
    current = home
    for part in (".local", "state", "ceo-hub", "pty-bridge"):
        current = current / part
        if create:
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
        try:
            info = current.lstat()
        except FileNotFoundError:
            raise BridgeError("Remote resize bridge is not connected") from None
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise BridgeError("Resize directory must be user-owned, non-symlink, and not writable by others")
    if current.lstat().st_mode & 0o077:
        raise BridgeError("Private resize directory must have mode 0700")
    return current


def socket_path(session_id, bridge_id, create=False):
    identity(session_id, bridge_id)
    key = hashlib.sha256((session_id + ":" + bridge_id).encode()).hexdigest()[:24]
    path = private_directory(create) / (key + ".sock")
    if len(os.fsencode(path)) >= 104:
        raise BridgeError("Remote home path is too long for a private Unix socket")
    return path


def check_socket(path):
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise BridgeError("Resize socket identity or permissions are invalid")


def tmux(*args):
    try:
        result = subprocess.run(["tmux", *args], stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        raise BridgeError("Could not query the owned tmux attachment") from None
    if result.returncode:
        # Do not echo remote startup/provider output or arbitrary request text.
        raise BridgeError("Owned tmux operation failed")
    if len(result.stdout) > 65536:
        raise BridgeError("Owned tmux metadata exceeded the limit")
    return result.stdout.strip()


def winsize(fd):
    rows, cols, _, _ = struct.unpack("HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8))
    return cols, rows


def apply_winsize(fd, cols, rows):
    size(cols, rows)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    if winsize(fd) != (cols, rows):
        raise BridgeError("Original SSH PTY did not accept the requested dimensions")


class Attachment:
    def __init__(self, session_id, bridge_id, cols, rows, cwd, require_existing=False):
        identity(session_id, bridge_id)
        size(cols, rows)
        if not os.isatty(0):
            raise BridgeError("SSH -tt is required for the display attachment")
        if not isinstance(cwd, str) or len(cwd) > 4096 or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in cwd):
            raise BridgeError("Invalid terminal directory")
        cwd, marker, expected = attach_directory(cwd, session_id)
        self.require_existing = bool(require_existing or marker)
        self.expected_identity = expected
        cwd = cwd or str(Path.home())
        if not os.path.isabs(cwd):
            raise BridgeError("Terminal directory must be absolute")
        self.session_id, self.bridge_id, self.cols, self.rows, self.cwd = session_id, bridge_id, cols, rows, cwd
        self.path = socket_path(session_id, bridge_id, create=True)
        self.tty = os.ttyname(0)
        self.child = None
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.listener = None
        self.socket_inode = None
        self.worker = None

    def client_geometry(self):
        listing = tmux("list-clients", "-t", getattr(self, "attachment_target", "=" + self.session_id),
                       "-F", "#{client_pid}\t#{client_width}\t#{client_height}\t#{client_tty}")
        for line in listing.splitlines():
            fields = line.split("\t")
            if len(fields) == 4 and fields[0] == str(self.child.pid) and fields[3] == self.tty:
                return int(fields[1]), int(fields[2])
        return None

    def window_target(self):
        """Explicit session component for target-window/target-pane commands."""
        return getattr(self, "attachment_target", "=" + self.session_id) + ":"

    def wait_client_size(self, cols, rows):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            if self.stop.is_set() or self.child.poll() is not None:
                raise BridgeError("The owned display attachment has disconnected")
            if self.client_geometry() == (cols, rows):
                return
            time.sleep(0.04)
        raise BridgeError("tmux client did not acknowledge the requested dimensions")

    def window_geometry(self):
        fields = tmux("display-message", "-p", "-t", self.window_target(),
                      "#{window_width}\t#{window_height}\t#{pane_pid}\t#{pane_width}\t#{pane_height}").split("\t")
        if len(fields) != 5:
            raise BridgeError("Owned window metadata was not confirmed")
        return tuple(int(value) for value in fields)

    def resize(self, cols, rows):
        size(cols, rows)
        with self.lock:
            if self.child is None or self.child.poll() is not None or self.stop.is_set():
                raise BridgeError("The owned display attachment is not ready")
            original_size = fcntl.ioctl(0, termios.TIOCGWINSZ, b"\0" * 8)
            old_rows, old_cols, _, _ = struct.unpack("HHHH", original_size)
            old_window = self.window_geometry()
            # fd 0 is the inherited original descriptor: never reopen root-owned
            # /dev/pts paths and never change their ownership or permissions.
            try:
                apply_winsize(0, cols, rows)
                # Signal only our own display child; never the server/pane.
                os.kill(self.child.pid, signal.SIGWINCH)
                self.wait_client_size(cols, rows)
                # Window layout changes only after kernel and cached client agree.
                target = self.window_target()
                tmux("set-window-option", "-t", target, "window-size", "manual")
                tmux("resize-window", "-t", target, "-x", str(cols), "-y", str(rows))
                tmux("refresh-client", "-t", self.tty)
                fields = self.window_geometry()
                if fields[:2] != (cols, rows) or winsize(0) != (cols, rows) or self.client_geometry() != (cols, rows):
                    raise BridgeError("Owned terminal geometry verification failed")
            except (OSError, ValueError, BridgeError):
                confirmed = False
                try:
                    fcntl.ioctl(0, termios.TIOCSWINSZ, original_size)
                    if self.child.poll() is not None:
                        raise BridgeError("Owned display client disconnected")
                    os.kill(self.child.pid, signal.SIGWINCH)
                    self.wait_client_size(old_cols, old_rows)
                    tmux("resize-window", "-t", self.window_target(), "-x", str(old_window[0]), "-y", str(old_window[1]))
                    tmux("refresh-client", "-t", self.tty)
                    confirmed = (winsize(0) == (old_cols, old_rows) == old_window[:2]
                                 and self.window_geometry()[:2] == old_window[:2]
                                 and self.client_geometry() == (old_cols, old_rows))
                except (OSError, ValueError, BridgeError):
                    pass
                failure = {"ok": False, "error": "Remote resize failed; verify display geometry before entering input",
                           "geometry_confirmed": confirmed, "rolled_back": confirmed}
                if confirmed:
                    failure.update({"cols": old_cols, "rows": old_rows})
                raise ResizeFailure(failure) from None
            self.cols, self.rows = cols, rows
            return {"cols": cols, "rows": rows, "client_cols": cols, "client_rows": rows,
                    "window_cols": fields[0], "window_rows": fields[1],
                    "pane_pid": fields[2], "pane_cols": fields[3], "pane_rows": fields[4],
                    "client_pid": self.child.pid}

    def serve(self):
        while not self.stop.is_set():
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(10)
                try:
                    if not hasattr(socket, "SO_PEERCRED"):
                        raise BridgeError("This host cannot verify local resize callers")
                    _, uid, _ = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                    if uid != os.geteuid():
                        raise BridgeError("Resize caller belongs to another user")
                    data = bytearray()
                    while b"\n" not in data:
                        chunk = connection.recv(min(1024, MAX_REQUEST + 1 - len(data)))
                        if not chunk:
                            raise BridgeError("Incomplete resize request")
                        data.extend(chunk)
                        if len(data) > MAX_REQUEST:
                            raise BridgeError("Resize request exceeded the limit")
                    request = json.loads(bytes(data).split(b"\n", 1)[0])
                    if not isinstance(request, dict) or set(request) != {"op", "session_id", "bridge_id", "cols", "rows"}:
                        raise BridgeError("Only the resize operation is allowed")
                    if request["op"] != "resize" or request["session_id"] != self.session_id or request["bridge_id"] != self.bridge_id:
                        raise BridgeError("Resize request does not match this display attachment")
                    result = {"ok": True, **self.resize(request["cols"], request["rows"])}
                except ResizeFailure as error:
                    result = error.result
                except (OSError, ValueError, TypeError, KeyError, BridgeError):
                    result = {"ok": False, "error": "Remote resize failed; the display geometry was not confirmed"}
                try:
                    connection.sendall(json.dumps(result).encode("utf-8") + b"\n")
                except OSError:
                    pass

    def run(self):
        apply_winsize(0, self.cols, self.rows)
        if self.require_existing:
            current = probe_existing_session(self.session_id)
            if not current.get("ok") or not current.get("exists"):
                raise BridgeError("Original tmux session is missing; replacement was not created")
            proof = current["identity"]
            if self.expected_identity is not None and self.expected_identity != proof:
                raise BridgeError("Original tmux identity changed; display was not attached")
            self.expected_identity = proof
            # Immutable tmux ID: a close/recreate of the same NAME cannot
            # redirect this display client to a different existing worker.
            self.attachment_target = proof["session"]
        else:
            self.attachment_target = "=" + self.session_id
        exists = subprocess.run(["tmux", "has-session", "-t", "=" + self.session_id], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5).returncode == 0
        if not exists:
            if self.require_existing:
                raise BridgeError("Original tmux session is missing; replacement was not created")
            tmux("new-session", "-d", "-s", self.session_id, "-c", self.cwd, "-x", str(self.cols), "-y", str(self.rows))
        # Existing sessions are attached as-is: no cwd change, resume command,
        # keystrokes, provider selection or process restart.
        tmux("set-option", "-t", self.attachment_target, "status", "off")
        tmux("set-option", "-t", self.attachment_target, "mouse", "off")
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.settimeout(0.2)
        old_umask = os.umask(0o077)
        try:
            self.listener.bind(str(self.path))
        finally:
            os.umask(old_umask)
        self.path.chmod(0o600)
        self.socket_inode = self.path.lstat().st_ino
        self.listener.listen(4)
        # Do not start a new session/process group and do not consume stdin.
        # tmux inherits the exact SSH PTY descriptors directly.
        # xterm/SSH pipes are UTF-8 even if the server's non-login environment
        # lacks LANG. Tell this display client explicitly, without changing the
        # shell locale, provider configuration or existing agent environment.
        self.child = subprocess.Popen(["tmux", "-u", "attach-session", "-t", self.attachment_target])
        if self.require_existing and probe_existing_session(self.session_id).get("identity") != self.expected_identity:
            raise BridgeError("Original tmux identity changed; display was not confirmed")
        self.resize(self.cols, self.rows)
        self.worker = threading.Thread(target=self.serve, name="ceo-pty-resize", daemon=True)
        self.worker.start()
        while not self.stop.wait(0.1):
            with self.lock:
                code = self.child.poll()
            if code is not None:
                return code
        return 0

    def cleanup(self):
        self.stop.set()
        if self.listener:
            self.listener.close()
        with self.lock:
            if self.child is not None and self.child.poll() is None:
                # Only our display client is ended. tmux keeps the pane alive.
                self.child.terminate()
                try:
                    self.child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.child.kill()
                    self.child.wait(timeout=3)
        if self.worker:
            self.worker.join(timeout=1)
        try:
            if self.socket_inode is not None and self.path.lstat().st_ino == self.socket_inode:
                self.path.unlink()
        except FileNotFoundError:
            pass


def request_resize(session_id, bridge_id, cols, rows):
    identity(session_id, bridge_id)
    size(cols, rows)
    deadline = time.monotonic() + 6
    while True:
        try:
            path = socket_path(session_id, bridge_id)
            check_socket(path)
            break
        except (FileNotFoundError, BridgeError) as error:
            if isinstance(error, BridgeError) and str(error) != "Remote resize bridge is not connected":
                raise
            if time.monotonic() >= deadline:
                raise BridgeError("Remote resize bridge is not connected") from None
            time.sleep(0.05)
    request = {"op": "resize", "session_id": session_id, "bridge_id": bridge_id, "cols": cols, "rows": rows}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(12)
        connection.connect(str(path))
        connection.sendall(json.dumps(request).encode("utf-8") + b"\n")
        data = bytearray()
        while b"\n" not in data:
            chunk = connection.recv(1024)
            if not chunk:
                raise BridgeError("Remote resize result was not confirmed")
            data.extend(chunk)
            if len(data) > MAX_REQUEST:
                raise BridgeError("Remote resize result exceeded the limit")
    result = json.loads(bytes(data).split(b"\n", 1)[0])
    if not isinstance(result, dict):
        raise BridgeError("Remote resize result was not confirmed")
    if result.get("ok") and (result.get("cols"), result.get("rows")) != (cols, rows):
        raise BridgeError("Remote resize failed; the display geometry was not confirmed")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("attach", "resize", "probe"))
    parser.add_argument("--session", required=True)
    parser.add_argument("--bridge-id")
    parser.add_argument("--cols", type=int)
    parser.add_argument("--rows", type=int)
    parser.add_argument("--cwd", default="")
    args = parser.parse_args()
    if args.mode == "probe":
        result = probe_existing_session(args.session)
        print(RESULT_MARKER + json.dumps(result), flush=True)
        return 0 if result.get("ok") else 1
    if args.mode == "resize":
        try:
            result = request_resize(args.session, args.bridge_id, args.cols, args.rows)
        except (OSError, ValueError, BridgeError):
            result = {"ok": False, "error": "Remote resize failed; the display geometry was not confirmed"}
        print(RESULT_MARKER + json.dumps(result), flush=True)
        return 0 if result["ok"] else 1
    attachment = Attachment(args.session, args.bridge_id, args.cols, args.rows, args.cwd)
    for signum in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: attachment.stop.set())
    try:
        return attachment.run()
    finally:
        attachment.cleanup()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, BridgeError, subprocess.TimeoutExpired):
        print("AgentCaptain display attachment failed; remote agent session was not closed", file=sys.stderr, flush=True)
        sys.exit(1)
