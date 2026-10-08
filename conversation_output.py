"""Exact-session, read-only Codex rollout output; portable over SSH stdin.

Only one caller-selected JSONL file is opened. No index, catalog, credentials,
or other session is inspected. Cursors freeze an append-only prefix and are
invalidated by truncation, replacement, or edits to that prefix.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
from collections import deque
from pathlib import Path

CHUNK_CHARS = 65536
PAGE_BYTES = 262144
MAX_RECORD_BYTES = 128 * 1024 * 1024
SIGNAL_CHARS = 2500
SIGNAL_ITEMS = 12
CURSOR_VERSION = 1


def _home(request):
    return Path(request.get("codex_home") or os.environ.get("CODEX_HOME") or
                str(Path.home() / ".codex")).resolve()


def _path(request):
    home = _home(request)
    path = Path(request["rollout_path"]).resolve(strict=True)
    roots = [(home / name).resolve() for name in ("sessions", "archived_sessions")]
    if (not path.is_file() or path.suffix.lower() != ".jsonl" or
            not any(root.is_relative_to(home) and path.is_relative_to(root) for root in roots)):
        raise ValueError("Refusing output outside CODEX_HOME sessions/archived_sessions.")
    return path


def _encode_cursor(state):
    encoded = json.dumps(state, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    envelope = {"state": state, "checksum": hashlib.sha256(encoded).hexdigest()}
    return base64.urlsafe_b64encode(json.dumps(envelope, ensure_ascii=False,
                                             separators=(",", ":")).encode("utf-8")).decode("ascii")


def _decode_cursor(value, path, session_id, include_tools=False):
    try:
        if not isinstance(value, str) or len(value) > 65536:
            raise ValueError("length")
        envelope = json.loads(base64.urlsafe_b64decode(value.encode("ascii")))
        state = envelope["state"]
        encoded = json.dumps(state, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
        if hashlib.sha256(encoded).hexdigest() != envelope["checksum"]:
            raise ValueError("checksum")
        if (state["version"] != CURSOR_VERSION or state["path"] != str(path) or
                state["session_id"] != session_id or state["mode"] not in ("window", "older", "after")):
            raise ValueError("binding")
        if state.get("include_tools", False) is not include_tools:
            raise ValueError("tool output binding")
        if not isinstance(state["size"], int) or state["size"] < 0:
            raise ValueError("size")
        if (not isinstance(state["digest"], str) or len(state["digest"]) != 64 or
                any(c not in "0123456789abcdef" for c in state["digest"]) or
                any(not isinstance(state[name], int) or state[name] < 0 for name in ("dev", "ino"))):
            raise ValueError("identity")
        if state["mode"] in ("older", "window"):
            if not isinstance(state["upper"], int) or not 0 <= state["upper"] <= state["size"]:
                raise ValueError("upper")
        if state["mode"] == "window":
            if (any(not isinstance(state[name], int) for name in ("lower", "next", "char")) or
                    not 0 <= state["lower"] <= state["next"] < state["upper"] or state["char"] < 0):
                raise ValueError("window")
        return state
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid or differently bound output cursor.") from exc


def _digest(stream, size):
    stream.seek(0)
    digest = hashlib.sha256()
    remaining = size
    while remaining:
        block = stream.read(min(1024 * 1024, remaining))
        if not block:
            raise ValueError("Rollout was truncated while reading.")
        digest.update(block)
        remaining -= len(block)
    return digest.hexdigest()


def _lines(stream, size, issues):
    """Yield complete bounded records, retaining byte offsets, not all records."""
    stream.seek(0)
    complete_end = 0
    while stream.tell() < size:
        offset = stream.tell()
        line = stream.readline(min(MAX_RECORD_BYTES + 1, size - offset))
        if len(line) > MAX_RECORD_BYTES and not line.endswith(b"\n"):
            while stream.tell() < size and not line.endswith(b"\n"):
                line = stream.readline(min(1024 * 1024, size - stream.tell()))
            issues["oversized_records"] += 1
            if line.endswith(b"\n"):
                complete_end = stream.tell()
            else:
                issues["incomplete_last_line"] = True
            continue
        if not line.endswith(b"\n"):
            issues["incomplete_last_line"] = True
            break
        complete_end = stream.tell()
        try:
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("not an object")
        except (ValueError, UnicodeError):
            issues["malformed_records"] += 1
            continue
        yield offset, value
    issues["complete_end"] = complete_end


def _visible_user_text(text):
    """Remove leading, explicitly generated context, never keyword matches.

    Source-qualified internal/ambient wrappers are recognized only as complete
    leading blocks. A quoted/fenced example, a block after ordinary prose, or
    an unknown source remains ordinary user text. Human text after a generated
    block survives, including app messages using the ``## My request:`` marker.
    """
    result = text
    while True:
        candidate = result.lstrip()
        opening = re.match(r'<(codex_internal_context|in-app-browser-context)\b([^>]*)>', candidate)
        if opening:
            tag, attributes = opening.groups()
            source = re.search(r'(?:^|\s)source\s*=\s*([\'"])(.*?)\1', attributes)
            expected = "goal" if tag == "codex_internal_context" else "ambient-ui-state"
            closing = "</" + tag + ">"
            end = candidate.find(closing, opening.end())
            if not source or source.group(2) != expected or end < 0:
                return result
            result = candidate[end + len(closing):].lstrip()
            if not result:
                return None
            continue
        known = ("<environment_context>", "<permissions instructions>",
                 "<skills_instructions>", "<app-context>")
        prefix = next((value for value in known if candidate.startswith(value)), None)
        if prefix:
            tag = prefix[1:-1]
            closing = "</" + tag + ">"
            end = candidate.find(closing, len(prefix))
            if end >= 0:
                result = candidate[end + len(closing):].lstrip()
                if not result:
                    return None
                continue
            # These reserved app wrappers were historically emitted without
            # a closing tag; recover an explicit human request when available.
            marker = "## My request:"
            if marker in candidate:
                return candidate.split(marker, 1)[1].strip() or None
            return None
        if candidate.startswith("# AGENTS.md instructions for "):
            marker = "## My request:"
            if marker in candidate:
                return candidate.split(marker, 1)[1].strip() or None
            return None
        if candidate.startswith("## My request:") and result != text:
            return candidate[len("## My request:"):].strip() or None
        return result


def _message(event, include_tools=False):
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    channel = payload.get("channel") or payload.get("phase")
    if channel in ("analysis", "reasoning", "summary", "system"):
        return None
    tool = None
    if (include_tools and event.get("type") == "response_item" and payload.get("type") in
            ("function_call", "custom_tool_call", "function_call_output", "custom_tool_call_output")):
        role = "tool"
        kind = payload["type"]
        value = payload.get("output", "") if kind.endswith("_output") else {
            "name": payload.get("name"),
            "arguments" if kind == "function_call" else "input": payload.get(
                "arguments" if kind == "function_call" else "input", "")}
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        source = "response_item"
        tool = {"tool_type": kind, "name": payload.get("name"), "call_id": payload.get("call_id")}
    elif event.get("type") == "response_item" and payload.get("type") == "message":
        role = payload.get("role")
        if role not in ("user", "assistant"):
            return None
        content = payload.get("content")
        if not isinstance(content, list):
            return None
        text = "\n".join(part["text"] for part in content
                         if isinstance(part, dict) and isinstance(part.get("text"), str)
                         and part.get("type") in ("input_text", "output_text", "text"))
        source = "response_item"
    elif event.get("type") == "event_msg" and payload.get("type") in ("agent_message", "user_message"):
        role = "assistant" if payload["type"] == "agent_message" else "user"
        text = payload.get("message", "")
        if not isinstance(text, str):
            return None
        source = "event_msg"
    else:
        return None
    if not text:
        return None
    if role == "assistant" and channel not in (None, "final", "final_answer", "commentary"):
        return None
    if role == "user":
        text = _visible_user_text(text)
        if not text:
            return None
    return {"role": role, "time": event.get("timestamp"), "channel": channel,
            "source": source, "text": text, **(tool or {})}


def _ref(offset, message, anchor=None):
    digest = hashlib.sha256(message["text"].encode("utf-8")).hexdigest()
    origin = offset if anchor is None else anchor
    return {"offset": offset, "anchor": origin, "role": message["role"],
            "hash": digest, "length": len(message["text"]),
            "message_id": "message-" + str(origin) + "-" + digest[:16]}


def _signal(event):
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    kind = payload.get("type")
    if event.get("type") == "event_msg" and kind in (
            "task_started", "turn_started", "task_complete", "turn_complete", "turn_completed",
            "turn_aborted", "task_aborted", "error", "warning"):
        text = payload.get("message", payload.get("last_agent_message", ""))
    elif event.get("type") == "response_item" and kind in ("function_call_output", "custom_tool_call_output"):
        text = payload.get("output", "")
        kind = "tool_output"
    else:
        return None
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False)
    return {"time": event.get("timestamp"), "type": kind, "text": text[-SIGNAL_CHARS:],
            "text_complete": len(text) <= SIGNAL_CHARS, "original_chars": len(text)}


def _scan(stream, size, session_id, issues, signals, status, include_tools=False):
    pending = None
    last_response = None
    meta_seen = False
    for offset, event in _lines(stream, size, issues):
        payload = event.get("payload")
        if event.get("type") == "session_meta":
            if not isinstance(payload, dict) or payload.get("id") != session_id:
                raise ValueError("Rollout session_meta does not match selected session.")
            meta_seen = True
            status["cwd"] = payload.get("cwd")
        elif not meta_seen:
            raise ValueError("Rollout must begin with matching session_meta.")
        signal = _signal(event)
        if signal:
            status["signal_count"] += 1
            signals.append(signal)
            if signal["type"] in ("task_started", "turn_started"):
                if pending:
                    yield pending
                    pending = None
                last_response = None
                status["status"] = "working_in_history"
            elif signal["type"] in ("task_complete", "turn_complete", "turn_completed"):
                status["status"] = "completed_in_history"
            elif signal["type"] in ("turn_aborted", "task_aborted"):
                status["status"] = "interrupted_in_history"
        message = _message(event, include_tools)
        if not message:
            continue
        ref = _ref(offset, message)
        signature = ref["role"], ref["hash"]
        if message["source"] == "event_msg":
            if last_response and signature == (last_response["role"], last_response["hash"]):
                continue
            if pending:
                yield pending
            pending = ref
            last_response = None
        else:
            if pending:
                if signature == (pending["role"], pending["hash"]):
                    ref = _ref(offset, message, pending["anchor"])
                else:
                    yield pending
                pending = None
            yield ref
            last_response = ref
    if not meta_seen:
        raise ValueError("Rollout has no complete matching session_meta.")
    if pending:
        yield pending


def _load_message(stream, ref, include_tools=False):
    stream.seek(ref["offset"])
    line = stream.readline(MAX_RECORD_BYTES + 1)
    message = _message(json.loads(line), include_tools)
    if not message or _ref(ref["offset"], message)["hash"] != ref["hash"]:
        raise ValueError("Rollout changed while reading selected output.")
    return message


def main(request):
    if request.get("op") != "read_output":
        raise ValueError("Unknown conversation output operation.")
    session_id = request.get("session_id")
    if not isinstance(session_id, str) or not session_id or len(session_id) > 200:
        raise ValueError("A precise session_id is required.")
    path = _path(request)
    include_tools = request.get("include_tools", False)
    if not isinstance(include_tools, bool):
        raise ValueError("include_tools must be a boolean.")
    limit = min(100, max(1, int(request.get("limit", 18))))
    value = request.get("cursor") or request.get("after_cursor")
    cursor = _decode_cursor(value, path, session_id, include_tools) if value else None
    issues = {"malformed_records": 0, "oversized_records": 0, "incomplete_last_line": False}
    signals = deque(maxlen=SIGNAL_ITEMS)
    status = {"status": "unknown", "signal_count": 0}
    with path.open("rb") as stream:
        stat = os.fstat(stream.fileno())
        if cursor:
            if (cursor["dev"] != stat.st_dev or cursor["ino"] != stat.st_ino or
                    stat.st_size < cursor["size"] or _digest(stream, cursor["size"]) != cursor["digest"]):
                raise ValueError("Output cursor is stale: rollout replaced, truncated, or rewritten.")
        live = cursor is not None and cursor["mode"] == "after"
        size = stat.st_size if cursor is None or live else cursor["size"]
        digest = _digest(stream, size)
        upper = cursor.get("upper", size) if cursor and not live else size
        lower = cursor["size"] if live else (cursor.get("lower", 0) if cursor and cursor["mode"] == "window" else 0)
        window_limit = cursor.get("window_limit", limit) if cursor and cursor["mode"] == "window" else limit
        if not isinstance(window_limit, int) or not 1 <= window_limit <= 100:
            raise ValueError("Invalid output window cursor.")
        selected = deque(maxlen=window_limit)
        forward = live or bool(cursor and cursor.get("forward"))
        forward_more = None
        earlier = False
        message_count = 0
        for ref in _scan(stream, size, session_id, issues, signals, status, include_tools):
            message_count += 1
            if ref["anchor"] < lower:
                if not forward:
                    earlier = True
                continue
            if ref["anchor"] >= upper:
                continue
            if len(selected) == window_limit:
                if forward:
                    if forward_more is None:
                        forward_more = ref["anchor"]
                    continue
                earlier = True
            selected.append(ref)
        refs = list(selected)
        begin = cursor.get("next", refs[0]["anchor"] if refs else upper) if cursor and cursor["mode"] == "window" else (refs[0]["anchor"] if refs else upper)
        char_offset = cursor.get("char", 0) if cursor and cursor["mode"] == "window" else 0
        outputs = []
        budget = PAGE_BYTES
        continuation = None
        for ref in refs:
            if ref["anchor"] < begin:
                continue
            message = _load_message(stream, ref, include_tools)
            start = char_offset if ref["anchor"] == begin else 0
            if not isinstance(start, int) or start < 0 or start >= ref["length"]:
                raise ValueError("Invalid message chunk cursor.")
            end = min(ref["length"], start + CHUNK_CHARS)
            if budget <= 0 or len(outputs) >= limit:
                continuation = (ref["anchor"], start)
                break
            fragment = message["text"][start:end]
            encoded_fragment = fragment.encode("utf-8")
            if len(encoded_fragment) > budget:
                fragment = encoded_fragment[:budget].decode("utf-8", errors="ignore")
                end = start + len(fragment)
            if not fragment:
                continuation = (ref["anchor"], start)
                break
            outputs.append({"role": message["role"], "time": message["time"],
                            "channel": message["channel"], "text": fragment,
                            "message_id": ref["message_id"], "byte_offset": ref["anchor"],
                            "chunk_offset": start, "total_chars": ref["length"],
                            "content_complete": start == 0 and end == ref["length"],
                            "has_more_content": end < ref["length"],
                            **{name: message[name] for name in ("tool_type", "name", "call_id") if name in message}})
            budget -= len(fragment.encode("utf-8"))
            if end < ref["length"]:
                continuation = (ref["anchor"], end)
                break
        base = {"version": CURSOR_VERSION, "path": str(path), "session_id": session_id,
                "dev": stat.st_dev, "ino": stat.st_ino, "size": size, "digest": digest,
                "include_tools": include_tools}
        if continuation:
            next_state = dict(base, mode="window", lower=refs[0]["anchor"], upper=upper,
                              next=continuation[0], char=continuation[1], forward=forward,
                              window_limit=window_limit)
        elif forward_more is not None:
            next_state = dict(base, mode="window", lower=forward_more, upper=upper,
                              next=forward_more, char=0, forward=True, window_limit=limit)
        elif earlier and refs:
            next_state = dict(base, mode="older", upper=refs[0]["anchor"])
        else:
            next_state = None
        # Detect mutation during extraction, including pathname replacement.
        current = path.stat()
        if current.st_dev != stat.st_dev or current.st_ino != stat.st_ino or _digest(stream, size) != digest:
            raise ValueError("Rollout changed while reading; retry without cursor.")
        complete_end = issues.pop("complete_end")
        after_state = dict(base, mode="after", size=complete_end, digest=_digest(stream, complete_end))
    content_complete = all(item["content_complete"] for item in outputs)
    snapshot_complete = not any(issues.values())
    entire_history = (not cursor and not next_state and len(outputs) == message_count and snapshot_complete)
    return {"id": session_id, "source": "codex_rollout", "cwd": status.get("cwd"),
            "messages": outputs, "recent_signals": list(signals), "include_tools": include_tools,
            "signals_complete": status["signal_count"] <= SIGNAL_ITEMS and all(s["text_complete"] for s in signals),
            "status": status["status"], "status_source": "persisted_rollout_not_live_process",
            "next_cursor": _encode_cursor(next_state) if next_state else None,
            "after_cursor": _encode_cursor(after_state), "has_more": bool(next_state),
            "truncated": bool(next_state) or not snapshot_complete or not content_complete,
            "content_complete": content_complete, "history_complete": entire_history,
            "snapshot_complete": snapshot_complete, "snapshot_size": size,
            "snapshot_message_count": message_count, "read_issues": issues,
            "note": "Selected-session persisted output, not terminal pixels. Cursors paginate without cutting reports off. History status is not live-process or task-acceptance evidence."}


if __name__ == "__main__":
    try:
        request = json.loads(base64.urlsafe_b64decode(sys.argv[1]))
        print(json.dumps({"ok": True, "result": main(request)}, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
