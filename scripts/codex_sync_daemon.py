#!/usr/bin/env python3
"""Conservative, current-user Codex daemon maintenance (Linux, stdlib only).

Run without arguments for a read-only check. ``--sync`` is the sole mutation:
``codex app-server daemon update --from-cli --yes``. No provider, auth, feature,
or config writes are performed. All loaded threads, including idle ones, block
sync. The loaded-thread probe is a snapshot, not an atomic server-side restart
lock: do not launch new clients while explicitly performing maintenance.

Protocol: https://learn.chatgpt.com/docs/app-server (WebSocket over Unix,
initialize -> initialized -> thread/loaded/list). Never reads thread history.
Unknown daemon status schemas and unsupported socket protocols fail closed.
The running/pid schema was verified on CLI 0.161.0. The stopped/pid schema is
conservatively supported but has not been verified on a live daemon.
Group checks reflect enumerable NSS users and do not prove hidden directory-service memberships; use only with an existing trusted CLI install.
"""
from __future__ import annotations

import argparse
import base64
import errno
import hashlib
import json
import os
import queue
import re
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path


MAX_OUTPUT = 64 * 1024
MAX_MESSAGE = 64 * 1024
MAX_HEADER = 8 * 1024
MAX_FRAMES = 64
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?\Z")
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class GuardError(RuntimeError):
    """Only fixed, safe error codes are displayed; never CLI/RPC error text."""


class Deadline:
    def __init__(self, timeout: float):
        if not 0 < timeout <= 60:
            raise GuardError("timeout_must_be_between_zero_and_60_seconds")
        self.end = time.monotonic() + timeout

    def remaining(self) -> float:
        remaining = self.end - time.monotonic()
        if remaining <= 0:
            raise GuardError("maintenance_timeout")
        return remaining


@dataclass(frozen=True)
class Identity:
    home: Path
    uid: int
    private_gids: frozenset[int] = frozenset()
    private_gid_refresh: object = field(default=None, repr=False, compare=False)


def verified_private_gids(uid: int, passwd_db, group_db) -> frozenset[int]:
    """Check private groups against explicit and enumerable primary-group users.

    Dependency-injected NSS readers keep tests platform-independent. Failed or
    inconsistent enumeration never authorizes group-write access. Empty groups
    are not evidence that the current owner belongs to a private user group.
    """
    try:
        users, groups = list(passwd_db.getpwall()), list(group_db.getgrall())
        by_name = {}
        for user in users:
            if (not isinstance(user.pw_name, str) or not user.pw_name
                    or user.pw_name in by_name
                    or type(user.pw_uid) is not int or user.pw_uid < 0
                    or type(user.pw_gid) is not int or user.pw_gid < 0):
                return frozenset()
            by_name[user.pw_name] = user
        if not any(user.pw_uid == uid for user in users):
            return frozenset()
        counts = {}
        for group in groups:
            if (not isinstance(group.gr_name, str) or not group.gr_name
                    or type(group.gr_gid) is not int or group.gr_gid < 0
                    or not isinstance(group.gr_mem, (list, tuple))
                    or any(not isinstance(name, str) or not name for name in group.gr_mem)):
                return frozenset()
            counts[group.gr_gid] = counts.get(group.gr_gid, 0) + 1
    except Exception:
        return frozenset()
    private = set()
    for group in groups:
        if counts[group.gr_gid] != 1:
            continue  # Ambiguous group aliases require a reviewed NSS schema.
        try:
            current = group_db.getgrgid(group.gr_gid)
            if (current.gr_gid != group.gr_gid or current.gr_name != group.gr_name
                    or set(current.gr_mem) != set(group.gr_mem)):
                continue
            members = {user.pw_uid for user in users if user.pw_gid == group.gr_gid}
            for name in group.gr_mem:
                explicit = passwd_db.getpwnam(name)
                enumerated = by_name.get(name)
                if (enumerated is None or explicit.pw_name != name
                        or explicit.pw_uid != enumerated.pw_uid or explicit.pw_gid != enumerated.pw_gid):
                    raise GuardError("private_group_nss_membership_unverified")
                members.add(explicit.pw_uid)
            if members == {uid}:
                private.add(group.gr_gid)
        except Exception:
            continue  # Unknown/unresolvable NSS members never authorize access.
    return frozenset(private)


def current_identity() -> Identity:
    # SO_PEERCRED is deliberately required: no unverifiable fallback on Windows
    # or platforms with different peer-credential semantics.
    if not sys.platform.startswith("linux") or not hasattr(socket, "SO_PEERCRED"):
        raise GuardError("linux_same_uid_socket_support_required")
    import grp
    import pwd
    uid = os.getuid()
    if uid != os.geteuid():
        raise GuardError("real_and_effective_uid_differ")
    refresh = lambda: verified_private_gids(uid, pwd, grp)
    return Identity(Path(pwd.getpwuid(uid).pw_dir).resolve(strict=True), uid,
                    refresh(), refresh)


def fingerprint(info) -> tuple:
    return (info.st_dev, info.st_ino, info.st_uid, info.st_gid, info.st_mode,
            info.st_size, info.st_mtime_ns)


def check_owner_write_mode(info, identity: Identity, *, parent=False):
    prefix = "path_parent" if parent else "path"
    if info.st_uid != identity.uid:
        raise GuardError(prefix + "_owner_not_current_user")
    if info.st_mode & 0o002:
        raise GuardError(prefix + "_world_writable")
    if info.st_mode & 0o020 and info.st_gid not in identity.private_gids:
        raise GuardError(prefix + "_group_write_not_private_or_unverified")


def check_private_group_acl(path: Path, info):
    # A private owning group does not prove that named POSIX ACL users/groups
    # cannot write. Refuse any ACL rather than interpreting or modifying it.
    if not info.st_mode & 0o020:
        return
    if not hasattr(os, "getxattr"):
        raise GuardError("group_writable_path_acl_support_unverified")
    attributes = ["system.posix_acl_access"]
    if stat.S_ISDIR(info.st_mode):
        attributes.append("system.posix_acl_default")
    for attribute in attributes:
        try:
            os.getxattr(path, attribute)
        except OSError as error:
            if error.errno == errno.ENODATA:
                continue
            raise GuardError("group_writable_path_acl_read_unverified") from None
        raise GuardError("group_writable_path_has_posix_acl")


def check_home_parents(paths, identity: Identity):
    for parent in {parent for path in paths for parent in path.parents}:
        if parent == identity.home or identity.home in parent.parents:
            if not parent.resolve(strict=True).is_relative_to(identity.home):
                raise GuardError("path_parent_resolves_outside_current_user_home")
            info = parent.stat()
            check_owner_write_mode(info, identity, parent=True)
            check_private_group_acl(parent, info)


def checked_default_runtime_socket(path: Path, identity: Identity) -> Path:
    """Only Codex's precise isolated default runtime, never arbitrary /tmp."""
    temp_root = Path("/tmp").absolute()
    runtime = temp_root / f"codex-daemon-{identity.uid}"
    if path.parent != runtime or not re.fullmatch(r"[a-f0-9]{64}", path.name):
        raise GuardError("control_socket_outside_allowed_default_runtime")
    # lstat rejects symlink directories even when their resolved owners match.
    temp_info = temp_root.lstat()
    if (not stat.S_ISDIR(temp_info.st_mode) or temp_info.st_uid != 0
            or stat.S_IMODE(temp_info.st_mode) != 0o1777):
        raise GuardError("default_runtime_tmp_not_root_owned_sticky_directory")
    runtime_info = runtime.lstat()
    if (not stat.S_ISDIR(runtime_info.st_mode) or runtime_info.st_uid != identity.uid
            or stat.S_IMODE(runtime_info.st_mode) != 0o700):
        raise GuardError("default_runtime_directory_owner_type_or_mode_unsafe")
    info = path.lstat()
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != identity.uid
            or info.st_mode & 0o077):
        raise GuardError("control_socket_type_owner_or_mode_unsafe")
    return path


def checked_path(path: Path, identity: Identity, *, executable=False, control=False) -> Path:
    path = path.expanduser().absolute()
    home = identity.home
    try:
        resolved = path.resolve(strict=True)
        if control and not resolved.is_relative_to(home):
            if path.is_relative_to(home):
                if path != home / ".codex/app-server-control/app-server-control.sock":
                    raise GuardError("external_control_socket_link_not_default_entry")
                check_home_parents([path], identity)
                link = path.lstat()
                if not stat.S_ISLNK(link.st_mode) or link.st_uid != identity.uid:
                    raise GuardError("default_control_socket_link_not_owned_by_current_user")
                proof = fingerprint(link)
                target = Path(os.readlink(path))
                if not target.is_absolute() or target != resolved or ".." in target.parts:
                    raise GuardError("default_control_socket_link_target_not_canonical")
            elif path != resolved:
                raise GuardError("external_control_socket_alias_not_allowed")
            result = checked_default_runtime_socket(resolved, identity)
            if path.is_relative_to(home) and fingerprint(path.lstat()) != proof:
                raise GuardError("default_control_socket_link_changed_during_validation")
            return result
        path.relative_to(home)
        resolved.relative_to(home)
        info = resolved.stat()
        check_owner_write_mode(info, identity)
        check_private_group_acl(resolved, info)
        check_home_parents([path, resolved], identity)
        if control:
            if path.is_symlink() or not stat.S_ISSOCK(info.st_mode) or info.st_mode & 0o077:
                raise GuardError("control_socket_type_or_mode_unsafe")
        elif not stat.S_ISREG(info.st_mode) or executable and not info.st_mode & 0o111:
            raise GuardError("cli_not_an_executable_regular_file")
        return resolved
    except (OSError, ValueError):
        raise GuardError("path_missing_or_outside_current_user_home") from None


class Cli:
    def __init__(self, entry: Path, identity: Identity):
        self.entry, self.identity = entry.expanduser().absolute(), identity
        self.resolved = checked_path(self.entry, identity, executable=True)
        self.proof = fingerprint(self.resolved.stat())

    def verify(self):
        if (checked_path(self.entry, self.identity, executable=True) != self.resolved
                or fingerprint(self.resolved.stat()) != self.proof):
            raise GuardError("cli_entry_changed_during_maintenance")

    def run(self, arguments: list[str], deadline: Deadline) -> str:
        self.verify()
        # A bounded reader avoids communicate() buffering unlimited child output.
        # Only this explicitly spawned CLI child is killed on timeout/failure.
        child = subprocess.Popen([str(self.resolved), *arguments], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        output = queue.Queue(maxsize=1)

        def read():
            data = bytearray()
            try:
                while True:
                    packet = child.stdout.read(min(4096, MAX_OUTPUT + 1 - len(data)))
                    if not packet:
                        break
                    data.extend(packet)
                    if len(data) > MAX_OUTPUT:
                        raise GuardError("cli_output_limit_exceeded")
                output.put(bytes(data))
            except Exception:
                output.put(GuardError("cli_output_read_failed_or_limit_exceeded"))

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        try:
            try:
                data = output.get(timeout=deadline.remaining())
            except queue.Empty:
                raise GuardError("cli_command_timeout") from None
            if isinstance(data, Exception):
                raise data
            try:
                code = child.wait(timeout=deadline.remaining())
            except subprocess.TimeoutExpired:
                raise GuardError("cli_command_timeout") from None
            if code != 0:
                raise GuardError("cli_command_failed")
            try:
                return data.decode("utf-8", errors="strict")
            except UnicodeError:
                raise GuardError("cli_output_not_utf8") from None
        finally:
            if child.poll() is None:
                child.kill()
                try:
                    child.wait(timeout=min(1, max(0, deadline.end - time.monotonic())))
                except subprocess.TimeoutExpired:
                    pass
            child.stdout.close()


def version_string(value) -> str:
    if not isinstance(value, str) or len(value) > 80 or not VERSION.fullmatch(value):
        raise GuardError("invalid_version_field")
    return value


def read_status(cli: Cli, deadline: Deadline) -> dict:
    try:
        raw = json.loads(cli.run(["app-server", "daemon", "version"], deadline))
    except (ValueError, TypeError):
        raise GuardError("daemon_version_not_json") from None
    if not isinstance(raw, dict) or raw.get("status") not in {"running", "stopped"}:
        raise GuardError("unrecognized_daemon_status")
    # These are the CLI's actual daemon-version field names, not process guesses.
    result = {"status": raw["status"], "backend": raw.get("backend"),
              "cliVersion": version_string(raw.get("cliVersion")),
              "managedCodexVersion": version_string(raw.get("managedCodexVersion"))}
    if result["backend"] != "pid":
        raise GuardError("unrecognized_daemon_backend")
    managed = raw.get("managedCodexPath")
    if not isinstance(managed, str) or not managed or "\x00" in managed or not Path(managed).is_absolute():
        raise GuardError("invalid_managed_cli_path")
    result["managedCodexPath"] = str(checked_path(Path(managed), cli.identity, executable=True))
    if result["status"] == "running":
        result["appServerVersion"] = version_string(raw.get("appServerVersion"))
        control = raw.get("socketPath")
        if not isinstance(control, str) or not control or "\x00" in control or not Path(control).is_absolute():
            raise GuardError("invalid_control_socket_path")
        result["socketPath"] = str(checked_path(Path(control), cli.identity, control=True))
    else:
        if raw.get("appServerVersion") is not None:
            raise GuardError("stopped_status_has_running_version")
        result["appServerVersion"] = None
    return result


def feature_rows(text: str) -> dict[str, bool]:
    rows = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r"\s*([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*)\s+[A-Za-z][A-Za-z ]*\s+(true|false)\s*", line)
        if not match or len(match[1]) > 80 or match[1] in rows or len(rows) >= 512:
            raise GuardError("unrecognized_feature_list_output")
        rows[match[1]] = match[2] == "true"
    if not rows:
        raise GuardError("empty_feature_list_output")
    return rows


class UnixRpc:
    """Tiny bounded RFC6455 client; unsupported frames fail closed."""
    def __init__(self, path: Path, identity: Identity, deadline: Deadline):
        self.deadline = deadline
        self.identity = identity
        self.path = checked_path(path, identity, control=True)
        self.proof = fingerprint(self.path.stat())
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.sock.settimeout(deadline.remaining())
            self.sock.connect(str(self.path))
            pid, uid, _gid = struct.unpack("3i", self.sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if pid <= 0 or uid != identity.uid:
                raise GuardError("control_socket_peer_identity_unsafe")
            self.peer_pid = pid
            if (checked_path(path, identity, control=True) != self.path
                    or fingerprint(self.path.stat()) != self.proof):
                raise GuardError("control_socket_changed_during_connect")
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            self._send(("GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                        "Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
                        f"Sec-WebSocket-Key: {key}\r\n\r\n").encode("ascii"))
            header = bytearray()
            while not header.endswith(b"\r\n\r\n"):
                if len(header) >= MAX_HEADER:
                    raise GuardError("websocket_upgrade_header_too_large")
                header.extend(self._exact(1))
            lines = header.decode("ascii", errors="strict").split("\r\n")
            if not re.fullmatch(r"HTTP/1\.1 101(?: .*)?", lines[0]):
                raise GuardError("websocket_upgrade_rejected")
            headers = {}
            for line in lines[1:-2]:
                name, separator, value = line.partition(":")
                name = name.strip().lower()
                if not separator or not name or name in headers:
                    raise GuardError("invalid_websocket_upgrade_headers")
                headers[name] = value.strip()
            expected = base64.b64encode(hashlib.sha1((key + WS_GUID).encode("ascii")).digest()).decode("ascii")
            if (headers.get("sec-websocket-accept") != expected
                    or headers.get("upgrade", "").lower() != "websocket"
                    or "upgrade" not in {x.strip().lower() for x in headers.get("connection", "").split(",")}
                    or "sec-websocket-extensions" in headers or "sec-websocket-protocol" in headers):
                raise GuardError("websocket_upgrade_validation_failed")
        except Exception:
            self.close()
            raise

    def _send(self, data: bytes):
        self.sock.settimeout(self.deadline.remaining())
        self.sock.sendall(data)

    def _exact(self, length: int) -> bytes:
        data = bytearray()
        while len(data) < length:
            self.sock.settimeout(self.deadline.remaining())
            packet = self.sock.recv(length - len(data))
            if not packet:
                raise GuardError("control_socket_closed")
            data.extend(packet)
        return bytes(data)

    def frame(self, payload: bytes, opcode=1):
        if len(payload) > MAX_MESSAGE:
            raise GuardError("websocket_message_too_large")
        mask = os.urandom(4)
        length = len(payload)
        if length < 126:
            size = bytes([0x80 | length])
        elif length < 65536:
            size = b"\xfe" + struct.pack("!H", length)
        else:
            size = b"\xff" + struct.pack("!Q", length)
        self._send(bytes([0x80 | opcode]) + size + mask
                   + bytes(value ^ mask[i % 4] for i, value in enumerate(payload)))

    def message(self) -> dict:
        # Fragmentation is intentionally unsupported rather than guessing partial
        # JSON. Ping/pong is handled; close/binary/extensions abort this probe.
        for _ in range(MAX_FRAMES):
            first, second = self._exact(2)
            opcode, final = first & 15, bool(first & 128)
            if first & 0x70 or second & 0x80 or not final:
                raise GuardError("unsupported_or_masked_server_frame")
            length = second & 127
            if length == 126:
                length = struct.unpack("!H", self._exact(2))[0]
                if length < 126:
                    raise GuardError("noncanonical_frame_length")
            elif length == 127:
                length = struct.unpack("!Q", self._exact(8))[0]
                if length < 65536:
                    raise GuardError("noncanonical_frame_length")
            if length > MAX_MESSAGE or opcode >= 8 and length > 125:
                raise GuardError("websocket_message_too_large")
            if opcode not in {1, 8, 9, 10}:
                raise GuardError("unsupported_websocket_opcode")
            payload = self._exact(length)
            if opcode == 8:
                raise GuardError("server_closed_websocket")
            if opcode == 9:
                self.frame(payload, opcode=10)
                continue
            if opcode == 10:
                continue
            try:
                message = json.loads(payload.decode("utf-8", errors="strict"))
            except (UnicodeError, ValueError):
                raise GuardError("invalid_rpc_json") from None
            if not isinstance(message, dict):
                raise GuardError("invalid_rpc_envelope")
            return message
        raise GuardError("too_many_websocket_control_frames")

    def call(self, request_id: int, method: str, params: dict) -> dict:
        self.notify(method, params, request_id=request_id)
        # 0.161 emits this read-only status notification after initialization.
        # Discard only that known envelope, not thread/turn data or approvals.
        for _ in range(MAX_FRAMES):
            message = self.message()
            if ("id" not in message and message.get("method") == "remoteControl/status/changed"
                    and isinstance(message.get("params"), dict)
                    and "result" not in message and "error" not in message):
                continue
            if message.get("id") != request_id or "method" in message or "error" in message:
                raise GuardError("unexpected_or_failed_rpc_response")
            result = message.get("result")
            if not isinstance(result, dict):
                raise GuardError("invalid_rpc_result")
            return result
        raise GuardError("too_many_safe_status_notifications")

    def notify(self, method: str, params: dict, *, request_id=None):
        message = {"method": method, "params": params}
        if request_id is not None:
            message["id"] = request_id
        self.frame(json.dumps(message, separators=(",", ":")).encode("utf-8"))

    def close(self):
        self.sock.close()

    def verify(self):
        if (checked_path(self.path, self.identity, control=True) != self.path
                or fingerprint(self.path.stat()) != self.proof):
            raise GuardError("control_socket_changed_during_maintenance")
        pid, uid, _gid = struct.unpack("3i", self.sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if pid != self.peer_pid or uid != self.identity.uid:
            raise GuardError("control_socket_peer_identity_changed")


def loaded_threads(rpc: UnixRpc, request_id=2) -> int:
    result = rpc.call(request_id, "thread/loaded/list", {})
    # 0.161 supplies nextCursor=null for a complete page; unfinished or unknown
    # schemas still refuse maintenance, without scanning further pages.
    if ("data" not in result or not set(result).issubset({"data", "nextCursor"})
            or result.get("nextCursor") is not None or not isinstance(result["data"], list)):
        raise GuardError("invalid_loaded_thread_list")
    if any(not isinstance(value, str) or not value or len(value) > 200 for value in result["data"]):
        raise GuardError("invalid_loaded_thread_id")
    return len(result["data"])


def maintain(cli: Cli, *, sync=False, timeout=30, rpc_factory=UnixRpc) -> dict:
    deadline = Deadline(timeout)
    before = read_status(cli, deadline)
    actual = cli.run(["--version"], deadline).strip()
    match = re.fullmatch(r"codex(?:-cli)?\s+(\S+)", actual)
    if not match or version_string(match[1]) != before["cliVersion"]:
        raise GuardError("cli_version_disagrees_with_daemon_status")
    managed = Cli(Path(before["managedCodexPath"]), cli.identity)
    cli_features = feature_rows(cli.run(["features", "list"], deadline))
    managed_features = feature_rows(managed.run(["features", "list"], deadline))
    report = {**before, "cliPath": str(cli.resolved), "mode": "sync" if sync else "check",
              "cliFeatures": cli_features, "managedFeatures": managed_features,
              "runningFeatureMode": "not_reported_by_daemon_version",
              "loadedThreadCount": 0, "updateAttempted": False}
    rpc = None
    try:
        if before["status"] == "running":
            rpc = rpc_factory(Path(before["socketPath"]), cli.identity, deadline)
            initialized = rpc.call(1, "initialize", {"clientInfo": {
                "name": "codex_sync_daemon", "title": "Codex daemon maintenance", "version": "1.0.0"}})
            user_agent = initialized.get("userAgent")
            if (not isinstance(user_agent, str) or not re.match(
                    r"codex-tui/" + re.escape(before["appServerVersion"]) + r"(?:\s|$)", user_agent)):
                raise GuardError("daemon_rpc_version_identity_mismatch")
            rpc.notify("initialized", {})
            report["loadedThreadCount"] = loaded_threads(rpc)
        compatible = (before["managedCodexVersion"] == before["cliVersion"]
                      and (before["status"] != "running" or before["appServerVersion"] == before["cliVersion"])
                      and cli_features == managed_features)
        report["cliAndManagedDefaultsMatch"] = cli_features == managed_features
        report["versionsMatch"] = (before["managedCodexVersion"] == before["cliVersion"]
                                   and before["appServerVersion"] == before["cliVersion"])
        if report["loadedThreadCount"]:
            report.update(action="blocked_loaded_threads", ok=not sync)
            return report
        if not sync:
            report.update(action="checked", ok=True, syncNeeded=not compatible)
            return report
        if compatible:
            report.update(action="already_synchronized", ok=True)
            return report
        # Recheck status and empty loaded-thread set immediately before update.
        # A later new connection remains a server-side TOCTOU limitation.
        if cli.identity.private_gid_refresh is not None:
            if cli.identity.private_gid_refresh() != cli.identity.private_gids:
                raise GuardError("private_group_membership_changed_before_update")
        if read_status(cli, deadline) != before:
            raise GuardError("daemon_status_changed_before_update")
        if rpc is not None:
            rpc.verify()
            if loaded_threads(rpc, request_id=3):
                raise GuardError("loaded_threads_appeared_before_update")
            rpc.verify()
        cli.verify()
        managed.verify()
        deadline.remaining()
        report["updateAttempted"] = True
        try:
            cli.run(["app-server", "daemon", "update", "--from-cli", "--yes"], deadline)
            after = read_status(cli, deadline)
            if (after["cliVersion"] != before["cliVersion"]
                    or after["managedCodexVersion"] != before["cliVersion"]
                    or after["status"] != before["status"]
                    or after["status"] == "running" and after["appServerVersion"] != before["cliVersion"]):
                raise GuardError("post_update_versions_or_status_do_not_match")
            updated = Cli(Path(after["managedCodexPath"]), cli.identity)
            if feature_rows(updated.run(["features", "list"], deadline)) != cli_features:
                raise GuardError("post_update_cli_managed_features_do_not_match")
            report.update(after=after, action="synchronized", ok=True)
            return report
        except Exception:
            # The one mutation may have completed even if its response was lost.
            report.update(action="update_unverified_do_not_retry", ok=False)
            return report
    finally:
        if rpc is not None:
            rpc.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true", help="read-only inspection (default)")
    modes.add_argument("--sync", action="store_true", help="explicitly update only after an empty loaded-thread probe")
    parser.add_argument("--cli", type=Path, help="current-user home CLI entry (default: ~/.local/bin/codex)")
    parser.add_argument("--timeout", type=float, default=30, help="total operation timeout, at most 60 seconds")
    args = parser.parse_args(argv)
    try:
        deadline = Deadline(args.timeout)  # Validate before opening any child/socket.
        identity = current_identity()
        cli = Cli(args.cli or identity.home / ".local/bin/codex", identity)
        report = maintain(cli, sync=args.sync, timeout=deadline.remaining())
    except GuardError as error:
        report = {"ok": False, "action": "refused", "error": str(error),
                  "updateAttempted": False}
    except (OSError, TimeoutError):
        report = {"ok": False, "action": "refused", "error": "read_only_preflight_failed",
                  "updateAttempted": False}
    except Exception:
        report = {"ok": False, "action": "refused", "error": "unexpected_preflight_failure",
                  "updateAttempted": False}
    print(json.dumps(report, ensure_ascii=True, sort_keys=True))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
