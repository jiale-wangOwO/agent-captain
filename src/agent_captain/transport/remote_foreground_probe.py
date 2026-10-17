"""Read-only identity probe for one owned tmux pane's foreground job.

The pane command can be a Node launcher while its native Codex child owns the
same foreground group. Only that pane's bounded descendant tree is inspected.
No task input, executable execution, environment or credential/config reads.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path, PurePosixPath

SESSION = re.compile(r"ceohub-[a-f0-9]{16,48}\Z")
SHELLS = {"bash", "sh", "zsh", "fish", "dash", "pwsh", "powershell", "cmd"}
NATIVE_AGENTS = {"codex", "claude", "aider", "opencode", "gemini"}
MAX_PROCESSES = 256
MAX_DEPTH = 16
MARKER = "CEO_HUB_FOREGROUND_RESULT="


def _command(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.+-]{1,64}", value) else ""


def _official_launcher(path):
    return isinstance(path, str) and bool(re.fullmatch(r"/.+/node_modules/@openai/codex/bin/codex\.(?:js|mjs|cjs)", path))


def classify_foreground(records, pane_pid, pane_tty_nr, foreground_pgid, uid, command, stable=True):
    """Pure bounded classifier. Process paths are input evidence, never output."""
    command = _command(command)
    unknown = {"command": command, "state": "unknown", "reason": "agent_identity_unresolved", "identity_verified": False}
    if not stable:
        return {**unknown, "reason": "process_changed"}
    if any(type(value) is not int for value in (pane_pid, pane_tty_nr, foreground_pgid, uid)) or pane_pid <= 0 or not pane_tty_nr or foreground_pgid <= 0 or uid < 0:
        return unknown
    records = list(records)
    if len(records) > MAX_PROCESSES:
        return unknown
    index = {}
    for row in records:
        if not isinstance(row, dict) or any(type(row.get(key)) is not int for key in ("pid", "ppid", "pgid", "tpgid", "tty_nr", "uid", "start_time")):
            return unknown
        if row["pid"] <= 0 or row["start_time"] <= 0:
            return unknown
        if row["pid"] in index and row != index[row["pid"]]:
            return {**unknown, "reason": "process_changed"}
        index[row["pid"]] = row
    pane = index.get(pane_pid)
    if not pane or (pane["uid"], pane["tty_nr"], pane["tpgid"]) != (uid, pane_tty_nr, foreground_pgid):
        return unknown
    if command in SHELLS:
        # A shell can share its group with asynchronous children when job
        # control is disabled. A background Codex never authorizes shell input.
        return {**unknown, "state": "shell", "reason": "foreground_shell"}
    leader = index.get(foreground_pgid)
    if not leader or (leader["uid"], leader["tty_nr"], leader["pgid"], leader["tpgid"]) != (uid, pane_tty_nr, foreground_pgid, foreground_pgid):
        return unknown
    reachable = {pane_pid}
    for _ in range(MAX_DEPTH):
        added = {row["pid"] for row in records if row["ppid"] in reachable and row["uid"] == uid and row["tty_nr"] == pane_tty_nr}
        if added <= reachable:
            break
        reachable |= added
    if foreground_pgid not in reachable:
        return unknown
    candidates = [row for row in records if row["pid"] in reachable and row["uid"] == uid and row["tty_nr"] == pane_tty_nr
                  and row["pgid"] == foreground_pgid and row["tpgid"] == foreground_pgid and row.get("stdin_matches_tty", True)]
    # Prefer the native child, not a wrapper, for stable PID/start-time evidence.
    native = [row for row in candidates if PurePosixPath(str(row.get("exe", ""))).name == "codex"]
    launchers = [row for row in candidates if PurePosixPath(str(row.get("exe", ""))).name in ("node", "nodejs") and _official_launcher(row.get("launcher_path"))]
    matches = native or launchers
    if not matches and command in NATIVE_AGENTS - {"codex"}:
        matches = [row for row in candidates if PurePosixPath(str(row.get("exe", ""))).name == command]
    if len(matches) != 1:
        return unknown
    agent = matches[0]
    return {"command": command, "state": "agent", "reason": "verified_foreground_codex" if command not in NATIVE_AGENTS - {"codex"} else "verified_foreground_agent",
            "identity_verified": True, "agent_kind": "codex" if native or launchers else command,
            "foreground_pgid": foreground_pgid, "agent_pid": agent["pid"], "agent_start_ticks": agent["start_time"]}


class ProcessChanged(RuntimeError):
    pass


class ProcReader:
    def __init__(self, root="/proc"):
        self.root = Path(root)

    @staticmethod
    def _read(path, limit):
        with path.open("rb") as stream:
            value = stream.read(limit + 1)
        if len(value) > limit:
            raise ProcessChanged()
        return value

    def stat(self, pid):
        path = self.root / str(pid)
        raw = self._read(path / "stat", 8192).decode("utf-8", "replace")
        prefix, separator, suffix = raw.rpartition(")")
        fields = suffix.split()
        if not separator or int(prefix.split(" ", 1)[0]) != pid or len(fields) < 20:
            raise ProcessChanged()
        return {"pid": pid, "ppid": int(fields[1]), "pgid": int(fields[2]), "tty_nr": int(fields[4]),
                "tpgid": int(fields[5]), "start_time": int(fields[19]), "uid": path.stat().st_uid}

    def read(self, pid, tty_nr):
        row = self.stat(pid)
        path = self.root / str(pid)
        row["exe"] = os.readlink(path / "exe").removesuffix(" (deleted)")
        try:
            info = (path / "fd" / "0").stat()
            # /proc encodes the controlling TTY using Linux's device encoding.
            major, minor = os.major(info.st_rdev), os.minor(info.st_rdev)
            device = (minor & 0xff) | (major << 8) | ((minor & ~0xff) << 12)
            row["stdin_matches_tty"] = bool(info.st_rdev) and device == (tty_nr & 0xffffffff)
        except OSError:
            row["stdin_matches_tty"] = False
        if self.stat(pid) != {key: row[key] for key in ("pid", "ppid", "pgid", "tty_nr", "tpgid", "start_time", "uid")}:
            raise ProcessChanged()
        return row

    def launcher(self, pid):
        # Read only a candidate Node process. Do not export or log its arguments;
        # only argv[1], the ordinary Node entry script, is considered evidence.
        args = self._read(self.root / str(pid) / "cmdline", 16384).split(b"\0")
        if len(args) < 2:
            return None
        path = args[1].decode("utf-8", "strict")
        return path if _official_launcher(path) else None

    def children(self, pid):
        raw = self._read(self.root / str(pid) / "task" / str(pid) / "children", 8192)
        return [int(value) for value in raw.split()]


def pane_metadata(session_id):
    if not SESSION.fullmatch(session_id):
        raise ValueError("Invalid owned terminal session ID")
    result = subprocess.run(["tmux", "display-message", "-p", "-t", session_id,
                             "#{pane_pid}\t#{pane_tty}\t#{pane_current_command}"], stdin=subprocess.DEVNULL,
                            capture_output=True, timeout=5, text=True, encoding="utf-8", errors="strict")
    if result.returncode or len(result.stdout) > 1024:
        raise ProcessChanged()
    values = result.stdout.strip().split("\t")
    if len(values) != 3 or not re.fullmatch(r"/dev/(?:pts/[0-9]+|tty[A-Za-z0-9]*)", values[1]):
        raise ProcessChanged()
    return {"pid": int(values[0]), "tty": values[1], "command": _command(values[2])}


def probe(session_id, proc_root="/proc"):
    first = pane_metadata(session_id)
    reader = ProcReader(proc_root)
    command = first["command"]
    uid = os.geteuid()
    pane = reader.stat(first["pid"])
    tty_nr, group = pane["tty_nr"], pane["tpgid"]
    if pane["uid"] != uid:
        raise ProcessChanged()
    records, pending, seen = [], [(first["pid"], 0)], set()
    while pending:
        pid, depth = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        if len(seen) > MAX_PROCESSES or depth > MAX_DEPTH:
            raise ProcessChanged()
        row = reader.read(pid, tty_nr)
        if row["uid"] != uid or row["tty_nr"] != tty_nr:
            continue
        records.append(row)
        pending.extend((child, depth + 1) for child in reader.children(pid))
    if not any(PurePosixPath(str(row.get("exe", ""))).name == "codex" and row["pgid"] == group and row.get("stdin_matches_tty") for row in records):
        for row in records:
            if row["pgid"] == group and row["tpgid"] == group and row.get("stdin_matches_tty") and PurePosixPath(row["exe"]).name in ("node", "nodejs"):
                row["launcher_path"] = reader.launcher(row["pid"])
    result = classify_foreground(records, first["pid"], tty_nr, group, uid, command)
    # Identity is trusted only if the relevant process ancestry and exact pane
    # remain unchanged while reading stat/exe/optional script identity.
    stable = pane_metadata(session_id) == first and reader.stat(first["pid"]) == pane
    if result.get("identity_verified"):
        index = {row["pid"]: row for row in records}
        current = result["agent_pid"]
        while current in index:
            row = index[current]
            if reader.stat(current) != {key: row[key] for key in ("pid", "ppid", "pgid", "tty_nr", "tpgid", "start_time", "uid")} or os.readlink(reader.root / str(current) / "exe").removesuffix(" (deleted)") != row["exe"]:
                stable = False
                break
            if row.get("launcher_path") and reader.launcher(current) != row["launcher_path"]:
                stable = False
                break
            if current == first["pid"]:
                break
            current = row["ppid"]
    return result if stable else classify_foreground(records, first["pid"], tty_nr, group, uid, command, stable=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", required=True)
    args = parser.parse_args()
    try:
        result = probe(args.session)
    except (OSError, ValueError, ProcessChanged, subprocess.TimeoutExpired):
        result = {"command": "", "state": "unknown", "reason": "process_changed", "identity_verified": False}
    print(MARKER + json.dumps(result, ensure_ascii=True), flush=True)
