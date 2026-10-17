"""Read-only, portable Codex history inspection. Also executes over SSH stdin."""
from __future__ import annotations

import base64
import json
import os
import sqlite3
import sys
from collections import deque
from contextlib import closing
from pathlib import Path


def home_path():
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).resolve()


def catalog(limit=1000):
    home = home_path()
    databases = sorted(home.glob("state_*.sqlite"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not databases:
        return _scan(home, limit)
    with closing(sqlite3.connect(databases[0].as_uri() + "?mode=ro", uri=True, timeout=3)) as conn:
        conn.row_factory = sqlite3.Row
        cols = {row[1] for row in conn.execute("pragma table_info(threads)")}
        selected = [c for c in ("id", "rollout_path", "cwd", "title", "name", "updated_at", "source", "archived", "has_user_event") if c in cols]
        return [dict(row) for row in conn.execute(
            "select " + ",".join(selected) + " from threads where archived=0 order by updated_at desc limit ?",
            (limit,),
        )]


def _scan(home, limit):
    rows = []
    for path in sorted((home / "sessions").rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            with path.open(encoding="utf-8") as stream:
                first = json.loads(stream.readline()).get("payload", {})
            if first.get("id"):
                rows.append({"id": first["id"], "cwd": first.get("cwd", ""), "title": "",
                             "rollout_path": str(path), "updated_at": path.stat().st_mtime, "source": first.get("source", "unknown")})
        except (OSError, ValueError):
            continue
    return rows


def _database():
    paths = sorted(home_path().glob("state_*.sqlite"), key=lambda p: p.stat().st_mtime, reverse=True)
    return paths[0] if paths else None


def _select(where, params, limit):
    database = _database()
    if not database:
        return None
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=3)) as conn:
        conn.row_factory = sqlite3.Row
        cols = {row[1] for row in conn.execute("pragma table_info(threads)")}
        selected = [c for c in ("id", "rollout_path", "cwd", "title", "name", "updated_at", "source", "archived", "has_user_event") if c in cols]
        clause, values = where(cols)
        return [dict(row) for row in conn.execute(
            "select " + ",".join(selected) + " from threads where " + clause + " order by updated_at desc limit ?",
            tuple(values) + (limit,))]


def find(query="", cwd="", limit=20):
    words = query.casefold().split()
    maximum = min(100, max(1, int(limit)))
    def where(cols):
        clauses, values = ["archived=0"], []
        for word in words:
            fields = [c for c in ("title", "name", "cwd", "id") if c in cols]
            clauses.append("(" + " or ".join(f"lower(coalesce({c},'')) like ? escape '\\'" for c in fields) + ")")
            escaped = word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            values.extend(["%" + escaped + "%"] * len(fields))
        if cwd:
            clauses.append("lower(cwd) like ? escape '\\'")
            values.append("%" + cwd.casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        if "source" in cols:
            clauses.append("lower(coalesce(source,'')) not like '%subagent%'")
        return " and ".join(clauses), values
    rows = _select(where, (), maximum)
    indexed = rows is not None
    if rows is None:
        rows = catalog()
    results = []
    for row in rows:
        source = str(row.get("source", "")).casefold()
        if "subagent" in source:
            continue  # A worker's own descendants are not the CEO's inventory.
        haystack = " ".join(str(row.get(k, "")) for k in ("title", "name", "cwd", "id")).casefold()
        if words and not all(word in haystack for word in words):
            continue
        if cwd and cwd.casefold() not in str(row.get("cwd", "")).casefold():
            continue
        row["title"] = row.get("name") or row.get("title") or row["id"]
        row.pop("rollout_path", None)
        row["status"] = "stored_history_only"
        results.append(row)
        if len(results) >= maximum:
            break
    return {"threads": results, "status_note": "列表来自持久化历史；不代表进程仍存活。", "indexed": indexed}


def tail_records(path, byte_limit=1024 * 1024):
    with path.open("rb") as stream:
        size = stream.seek(0, 2)
        offset = max(0, size - byte_limit)
        stream.seek(offset)
        if offset:
            stream.readline()
        records = []
        for line in stream:
            try:
                records.append(json.loads(line))
            except ValueError:
                pass  # A live writer may not have completed its last line.
    return records, offset > 0


def read(thread_id, limit=18):
    rows = _select(lambda cols: ("id=? and archived=0", [thread_id]), (), 1)
    row = rows[0] if rows else (next((r for r in catalog(100000) if r["id"] == thread_id), None) if rows is None else None)
    if row is None:
        raise ValueError("未找到这个对话；必须使用查询返回的精确 ID。")
    if row.get("has_user_event") == 0 and (not row.get("rollout_path") or not Path(row["rollout_path"]).exists()):
        return {"id": thread_id, "title": row.get("name") or row.get("title") or thread_id, "cwd": row.get("cwd"),
                "messages": [], "recent_signals": [], "status": "no_messages_yet", "status_source": "persisted_thread_metadata",
                "note": "尚未记录第一条消息；进程运行情况需以控制连接为准。"}
    path = Path(row["rollout_path"]).resolve()
    home = home_path()
    if path.suffix != ".jsonl" or not any(path.is_relative_to(home / directory) for directory in ("sessions", "archived_sessions")):
        raise ValueError("拒绝读取对话日志目录之外的文件。")
    records, truncated = tail_records(path)
    messages = deque(maxlen=min(80, max(1, limit)))
    signals = deque(maxlen=12)
    inferred = "unknown"
    for event in records:
        payload = event.get("payload", {})
        kind = payload.get("type")
        if event.get("type") == "event_msg":
            if kind in ("task_started", "turn_started"):
                inferred = "working_in_history"
            elif kind in ("task_complete", "turn_complete", "turn_completed"):
                inferred = "completed_in_history"
            elif kind in ("turn_aborted", "task_aborted"):
                inferred = "interrupted_in_history"
            if kind in ("task_started", "task_complete", "turn_aborted", "agent_message"):
                signals.append({"time": event.get("timestamp"), "type": kind,
                                "text": str(payload.get("message", payload.get("last_agent_message", "")))[:2500]})
        if event.get("type") == "response_item":
            if kind == "message" and payload.get("role") in ("user", "assistant"):
                text = "\n".join(c.get("text", "") for c in payload.get("content", [])
                                 if isinstance(c, dict) and c.get("type") in ("input_text", "output_text", "text"))
                if text and not text.startswith(("<environment_context>", "<permissions instructions>", "# AGENTS.md")):
                    messages.append({"role": payload["role"], "time": event.get("timestamp"), "text": text[:6000]})
            elif kind in ("function_call_output", "custom_tool_call_output"):
                output = payload.get("output", "")
                if not isinstance(output, str):
                    output = json.dumps(output, ensure_ascii=False)
                signals.append({"time": event.get("timestamp"), "type": "tool_output", "text": output[-2500:]})
    return {"id": thread_id, "title": row.get("name") or row.get("title") or thread_id,
            "cwd": row.get("cwd"), "updated_at": path.stat().st_mtime,
            "status": inferred, "status_source": "persisted_rollout_not_live_process",
            "messages": list(messages), "recent_signals": list(signals), "truncated": truncated,
            "note": "working/completed 是历史记录中的信号，不能证明当前进程存活，也不能证明任务验收通过。"}


def main(request):
    if request["op"] == "bind_output":
        if __package__:
            from . import session_binding
        else:
            import session_binding
        return session_binding.main(request)
    if request["op"] == "resolve_output":
        if __package__:
            from . import session_binding
        else:
            import session_binding
        return session_binding.resolve_session(request["session_id"], request["codex_home"])
    if request["op"] == "read_output":
        if __package__:
            from . import conversation_output
        else:
            import conversation_output
        return conversation_output.main(request)
    if request["op"] == "find":
        return find(request.get("query", ""), request.get("cwd", ""), request.get("limit", 20))
    if request["op"] == "read":
        return read(request["thread_id"], request.get("limit", 18))
    if request["op"] == "probe":
        import shutil
        return {"home": str(home_path()), "codex_available": bool(shutil.which("codex")), "history_exists": home_path().exists()}
    raise ValueError("Unknown catalog operation")


if __name__ == "__main__":
    try:
        request = json.loads(base64.urlsafe_b64decode(sys.argv[1]))
        print(json.dumps({"ok": True, "result": main(request)}, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
