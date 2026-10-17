"""Read one owned tmux pane's retained text without changing its display/input.

Sent through one selected SSH connection's stdin; no remote installation. This
is retained terminal history, not a complete or semantic Codex conversation.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import selectors
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SESSION = re.compile(r"ceohub-[a-f0-9]{16,48}\Z")
MAX_LINES = 5000
MAX_TEXT_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 2 * MAX_TEXT_BYTES + 65536
RESULT_MARKER = "PRON_HUB_TERMINAL_HISTORY="
NOTE = "只读的 tmux 已保留终端文本快照，不改变远端显示或输入；历史可能受 tmux 上限、清屏或备用屏幕限制，不是完整 Codex 会话。完整回复请使用已准确关联的「会话输出」。"


class HistoryError(RuntimeError):
    """Only stable public reason codes, never raw subprocess/provider text."""


def validate_lines(lines):
    if type(lines) is not int or not 1 <= lines <= MAX_LINES:
        raise HistoryError("invalid_lines")
    return lines


def valid_identity(identity, session_id):
    return (isinstance(identity, dict)
        and set(identity) == {"name", "session", "created", "pane", "pane_pid", "pane_start_ticks"}
        and identity["name"] == session_id
        and isinstance(identity["session"], str) and re.fullmatch(r"\$[0-9]+", identity["session"])
        and isinstance(identity["pane"], str) and re.fullmatch(r"%[0-9]+", identity["pane"])
        and all(type(identity[key]) is int and identity[key] > 0 for key in ("created", "pane_pid", "pane_start_ticks")))


def _pane_start_ticks(pid):
    directory = Path("/proc") / str(pid)
    try:
        if directory.stat().st_uid != os.geteuid():
            raise ValueError()
        with (directory / "stat").open("rb") as stream:
            raw = stream.read(8193)
        prefix, separator, suffix = raw.decode("utf-8").rpartition(")")
        fields = suffix.split()
        if (len(raw) > 8192 or not separator or int(prefix.split(" ", 1)[0]) != pid
                or len(fields) < 20 or int(fields[19]) <= 0):
            raise ValueError()
        return int(fields[19])
    except (OSError, UnicodeError, ValueError):
        raise HistoryError("identity_unavailable") from None


def probe_pane(session_id):
    if not isinstance(session_id, str) or not SESSION.fullmatch(session_id):
        raise HistoryError("invalid_terminal_id")
    # '=name:' makes the session component exact. Only this pane is inspected;
    # no listing/search of other sessions, cwd, argv or credential files.
    try:
        reply = subprocess.run(["tmux", "display-message", "-p", "-t", "=" + session_id + ":",
            "#{session_name}\t#{session_id}\t#{session_created}\t#{pane_id}\t#{pane_pid}\t#{pane_width}\t#{pane_height}\t#{history_size}\t#{alternate_on}"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5)
        raw = reply.stdout
        if reply.returncode or not isinstance(raw, bytes) or len(raw) > 8192:
            raise ValueError()
        fields = raw.decode("utf-8").strip().split("\t")
        if (len(fields) != 9 or fields[0] != session_id or not re.fullmatch(r"\$[0-9]+", fields[1])
                or not re.fullmatch(r"%[0-9]+", fields[3])
                or any(not value.isascii() or not value.isdigit() for value in (fields[2], *fields[4:]))):
            raise ValueError()
        created, pid, cols, rows, history_size, alternate = map(int, (fields[2], *fields[4:]))
        if (created <= 0 or pid <= 0 or not 1 <= cols <= 500 or not 1 <= rows <= 200
                or not 0 <= history_size <= 2 ** 31 - 1 or alternate not in (0, 1)):
            raise ValueError()
        identity = {"name": fields[0], "session": fields[1], "created": created,
                    "pane": fields[3], "pane_pid": pid, "pane_start_ticks": _pane_start_ticks(pid)}
        return {"identity": identity, "cols": cols, "rows": rows,
                "history_size": history_size, "alternate_on": bool(alternate)}
    except (OSError, UnicodeError, ValueError, subprocess.TimeoutExpired):
        raise HistoryError("identity_unavailable") from None


def capture_text(pane, lines):
    """Bound bytes while reading; only our own read-only capture child is ended."""
    if not isinstance(pane, str) or not re.fullmatch(r"%[0-9]+", pane):
        raise HistoryError("identity_unavailable")
    validate_lines(lines)
    command = ["tmux", "capture-pane", "-p", "-S", str(-lines), "-E", "-", "-t", pane]
    child = None
    try:
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL)
        retained = bytearray()
        truncated = False
        deadline = time.monotonic() + 8
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise HistoryError("capture_unavailable")
                events = selector.select(remaining)
                if not events:
                    raise HistoryError("capture_unavailable")
                for key, _ in events:
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    retained.extend(chunk)
                    if len(retained) > MAX_TEXT_BYTES:
                        del retained[:len(retained) - MAX_TEXT_BYTES]
                        truncated = True
        if child.wait(timeout=max(0.01, deadline - time.monotonic())):
            raise HistoryError("capture_unavailable")
        return bytes(retained), truncated
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise HistoryError("capture_unavailable") from None
    finally:
        if child is not None:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=2)
            if child.stdout is not None:
                child.stdout.close()


def capture_history(session_id, lines=5000, *, probe=None, capture=None):
    if not isinstance(session_id, str) or not SESSION.fullmatch(session_id):
        raise HistoryError("invalid_terminal_id")
    validate_lines(lines)
    probe = probe or probe_pane
    capture = capture or capture_text
    before = probe(session_id)
    identity = before.get("identity")
    if not valid_identity(identity, session_id):
        raise HistoryError("identity_unavailable")
    raw, byte_truncated = capture(identity["pane"], lines)
    after = probe(session_id)
    if (after.get("identity") != identity or any(after.get(key) != before.get(key)
            for key in ("cols", "rows", "alternate_on"))):
        raise HistoryError("identity_changed")
    if not isinstance(raw, bytes) or len(raw) > MAX_TEXT_BYTES or type(byte_truncated) is not bool:
        raise HistoryError("capture_unavailable")
    # capture-pane without -e returns plain text. Remove any remaining terminal
    # controls, not just ANSI, so history can never replay protocol/input bytes.
    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
    text = "".join(char for char in text if char in "\n\t" or ord(char) >= 32 and not 127 <= ord(char) <= 159)
    if byte_truncated:
        # A bounded tail can begin in the middle of a wrapped physical row.
        _, separator, text = text.partition("\n")
        if not separator:
            text = ""
    text = text.rstrip("\n")
    captured_lines = text.split("\n") if text else []
    line_truncated = len(captured_lines) > lines
    captured_lines = captured_lines[-lines:]
    text = "\n".join(captured_lines)
    result = {"source": "tmux_pane_history", "text": text, "lines": lines,
              "line_count": len(captured_lines), "history_size": before["history_size"],
              "alternate_on": before["alternate_on"], "cols": before["cols"], "rows": before["rows"],
              "truncated": byte_truncated or line_truncated or before["history_size"] + before["rows"] > lines,
              "history_complete": False, "terminal_id": session_id, "identity": identity,
              "captured_at": datetime.now(timezone.utc).isoformat(), "note": NOTE}
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise HistoryError("capture_unavailable")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", required=True)
    parser.add_argument("--lines", type=int, default=5000)
    args = parser.parse_args()
    try:
        result = capture_history(args.session, args.lines)
        reply = {"ok": True, "result": result}
    except HistoryError as error:
        reply = {"ok": False, "reason": str(error)}
    except Exception:
        reply = {"ok": False, "reason": "capture_unavailable"}
    print(RESULT_MARKER + json.dumps(reply, ensure_ascii=False), flush=True)
    return 0 if reply["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
