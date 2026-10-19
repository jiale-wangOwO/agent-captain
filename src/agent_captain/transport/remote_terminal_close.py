"""Verify or close one exact owned tmux terminal for an explicit replacement.

Standalone source sent over authenticated SSH. It never creates sessions, starts
models, reads task text, or accepts an arbitrary command. Failure is not absence.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import re
import subprocess

SESSION = re.compile(r"ceohub-[a-f0-9]{16,48}\Z")
MARKER = "CEO_HUB_TERMINAL_CLOSE_RESULT="
IDENTITY_KEYS = {"name", "session", "created", "pane", "pane_pid", "pane_start_ticks", "boot_id", "server_pid"}
# tmux sanitizes tab characters to underscores under LC_ALL=C on some hosts.
# An ASCII separator works independently of locale; external session names may
# contain it, so the broad registry query splits only the final ID separator.
FIELD_SEPARATOR = "|"


class CloseError(RuntimeError):
    pass


def valid_identity(value, session_id):
    return (isinstance(value, dict) and set(value) == IDENTITY_KEYS
            and value.get("name") == session_id
            and isinstance(value.get("session"), str) and re.fullmatch(r"\$[0-9]+", value["session"]) is not None
            and isinstance(value.get("pane"), str) and re.fullmatch(r"%[0-9]+", value["pane"]) is not None
            and isinstance(value.get("boot_id"), str) and re.fullmatch(r"[a-f0-9-]{36}", value["boot_id"]) is not None
            and all(type(value.get(key)) is int and value[key] > 0
                    for key in ("created", "pane_pid", "pane_start_ticks", "server_pid")))


def run_tmux(*arguments):
    try:
        result = subprocess.run(["tmux", *arguments], stdin=subprocess.DEVNULL,
            capture_output=True, text=True, encoding="utf-8", errors="strict", timeout=5,
            env={**os.environ, "LC_ALL": "C"})
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        raise CloseError("tmux_unavailable") from None
    if len(result.stdout.encode("utf-8")) > 65536 or len(result.stderr.encode("utf-8")) > 4096:
        raise CloseError("invalid_tmux_reply")
    return result


def session_rows():
    result = run_tmux("list-sessions", "-F", "#{session_name}|#{session_id}")
    if result.returncode:
        # Only tmux's explicit no-running-server result is affirmative absence.
        # Permission, transport, missing binary, malformed output and timeout are
        # unknown. Do not convert a generic nonzero has-session into absence.
        if result.returncode == 1 and not result.stdout and re.fullmatch(
                r"no server running on /[^\r\n]+\n?", result.stderr):
            return []
        missing = re.fullmatch(r"error connecting to (/[^\r\n]+) \(No such file or directory\)\n?", result.stderr)
        if result.returncode == 1 and not result.stdout and missing:
            try:
                Path(missing.group(1)).lstat()
            except FileNotFoundError:
                return []
            except OSError:
                pass
        raise CloseError("tmux_query_failed")
    rows = []
    for line in result.stdout.splitlines():
        parts = line.rsplit(FIELD_SEPARATOR, 1)
        if len(parts) != 2 or not re.fullmatch(r"\$[0-9]+", parts[1]):
            raise CloseError("invalid_tmux_reply")
        rows.append(parts)
    return rows


def boot_id():
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        raise CloseError("identity_unavailable") from None
    if not re.fullmatch(r"[a-f0-9-]{36}", value):
        raise CloseError("identity_unavailable")
    return value


def pane_start_ticks(pid):
    try:
        path = Path("/proc") / str(pid)
        raw = (path / "stat").read_bytes()
        if len(raw) > 8192 or path.stat().st_uid != os.geteuid():
            raise ValueError()
        prefix, separator, suffix = raw.decode("utf-8").rpartition(")")
        ticks = int(suffix.split()[19])
        if not separator or int(prefix.split(" ", 1)[0]) != pid or ticks <= 0:
            raise ValueError()
        return ticks
    except (OSError, UnicodeError, ValueError, IndexError):
        raise CloseError("identity_unavailable") from None


def probe(session_id):
    if not isinstance(session_id, str) or not SESSION.fullmatch(session_id):
        raise CloseError("invalid_terminal")
    rows = session_rows()
    matches = [row for row in rows if row[0] == session_id]
    if not matches:
        return {"ok": True, "terminal_id": session_id, "exists": False}
    if len(matches) != 1:
        raise CloseError("identity_changed")
    result = run_tmux("display-message", "-p", "-t", matches[0][1] + ":",
        "#{session_name}|#{session_id}|#{session_created}|#{pane_id}|#{pane_pid}|#{pid}")
    if result.returncode:
        raise CloseError("identity_changed")
    fields = result.stdout.strip().split(FIELD_SEPARATOR)
    if len(fields) != 6:
        raise CloseError("identity_changed")
    try:
        identity = {"name": fields[0], "session": fields[1], "created": int(fields[2]),
            "pane": fields[3], "pane_pid": int(fields[4]), "server_pid": int(fields[5]),
            "boot_id": boot_id()}
        identity["pane_start_ticks"] = pane_start_ticks(identity["pane_pid"])
    except (IndexError, ValueError):
        raise CloseError("identity_changed") from None
    if not valid_identity(identity, session_id) or identity["session"] != matches[0][1]:
        raise CloseError("identity_changed")
    return {"ok": True, "terminal_id": session_id, "exists": True, "identity": identity}


def close(session_id, *, expected_identity=None, expected_absent=False):
    if type(expected_absent) is not bool or (expected_absent == (expected_identity is not None)):
        raise CloseError("invalid_expected_identity")
    if expected_identity is not None and not valid_identity(expected_identity, session_id):
        raise CloseError("invalid_expected_identity")
    before = probe(session_id)
    if expected_absent:
        if before["exists"]:
            raise CloseError("identity_changed")
    elif not before["exists"] or before["identity"] != expected_identity:
        raise CloseError("identity_changed")
    else:
        identity = before["identity"]
        # tmux evaluates the complete identity predicate and kill action in its
        # own command queue. A recreated name or changed pane cannot redirect
        # deletion. $id, never a prefix name, is the destructive target.
        conditions = ["#{==:#{" + field + "}," + str(identity[key]) + "}"
            for field, key in (("session_name", "name"), ("session_id", "session"),
                ("session_created", "created"), ("pane_id", "pane"),
                ("pane_pid", "pane_pid"), ("pid", "server_pid"))]
        predicate = conditions.pop()
        for condition in reversed(conditions):
            predicate = "#{&&:" + condition + "," + predicate + "}"
        if boot_id() != identity["boot_id"] or pane_start_ticks(identity["pane_pid"]) != identity["pane_start_ticks"]:
            raise CloseError("identity_changed")
        # if-shell parses the action again; quote literal $id so tmux does not
        # expand it as a global environment variable during that second parse.
        result = run_tmux("if-shell", "-F", "-t", identity["session"] + ":", predicate,
            "kill-session -t '" + identity["session"] + "'", "")
        if result.returncode:
            raise CloseError("close_unconfirmed")
    after = session_rows()
    if any(name == session_id or (expected_identity is not None and value == expected_identity["session"])
            for name, value in after):
        raise CloseError("close_unconfirmed")
    return {"ok": True, "terminal_id": session_id, "closed_verified": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("probe", "close"))
    parser.add_argument("--session", required=True)
    parser.add_argument("--expected")
    parser.add_argument("--expected-absent", action="store_true")
    args = parser.parse_args(argv)
    try:
        expected = None
        if args.expected is not None:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,4096}", args.expected):
                raise CloseError("invalid_expected_identity")
            expected = json.loads(base64.urlsafe_b64decode(args.expected + "=" * (-len(args.expected) % 4)))
        result = probe(args.session) if args.mode == "probe" else close(args.session,
            expected_identity=expected, expected_absent=args.expected_absent)
    except (CloseError, ValueError, UnicodeError) as error:
        result = {"ok": False, "reason": str(error) if isinstance(error, CloseError) else "invalid_expected_identity"}
    print(MARKER + json.dumps(result, separators=(",", ":")))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
