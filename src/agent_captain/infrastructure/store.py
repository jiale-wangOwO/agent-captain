from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.db:
            self.db.executescript("""
            create table if not exists settings (key text primary key, value text);
            create table if not exists events (id integer primary key, time real, kind text, data text);
            create table if not exists agents (host text, thread_id text, alias text, owned integer default 0,
                primary key(host,thread_id));
            create table if not exists outbox (id text primary key, host text, thread_id text, text text,
                state text, turn_id text, error text, time real);
            create table if not exists tool_results (call_id text primary key, data text);
            """)
            columns = {r[1] for r in self.db.execute("pragma table_info(agents)")}
            for name, declaration in (("active", "integer default 0"), ("access", "text default 'observe'"),
                                      ("mode", "text default 'history'"), ("terminal_id", "text"), ("cwd", "text default ''"),
                                      ("pinned", "integer default 0")):
                if name not in columns:
                    self.db.execute(f"alter table agents add column {name} {declaration}")
            outbox_columns = {r[1] for r in self.db.execute("pragma table_info(outbox)")}
            if "origin" not in outbox_columns:
                self.db.execute("alter table outbox add column origin text default 'legacy'")
            if "request_key" not in outbox_columns:
                self.db.execute("alter table outbox add column request_key text")
            if "kind" not in outbox_columns:
                self.db.execute("alter table outbox add column kind text default 'message'")
            self.db.execute("create unique index if not exists outbox_request_key on outbox(request_key) where request_key is not null")
            self.db.execute("create table if not exists operations(request_key text primary key, fingerprint text, state text, result text)")

    def event(self, kind, **data):
        with self.lock, self.db:
            self.db.execute("insert into events(time,kind,data) values(?,?,?)", (time.time(), kind, json.dumps(data, ensure_ascii=False)))

    def events(self, after=0, limit=400, kinds=None):
        with self.lock:
            if kinds:
                placeholders = ",".join("?" for _ in kinds)
                rows = self.db.execute("select * from events where id>? and kind in (" + placeholders + ") order by id limit ?", (after, *kinds, limit)).fetchall()
            else:
                rows = self.db.execute("select * from events where id>? order by id limit ?", (after, limit)).fetchall()
        return [dict(r) | {"data": json.loads(r["data"])} for r in rows]

    def recent_events(self, limit=400, kinds=None):
        """Recent UI tail in ascending cursor order; durable history remains."""
        with self.lock:
            if kinds:
                placeholders = ",".join("?" for _ in kinds)
                rows = self.db.execute("select * from events where kind in (" + placeholders + ") order by id desc limit ?", (*kinds, limit)).fetchall()
            else:
                rows = self.db.execute("select * from events order by id desc limit ?", (limit,)).fetchall()
        return [dict(row) | {"data": json.loads(row["data"])} for row in reversed(rows)]

    def event_cursor(self):
        with self.lock:
            return self.db.execute("select coalesce(max(id),0) from events").fetchone()[0]

    def setting(self, key, value=None):
        with self.lock, self.db:
            if value is not None:
                self.db.execute("insert or replace into settings values(?,?)", (key, json.dumps(value)))
            row = self.db.execute("select value from settings where key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def remember(self, host, thread_id, alias, owned=False, active=None, access=None, mode=None, terminal_id=None, cwd=None):
        with self.lock, self.db:
            self.db.execute("insert into agents(host,thread_id,alias,owned) values(?,?,?,?) on conflict(host,thread_id) do update set alias=excluded.alias, owned=max(agents.owned,excluded.owned)",
                            (host, thread_id, alias, int(owned)))
            if active is not None:
                self.db.execute("update agents set active=? where host=? and thread_id=?", (int(active), host, thread_id))
            if access is not None:
                self.db.execute("update agents set access=? where host=? and thread_id=?", (access, host, thread_id))
            for name, value in (("mode", mode), ("terminal_id", terminal_id), ("cwd", cwd)):
                if value is not None:
                    self.db.execute(f"update agents set {name}=? where host=? and thread_id=?", (value, host, thread_id))
        return self.agent(host, thread_id)

    def agent(self, host, thread_id):
        with self.lock:
            row = self.db.execute("select * from agents where host=? and thread_id=?", (host, thread_id)).fetchone()
            return dict(row) if row else None

    def set_active(self, host, thread_id, active):
        with self.lock, self.db:
            self.db.execute("update agents set active=? where host=? and thread_id=?", (int(active), host, thread_id))
        return self.agent(host, thread_id)

    def update_agent_metadata(self, host, thread_id, alias=None, pinned=None):
        """Change presentation only; never activation, terminal or output identity."""
        values = {}
        if alias is not None:
            values["alias"] = alias
        if pinned is not None:
            values["pinned"] = int(pinned)
        with self.lock, self.db:
            if not self.db.execute("select 1 from agents where host=? and thread_id=?", (host, thread_id)).fetchone():
                raise ValueError("未登记的工作终端。")
            if values:
                self.db.execute("update agents set " + ",".join(key + "=?" for key in values) + " where host=? and thread_id=?",
                                (*values.values(), host, thread_id))
        return self.agent(host, thread_id)

    def agents(self):
        with self.lock:
            return [dict(r) for r in self.db.execute("select * from agents")]

    def enqueue(self, host, thread_id, text, origin="ui", request_key=None, kind="message"):
        request_id = str(uuid.uuid4())
        with self.lock, self.db:
            if request_key:
                previous = self.db.execute("select * from outbox where request_key=?", (request_key,)).fetchone()
                if previous:
                    if (previous["host"], previous["thread_id"], previous["text"], previous["origin"], previous["kind"]) != (host, thread_id, text, origin, kind):
                        raise ValueError("同一个 request_key 不能用于不同消息。")
                    return previous["id"]
            self.db.execute("insert into outbox(id,host,thread_id,text,state,time,origin,request_key,kind) values(?,?,?,?,?,?,?,?,?)",
                            (request_id, host, thread_id, text, "queued", time.time(), origin, request_key, kind))
        return request_id

    def delivery(self, request_id):
        with self.lock:
            row = self.db.execute("select * from outbox where id=?", (request_id,)).fetchone()
            return dict(row) if row else None

    def begin_operation(self, request_key, fingerprint):
        with self.lock, self.db:
            row = self.db.execute("select * from operations where request_key=?", (request_key,)).fetchone()
            if row:
                if row["fingerprint"] != fingerprint:
                    raise ValueError("同一个 request_key 不能用于不同创建请求。")
                if row["state"] != "done":
                    raise RuntimeError("原创建请求结果尚不确定；先查看已登记 agent，不能重复创建。")
                return json.loads(row["result"])
            self.db.execute("insert into operations values(?,?,?,null)", (request_key, fingerprint, "started"))
            return None

    def finish_operation(self, request_key, result):
        with self.lock, self.db:
            self.db.execute("update operations set state='done',result=? where request_key=?", (json.dumps(result, ensure_ascii=False), request_key))

    def update_outbox(self, request_id, state, turn_id=None, error=None):
        with self.lock, self.db:
            self.db.execute("update outbox set state=?,turn_id=coalesce(?,turn_id),error=? where id=?", (state, turn_id, error, request_id))

    def complete_turn(self, host, thread_id, turn_id, state, error=None):
        with self.lock, self.db:
            self.db.execute("update outbox set state=?,error=? where host=? and thread_id=? and turn_id=?",
                            (state, error, host, thread_id, turn_id))

    def revoke_pending(self, host, thread_id, states):
        with self.lock, self.db:
            placeholders = ",".join("?" for _ in states)
            self.db.execute("update outbox set state='scope_revoked',error=? where host=? and thread_id=? and origin='plugin' and state in (" + placeholders + ")",
                            ("已移出 CEO 管理范围；未提交的输入不会执行。", host, thread_id, *states))

    def fence_pending_policy_upgrade(self, agents, states, migration_key, reason):
        """Once-only durable not-sent fence, with an audit for each old input.

        This runs before new automatic management becomes usable. It retains
        delivery IDs/keys/text and terminal metadata, never calls a transport,
        and leaves submitted, unknown and previously cancelled rows intact.
        """
        with self.lock, self.db:
            if self.db.execute("select 1 from settings where key=?", (migration_key,)).fetchone():
                return []
            fenced = []
            placeholders = ",".join("?" for _ in states)
            for host, thread_id in agents:
                rows = self.db.execute("select id from outbox where host=? and thread_id=? and origin='plugin' and state in ("
                    + placeholders + ")", (host, thread_id, *states)).fetchall()
                for row in rows:
                    self.db.execute("update outbox set state='not_sent',error=? where id=?", (reason, row["id"]))
                    self.db.execute("insert into events(time,kind,data) values(?,?,?)", (time.time(), "management_policy_input_fenced",
                        json.dumps({"host": host, "thread_id": thread_id, "request_id": row["id"], "policy": "connected_workers",
                                    "state": "not_sent", "reason": "previously_inactive"}, ensure_ascii=False)))
                    fenced.append(row["id"])
            self.db.execute("insert into settings(key,value) values(?,?)", (migration_key, json.dumps({"version": 1, "fenced_count": len(fenced)})))
            return fenced

    def outbox(self, state=None):
        with self.lock:
            if state:
                rows = self.db.execute("select * from outbox where state=? order by time", (state,)).fetchall()
            else:
                rows = self.db.execute("select * from outbox order by time desc limit 100").fetchall()
            return [dict(r) for r in rows]

    def cached_tool(self, call_id, result=None):
        with self.lock, self.db:
            if result is not None:
                self.db.execute("insert or replace into tool_results values(?,?)", (call_id, json.dumps(result, ensure_ascii=False)))
            row = self.db.execute("select data from tool_results where call_id=?", (call_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def close(self):
        with self.lock:
            self.db.close()
