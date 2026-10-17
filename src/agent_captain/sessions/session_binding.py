"""Exact, read-only binding of one shared terminal to a Codex rollout.

This file is intentionally stdlib-only and can execute over SSH stdin. It
never searches conversation contents or chooses the newest thread. Linux
probes only the chosen owned tmux pane's bounded descendants and the native
foreground Codex's held files/explicit resume selector. Windows probes the
chosen terminal root's descendants and their per-process file handles, not
the system-wide handle table. Unknown or ambiguous evidence fails closed.
"""
from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import uuid
from contextlib import closing
from pathlib import Path


SESSION = re.compile(r"ceohub-[a-f0-9]{16,48}\Z")
MAX_PROCESSES = 256
MAX_DEPTH = 16
MAX_HEADER = 256 * 1024
MAX_HANDLES = 32768
SHELLS = {"bash", "sh", "zsh", "fish", "dash", "pwsh", "powershell", "cmd"}


class BindingError(RuntimeError):
    pass


def _unbound(reason, **evidence):
    return {"state": "unbound", "reason": reason, **evidence}


def _session_uuid(value):
    if not isinstance(value, str):
        raise BindingError("invalid_session_id")
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        raise BindingError("invalid_session_id") from None
    if str(parsed) != value.lower():
        raise BindingError("invalid_session_id")
    return str(parsed)


def _read_limited(path, limit):
    with Path(path).open("rb") as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise BindingError("process_metadata_limit")
    return value


def _rollout_metadata(path, home, expected_id=None, expected_cwd=None):
    """Validate only the first session header; never consume other threads."""
    home = Path(home).resolve(strict=True)
    path = Path(path).resolve(strict=True)
    roots = [home / "sessions", home / "archived_sessions"]
    if path.suffix != ".jsonl" or not any(path.is_relative_to(root) for root in roots):
        raise BindingError("rollout_outside_codex_home")
    before = path.stat()
    if not stat.S_ISREG(before.st_mode):
        raise BindingError("invalid_rollout_file")
    with path.open("rb") as stream:
        header = stream.readline(MAX_HEADER + 1)
        opened = os.fstat(stream.fileno())
    after = path.stat()
    identity = lambda item: (item.st_dev, item.st_ino)
    if identity(before) != identity(opened) or identity(opened) != identity(after):
        raise BindingError("rollout_replaced")
    if len(header) > MAX_HEADER or not header.endswith(b"\n"):
        raise BindingError("invalid_session_header")
    try:
        event = json.loads(header)
    except (UnicodeError, ValueError):
        raise BindingError("invalid_session_header") from None
    payload = event.get("payload") if isinstance(event, dict) else None
    if not isinstance(event, dict) or event.get("type") != "session_meta" or not isinstance(payload, dict):
        raise BindingError("invalid_session_header")
    session_id = _session_uuid(payload.get("id"))
    if expected_id is not None and session_id != _session_uuid(expected_id):
        raise BindingError("session_id_mismatch")
    # Subagent logs held by the parent process must not become CEO inventory.
    if payload.get("source") != "cli" or payload.get("parent_thread_id"):
        raise BindingError("unsupported_session_source")
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not Path(cwd).is_absolute():
        raise BindingError("invalid_session_cwd")
    if expected_cwd is not None and Path(cwd).resolve() != Path(expected_cwd).resolve():
        raise BindingError("session_cwd_mismatch")
    return {"session_id": session_id, "rollout_path": str(path), "codex_home": str(home),
            "cwd": cwd, "source": "cli", "rollout_identity": {"device": before.st_dev, "inode": before.st_ino}}


def resolve_session(session_id, codex_home):
    """Resolve an explicitly selected UUID by exact indexed ID, never by date/cwd."""
    try:
        session_id = _session_uuid(session_id)
        home = Path(codex_home).resolve(strict=True)
        databases = [path for path in home.iterdir() if re.fullmatch(r"state_[0-9]+\.sqlite", path.name) and path.is_file()]
        if len(databases) > 16:
            raise BindingError("session_index_ambiguous")
        matches = []
        for database in databases:
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=2)) as connection:
                connection.execute("PRAGMA query_only=ON")
                columns = {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
                if not {"id", "rollout_path", "cwd"} <= columns:
                    continue
                rows = connection.execute("SELECT id, rollout_path, cwd FROM threads WHERE id=? LIMIT 2", (session_id,)).fetchall()
                if len(rows) > 1:
                    raise BindingError("session_index_ambiguous")
                for identifier, path, cwd in rows:
                    matches.append(_rollout_metadata(path, home, identifier, cwd))
        unique = {(item["session_id"], item["rollout_path"]): item for item in matches}
        if len(unique) != 1:
            raise BindingError("session_not_indexed" if not unique else "session_index_ambiguous")
        return {"state": "resolved", **next(iter(unique.values()))}
    except BindingError as exc:
        return _unbound(str(exc))
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return _unbound("session_index_unavailable")


class LinuxProc:
    def __init__(self, root="/proc"):
        self.root = Path(root)

    def identity(self, pid):
        raw = _read_limited(self.root / str(pid) / "stat", 8192).decode("utf-8", "strict")
        prefix, separator, suffix = raw.rpartition(")")
        fields = suffix.split()
        if not separator or int(prefix.split(" ", 1)[0]) != pid or len(fields) < 20:
            raise BindingError("process_changed")
        return {"pid": pid, "ppid": int(fields[1]), "pgid": int(fields[2]), "tty_nr": int(fields[4]),
                "tpgid": int(fields[5]), "start_ticks": int(fields[19]), "uid": (self.root / str(pid)).stat().st_uid}

    def children(self, pid):
        return [int(item) for item in _read_limited(self.root / str(pid) / "task" / str(pid) / "children", 8192).split()]

    def executable(self, pid):
        return os.readlink(self.root / str(pid) / "exe").removesuffix(" (deleted)")

    def cwd(self, pid):
        return os.readlink(self.root / str(pid) / "cwd")

    def argv(self, pid):
        # Only whitelisted selectors are exported; the rest remains private.
        return _read_limited(self.root / str(pid) / "cmdline", 65536).decode("utf-8", "strict").rstrip("\0").split("\0")

    def codex_home(self, pid, uid):
        # Do not decode, log or return any other environment key.
        keys = []
        for entry in _read_limited(self.root / str(pid) / "environ", 1024 * 1024).split(b"\0"):
            if entry.startswith(b"CODEX_HOME="):
                keys.append(entry[len(b"CODEX_HOME="):].decode("utf-8", "strict"))
        if len(keys) > 1 or (keys and not Path(keys[0]).is_absolute()):
            raise BindingError("codex_home_unresolved")
        if keys:
            return str(Path(keys[0]).resolve())
        import pwd
        return str((Path(pwd.getpwuid(uid).pw_dir) / ".codex").resolve())

    def held_rollouts(self, pid, home):
        paths = set()
        descriptors = list((self.root / str(pid) / "fd").iterdir())
        if len(descriptors) > MAX_HANDLES:
            raise BindingError("process_handle_limit")
        for descriptor in descriptors:
            if not descriptor.name.isdecimal():
                continue
            try:
                value = os.readlink(descriptor)
            except FileNotFoundError:
                continue  # A non-rollout descriptor may legitimately close.
            if value.endswith(".jsonl") and Path(value).is_absolute():
                candidate = Path(value).resolve()
                if any(candidate.is_relative_to(Path(home) / subdir) for subdir in ("sessions", "archived_sessions")):
                    held, named = descriptor.stat(), candidate.stat()
                    if (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino):
                        raise BindingError("session_evidence_changed")
                    paths.add(str(candidate))
        return sorted(paths)

    def stdin_tty(self, pid, tty_nr):
        info = (self.root / str(pid) / "fd" / "0").stat()
        major, minor = os.major(info.st_rdev), os.minor(info.st_rdev)
        device = (minor & 0xff) | (major << 8) | ((minor & ~0xff) << 12)
        return bool(info.st_rdev) and device == (tty_nr & 0xffffffff)


def _pane_metadata(session):
    if not SESSION.fullmatch(session):
        raise BindingError("invalid_terminal_id")
    result = subprocess.run(["tmux", "list-panes", "-t", "=" + session, "-F",
                             "#{session_name}\t#{pane_id}\t#{pane_pid}\t#{pane_tty}\t#{pane_current_command}"],
                            stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="strict", timeout=5)
    rows = result.stdout.splitlines()
    if result.returncode or len(rows) != 1 or len(result.stdout) > 2048:
        raise BindingError("terminal_pane_unresolved")
    values = rows[0].split("\t")
    if len(values) != 5 or values[0] != session or not re.fullmatch(r"%[0-9]+", values[1]) or not re.fullmatch(r"/dev/(?:pts/[0-9]+|tty[A-Za-z0-9]*)", values[3]):
        raise BindingError("terminal_pane_unresolved")
    return {"session": session, "pane_id": values[1], "pid": int(values[2]), "tty": values[3], "command": values[4]}


def _resume_selector(argv):
    """Accept an explicit UUID/path in a known CLI grammar, not prompt strings."""
    args = list(argv[1:])
    position = 0
    value_options = {"-C", "--cd", "-c", "--config", "-m", "--model", "-p", "--profile", "-a", "--ask-for-approval", "-s", "--sandbox", "--enable", "--disable", "--image", "-i"}
    flags = {"--full-auto", "--dangerously-bypass-approvals-and-sandbox", "--no-alt-screen", "--search", "--oss"}
    while position < len(args):
        value = args[position]
        if value == "resume":
            position += 1
            break
        if value in value_options and position + 1 < len(args):
            position += 2
        elif value in flags:
            position += 1
        elif any(value.startswith(option + "=") for option in value_options):
            position += 1
        else:
            return None
    else:
        return None
    if position >= len(args):
        return None
    selector = args[position]
    try:
        return {"kind": "uuid", "value": _session_uuid(selector)}
    except BindingError:
        if Path(selector).is_absolute() and selector.endswith(".jsonl"):
            return {"kind": "path", "value": selector}
    return None


def _candidate_binding(paths, home, cwd, selector=None):
    candidates = []
    held = set()
    for path in paths:
        try:
            candidate = _rollout_metadata(path, home, expected_cwd=cwd)
            candidates.append(candidate)
            held.add((candidate["session_id"], candidate["rollout_path"]))
        except BindingError as exc:
            if str(exc) == "unsupported_session_source":
                continue
            raise
    # argv records the INITIAL resume selector and is unchanged by in-TUI
    # /new or /resume. A unique validated held main rollout is current evidence
    # and takes precedence; never merge stale argv into live candidates.
    if selector and not candidates:
        if selector["kind"] == "uuid":
            resolved = resolve_session(selector["value"], home)
            if resolved["state"] == "resolved":
                candidates.append(_rollout_metadata(resolved["rollout_path"], home, selector["value"], cwd))
        else:
            candidates.append(_rollout_metadata(selector["value"], home, expected_cwd=cwd))
    unique = {(item["session_id"], item["rollout_path"]): item for item in candidates}
    if len(unique) != 1:
        return _unbound("no_exact_session_evidence" if not unique else "ambiguous_session_evidence")
    chosen_key, chosen = next(iter(unique.items()))
    return {"state": "bound", "binding_evidence": "held_rollout" if chosen_key in held else "explicit_resume", **chosen}


def bind_linux(terminal_id, proc_root="/proc"):
    evidence = {}
    try:
        first = _pane_metadata(terminal_id)
        reader = LinuxProc(proc_root)
        pane = reader.identity(first["pid"])
        uid = os.geteuid()
        if pane["uid"] != uid or not pane["tty_nr"] or pane["tpgid"] <= 0:
            raise BindingError("foreground_identity_unresolved")
        if first["command"] in SHELLS:
            return _unbound("foreground_shell")
        pending, seen, records = [(pane["pid"], 0)], set(), {}
        while pending:
            pid, depth = pending.pop()
            if pid in seen:
                continue
            seen.add(pid)
            if len(seen) > MAX_PROCESSES or depth > MAX_DEPTH:
                raise BindingError("process_tree_limit")
            row = reader.identity(pid)
            if row["uid"] != uid or row["tty_nr"] != pane["tty_nr"]:
                continue
            records[pid] = {**row, "exe": reader.executable(pid)}
            pending.extend((child, depth + 1) for child in reader.children(pid))
        group = pane["tpgid"]
        leader = records.get(group)
        if not leader or leader["pgid"] != group or leader["tpgid"] != group:
            raise BindingError("foreground_identity_unresolved")
        native = [row for row in records.values() if Path(row["exe"]).name == "codex" and row["pgid"] == group and row["tpgid"] == group and reader.stdin_tty(row["pid"], pane["tty_nr"])]
        if len(native) != 1:
            raise BindingError("foreground_codex_ambiguous" if native else "foreground_not_codex")
        agent = native[0]
        home = reader.codex_home(agent["pid"], uid)
        cwd = reader.cwd(agent["pid"])
        identity = {"platform": "linux", "terminal_id": terminal_id, "pane_id": first["pane_id"], "pane_pid": pane["pid"],
                    "pane_start_ticks": pane["start_ticks"], "pid": agent["pid"], "start_ticks": agent["start_ticks"],
                    "pgid": group, "tty_nr": pane["tty_nr"], "tty": first["tty"], "uid": uid}
        evidence = {"process_identity": identity, "codex_home": home, "process_cwd": cwd}
        argv = reader.argv(agent["pid"])
        selector = _resume_selector(argv)
        paths = reader.held_rollouts(agent["pid"], home)
        result = _candidate_binding(paths, home, cwd, selector)
        # Revalidate the complete selected agent ancestry, pane, and metadata.
        current = agent["pid"]
        checked = set()
        while current != pane["pid"]:
            if current in checked or current not in records:
                raise BindingError("process_changed")
            checked.add(current)
            row = records[current]
            expected = {key: value for key, value in row.items() if key != "exe"}
            if reader.identity(current) != expected or reader.executable(current) != row["exe"]:
                raise BindingError("process_changed")
            current = row["ppid"]
        if (_pane_metadata(terminal_id) != first or reader.identity(pane["pid"]) != pane
                or reader.cwd(agent["pid"]) != cwd or reader.codex_home(agent["pid"], uid) != home
                or reader.argv(agent["pid"]) != argv):
            raise BindingError("process_changed")
        if paths and reader.held_rollouts(agent["pid"], home) != paths:
            raise BindingError("session_evidence_changed")
        return {**result, **evidence}
    except BindingError as exc:
        # A raced process identity must never be offered for manual association.
        return _unbound(str(exc), **({} if str(exc) in {"process_changed", "session_evidence_changed"} else evidence))
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired):
        return _unbound("process_metadata_unavailable")


def bind_windows(root_pid):
    """Windows implementation below uses only selected-process handles."""
    if os.name != "nt":
        return _unbound("windows_unavailable")
    return _bind_windows_native(root_pid)


def _bind_windows_native(root_pid):
    if type(root_pid) is not int or root_pid <= 0:
        return _unbound("invalid_root_pid")
    evidence = {}
    try:
        reader = WindowsProc()
        root = reader.identity(root_pid)
        descendants = reader.descendants(root_pid)
        native = []
        for pid, parent in descendants:
            row = reader.identity(pid)
            if row["owner"] != root["owner"] or row["session"] != root["session"]:
                continue
            if Path(row["exe"]).name.lower() == "codex.exe":
                native.append(row)
        if len(native) != 1:
            raise BindingError("selected_codex_ambiguous" if native else "selected_not_codex")
        agent = native[0]
        if agent["owner"] != reader.identity(os.getpid())["owner"]:
            raise BindingError("selected_process_owner_mismatch")
        home, cwd = reader.home_and_cwd(agent["pid"])
        identity = {"platform": "windows", "root_pid": root_pid, "root_start_ticks": root["start_ticks"],
                    "pid": agent["pid"], "start_ticks": agent["start_ticks"], "session": agent["session"]}
        evidence = {"process_identity": identity, "codex_home": home, "process_cwd": cwd}
        paths = reader.held_rollouts(agent["pid"], home)
        result = _candidate_binding(paths, home, cwd)
        # Snapshot selected ancestry once more to exclude PID reuse/reparenting.
        if (reader.identity(root_pid) != root or reader.identity(agent["pid"]) != agent
                or reader.descendants(root_pid) != descendants or reader.home_and_cwd(agent["pid"]) != (home, cwd)):
            raise BindingError("process_changed")
        if paths and reader.held_rollouts(agent["pid"], home) != paths:
            raise BindingError("session_evidence_changed")
        return {**result, **evidence}
    except BindingError as exc:
        return _unbound(str(exc), **({} if str(exc) in {"process_changed", "session_evidence_changed"} else evidence))
    except (OSError, ValueError, TypeError, KeyError):
        return _unbound("windows_process_metadata_unavailable")


def _held_rollout_candidate(value, home):
    """Scope a held file using canonical paths on both sides of containment.

    Windows handle paths and process CODEX_HOME can use different long/8.3
    spellings. Resolve both without broadening the allowed rollout roots.
    """
    if not value.endswith(".jsonl"):
        return None
    canonical_home = Path(home).resolve()
    candidate = Path(value).resolve()
    if any(candidate.is_relative_to(canonical_home / directory)
           for directory in ("sessions", "archived_sessions")):
        return str(candidate)
    return None


class WindowsProc:
    """Native process-local inspection; private env/path data stays private."""
    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ctypes, self.w = ctypes, wintypes
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        self.nt = ctypes.WinDLL("ntdll")
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.GetCurrentProcess.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        self.kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        self.kernel.ReadProcessMemory.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.LPVOID, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        self.kernel.DuplicateHandle.argtypes = [wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.GetFileType.argtypes = [wintypes.HANDLE]
        self.kernel.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        self.kernel.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        self.advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        self.advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        self.advapi.GetLengthSid.argtypes = [wintypes.LPVOID]
        self.advapi.GetLengthSid.restype = wintypes.DWORD
        self.nt.NtQueryInformationProcess.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
        self.nt.NtQueryInformationProcess.restype = ctypes.c_long

    def _open(self, pid, access=0x1000):
        handle = self.kernel.OpenProcess(access, False, pid)
        if not handle:
            raise BindingError("selected_process_unavailable")
        return handle

    def _owner(self, process):
        c, w = self.ctypes, self.w
        token = w.HANDLE()
        if not self.advapi.OpenProcessToken(process, 8, c.byref(token)):
            raise BindingError("selected_process_owner_unavailable")
        try:
            needed = w.DWORD()
            self.advapi.GetTokenInformation(token, 1, None, 0, c.byref(needed))
            if not 0 < needed.value <= 65536:
                raise BindingError("selected_process_owner_unavailable")
            buffer = c.create_string_buffer(needed.value)
            if not self.advapi.GetTokenInformation(token, 1, buffer, needed, c.byref(needed)):
                raise BindingError("selected_process_owner_unavailable")
            pointer = c.cast(buffer, c.POINTER(c.c_void_p))[0]
            length = self.advapi.GetLengthSid(pointer)
            if not 0 < length < 1024 or not c.addressof(buffer) <= pointer < c.addressof(buffer) + needed.value:
                raise BindingError("selected_process_owner_unavailable")
            return c.string_at(pointer, length)
        finally:
            self.kernel.CloseHandle(token)

    def identity(self, pid):
        c, w = self.ctypes, self.w
        handle = self._open(pid)
        try:
            times = [w.FILETIME() for _ in range(4)]
            if not self.kernel.GetProcessTimes(handle, *(c.byref(item) for item in times)):
                raise BindingError("process_changed")
            buffer, size = c.create_unicode_buffer(32768), w.DWORD(32768)
            if not self.kernel.QueryFullProcessImageNameW(handle, 0, buffer, c.byref(size)):
                raise BindingError("process_changed")
            session = w.DWORD()
            if not self.kernel.ProcessIdToSessionId(pid, c.byref(session)):
                raise BindingError("process_changed")
            return {"pid": pid, "start_ticks": (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime,
                    "exe": buffer.value, "session": session.value, "owner": self._owner(handle)}
        finally:
            self.kernel.CloseHandle(handle)

    def descendants(self, root_pid):
        c, w = self.ctypes, self.w
        class Entry(c.Structure):
            _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("th32ProcessID", w.DWORD),
                        ("th32DefaultHeapID", c.c_size_t), ("th32ModuleID", w.DWORD), ("cntThreads", w.DWORD),
                        ("th32ParentProcessID", w.DWORD), ("pcPriClassBase", w.LONG), ("dwFlags", w.DWORD), ("szExeFile", w.WCHAR * 260)]
        self.kernel.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
        self.kernel.CreateToolhelp32Snapshot.restype = w.HANDLE
        self.kernel.Process32FirstW.argtypes = [w.HANDLE, c.POINTER(Entry)]
        self.kernel.Process32NextW.argtypes = [w.HANDLE, c.POINTER(Entry)]
        snapshot = self.kernel.CreateToolhelp32Snapshot(2, 0)
        if snapshot == c.c_void_p(-1).value:
            raise BindingError("process_tree_unavailable")
        try:
            row, parents = Entry(), {}
            row.dwSize = c.sizeof(row)
            available = self.kernel.Process32FirstW(snapshot, c.byref(row))
            while available:
                parents[row.th32ProcessID] = row.th32ParentProcessID
                if len(parents) > 65536:
                    raise BindingError("process_tree_limit")
                available = self.kernel.Process32NextW(snapshot, c.byref(row))
            found, pending = {}, [(root_pid, 0)]
            while pending:
                pid, depth = pending.pop()
                if pid in found:
                    continue
                if depth > MAX_DEPTH or len(found) >= MAX_PROCESSES or pid not in parents:
                    raise BindingError("process_tree_limit")
                found[pid] = parents[pid]
                pending.extend((child, depth + 1) for child, parent in parents.items() if parent == pid and child != pid)
            return sorted(found.items())
        finally:
            self.kernel.CloseHandle(snapshot)

    def _memory(self, handle, address, size):
        c = self.ctypes
        if not address or not 0 < size <= 1024 * 1024:
            raise BindingError("process_metadata_limit")
        buffer, copied = c.create_string_buffer(size), c.c_size_t()
        if not self.kernel.ReadProcessMemory(handle, address, buffer, size, c.byref(copied)) or copied.value != size:
            raise BindingError("process_environment_unavailable")
        return buffer.raw

    def home_and_cwd(self, pid):
        c, w = self.ctypes, self.w
        handle = self._open(pid, 0x410)  # QUERY_INFORMATION | VM_READ
        try:
            pointer_size = c.sizeof(c.c_void_p)
            wow_peb, length = c.c_size_t(), w.ULONG()
            status = self.nt.NtQueryInformationProcess(handle, 26, c.byref(wow_peb), c.sizeof(wow_peb), c.byref(length))
            if status == 0 and wow_peb.value:
                peb, pointer_size = wow_peb.value, 4
            else:
                basic = c.create_string_buffer(pointer_size * 6)
                if self.nt.NtQueryInformationProcess(handle, 0, basic, len(basic), c.byref(length)) != 0:
                    raise BindingError("process_environment_unavailable")
                peb = int.from_bytes(basic.raw[pointer_size:pointer_size * 2], "little")
            params = int.from_bytes(self._memory(handle, peb + (0x20 if pointer_size == 8 else 0x10), pointer_size), "little")
            cwd_offset, env_offset = (0x38, 0x80) if pointer_size == 8 else (0x24, 0x48)
            cwd_header = self._memory(handle, params + cwd_offset, 16 if pointer_size == 8 else 8)
            cwd_length = int.from_bytes(cwd_header[:2], "little")
            cwd_address = int.from_bytes(cwd_header[8:16] if pointer_size == 8 else cwd_header[4:8], "little")
            cwd = self._memory(handle, cwd_address, cwd_length).decode("utf-16-le", "strict")
            env = int.from_bytes(self._memory(handle, params + env_offset, pointer_size), "little")
            # Read bounded UTF-16 chunks until the environment terminator. Do
            # not decode/export any key other than CODEX_HOME.
            data = b""
            while len(data) < 1024 * 1024:
                # Never cross a page boundary, which could include uncommitted
                # memory after the final environment allocation.
                address = env + len(data)
                chunk = self._memory(handle, address, min(256, 4096 - address % 4096))
                data += chunk
                terminator = next((offset for offset in range(0, len(data) - 3, 2)
                                   if data[offset:offset + 4] == b"\0\0\0\0"), None)
                if terminator is not None:
                    data = data[:terminator + 4]
                    break
            else:
                raise BindingError("process_environment_limit")
            prefix = "CODEX_HOME=".encode("utf-16-le")
            values, start = [], 0
            for offset in range(0, len(data), 2):
                if data[offset:offset + 2] == b"\0\0":
                    item = data[start:offset]
                    if item.startswith(prefix):
                        values.append(item[len(prefix):].decode("utf-16-le", "strict"))
                    start = offset + 2
            if len(values) > 1 or (values and not Path(values[0]).is_absolute()):
                raise BindingError("codex_home_unresolved")
            home = Path(values[0]) if values else Path.home() / ".codex"
            return str(home.resolve()), cwd
        finally:
            self.kernel.CloseHandle(handle)

    def held_rollouts(self, pid, home):
        c, w = self.ctypes, self.w
        class HandleEntry(c.Structure):
            _fields_ = [("value", w.HANDLE), ("handle_count", c.c_size_t), ("pointer_count", c.c_size_t),
                        ("access", w.ULONG), ("type_index", w.ULONG), ("attributes", w.ULONG), ("reserved", w.ULONG)]
        process = self._open(pid, 0x440)  # QUERY_INFORMATION | DUP_HANDLE
        try:
            size, needed = 65536, w.ULONG()
            while size <= 8 * 1024 * 1024:
                buffer = c.create_string_buffer(size)
                status = self.nt.NtQueryInformationProcess(process, 51, buffer, size, c.byref(needed))
                if status == 0:
                    break
                if status & 0xffffffff != 0xc0000004:
                    raise BindingError("process_handles_unavailable")
                size = max(size * 2, needed.value)
            else:
                raise BindingError("process_handle_limit")
            count = c.cast(buffer, c.POINTER(c.c_size_t))[0]
            offset = c.sizeof(c.c_size_t) * 2
            if count > MAX_HANDLES or offset + count * c.sizeof(HandleEntry) > size:
                raise BindingError("process_handle_limit")
            paths = set()
            for index in range(count):
                entry = HandleEntry.from_buffer(buffer, offset + index * c.sizeof(HandleEntry))
                duplicate = w.HANDLE()
                if not self.kernel.DuplicateHandle(process, entry.value, self.kernel.GetCurrentProcess(), c.byref(duplicate), 0, False, 2):
                    continue
                try:
                    if self.kernel.GetFileType(duplicate) != 1:
                        continue
                    output = c.create_unicode_buffer(32768)
                    length = self.kernel.GetFinalPathNameByHandleW(duplicate, output, len(output), 0)
                    if not 0 < length < len(output):
                        continue
                    value = output.value
                    if value.startswith("\\\\?\\UNC\\"):
                        value = "\\\\" + value[8:]
                    elif value.startswith("\\\\?\\"):
                        value = value[4:]
                    candidate = _held_rollout_candidate(value, home)
                    if candidate is not None:
                        paths.add(candidate)
                finally:
                    self.kernel.CloseHandle(duplicate)
            return sorted(paths)
        finally:
            self.kernel.CloseHandle(process)


def main(request):
    if not isinstance(request, dict):
        raise ValueError("Invalid binding request")
    operation = request.get("op", "bind_output")
    if operation == "resolve_session":
        return resolve_session(request["session_id"], request["codex_home"])
    if operation == "bind_output":
        if "terminal_id" in request:
            return bind_linux(request["terminal_id"])
        return bind_windows(request.get("root_pid"))
    raise ValueError("Unknown binding operation")


if __name__ == "__main__":
    try:
        request = json.loads(base64.urlsafe_b64decode(sys.argv[1]))
        print(json.dumps({"ok": True, "result": main(request)}, ensure_ascii=False))
    except Exception:
        print(json.dumps({"ok": False, "error": "Session binding request failed"}, ensure_ascii=False))
