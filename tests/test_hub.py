import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import catalog
from legacy_appserver_hub import LegacyAppServerHub as Hub
from rpc import Rpc
from store import Store


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.patch = patch.dict(os.environ, {"CODEX_HOME": str(self.home)})
        self.patch.start()
        self.rollout = self.home / "sessions" / "sample.jsonl"
        self.rollout.parent.mkdir()
        self.rollout.write_text("", encoding="utf-8")
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db, db:
            db.execute("create table threads(id text,rollout_path text,cwd text,title text,updated_at integer,source text,archived integer)")
            db.execute("insert into threads values(?,?,?,?,?,?,?)", ("thread-1", str(self.rollout), "/workspace/slides", "汇报 PPT", 1, "appServer", 0))

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def append(self, record):
        with self.rollout.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    def test_search_finds_desktop_source_by_title_and_path_without_modifying_database(self):
        before = (self.home / "state_5.sqlite").read_bytes()
        self.assertEqual(catalog.find("PPT", "/workspace")["threads"][0]["id"], "thread-1")
        self.assertEqual((self.home / "state_5.sqlite").read_bytes(), before)

    def test_history_completion_is_not_claimed_as_live_process(self):
        self.append({"type": "event_msg", "payload": {"type": "task_started"}})
        self.append({"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "已生成幻灯片，待检查。"}]}})
        self.append({"type": "event_msg", "payload": {"type": "task_complete"}})
        with self.rollout.open("a", encoding="utf-8") as stream:
            stream.write('{"type":')
        data = catalog.read("thread-1")
        self.assertEqual(data["status"], "completed_in_history")
        self.assertNotIn("live_status", data)
        self.assertEqual(data["messages"][-1]["text"], "已生成幻灯片，待检查。")

    def test_refuses_database_paths_outside_codex_home(self):
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db, db:
            db.execute("update threads set rollout_path=?", (str(self.home.parent / "auth.json"),))
        with self.assertRaisesRegex(ValueError, "之外"):
            catalog.read("thread-1")

    def test_refuses_auth_file_even_inside_codex_home(self):
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db, db:
            db.execute("update threads set rollout_path=?", (str(self.home / "auth.json"),))
        with self.assertRaisesRegex(ValueError, "之外"):
            catalog.read("thread-1")

    def test_tail_preserves_utf8_and_marks_truncation(self):
        self.append({"type": "ignored", "text": "填充" * 1000})
        self.append({"type": "message", "payload": {"text": "最新中文"}})
        rows, truncated = catalog.tail_records(self.rollout, byte_limit=300)
        self.assertTrue(truncated)
        self.assertEqual(rows[-1]["payload"]["text"], "最新中文")

    def test_ceo_inventory_excludes_workers_internal_subagents(self):
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db, db:
            db.execute("insert into threads values(?,?,?,?,?,?,?)", ("internal", str(self.rollout), "/workspace/slides", "PPT internal", 2, '{"subagent":{"depth":1}}', 0))
        self.assertEqual([r["id"] for r in catalog.find("PPT")["threads"]], ["thread-1"])

    def test_new_empty_thread_has_readable_metadata_without_loading_a_model(self):
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db, db:
            db.execute("alter table threads add column has_user_event integer")
            db.execute("update threads set has_user_event=0,rollout_path='' where id='thread-1'")
        result = catalog.read("thread-1")
        self.assertEqual(result["status"], "no_messages_yet")
        self.assertEqual(result["messages"], [])


class FakeRpc:
    closed = False
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail
    def call(self, method, params):
        self.calls.append((method, params))
        if self.fail:
            raise TimeoutError("response lost")
        return {"turn": {"id": "turn-1"}}


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.hub = Hub(self.temp.name)
        self.hub.history = lambda *args: {"id": "thread-1"}
        self.hub.store.remember("local", "thread-1", "test", access="control")

    def tearDown(self):
        self.hub.close()
        self.temp.cleanup()

    def test_unmanaged_worker_is_never_automatically_resumed(self):
        self.hub.connected_owner = lambda *args: None
        self.hub.server = lambda *args, **kwargs: self.fail("must not start an unmanaged conversation")
        result = self.hub.send("local", "thread-1", "请检查进展")
        self.assertEqual(result["state"], "awaiting_connection")
        self.assertEqual(self.hub.store.outbox()[0]["state"], "awaiting_connection")

    def test_busy_worker_queues_message_and_later_receives_plain_user_text(self):
        rpc = FakeRpc()
        self.hub.connected_owner = lambda *args: rpc
        self.hub.active[("local", "thread-1")] = "busy-turn"
        self.hub.send("local", "thread-1", "请继续修复那个 bug。")
        self.assertEqual(rpc.calls, [])
        self.hub.active.clear()
        self.hub.flush("local", "thread-1", rpc)
        self.assertEqual(rpc.calls[0], ("turn/start", {"threadId": "thread-1", "input": [{"type": "text", "text": "请继续修复那个 bug。"}]}))
        self.assertEqual(self.hub.store.outbox()[0]["state"], "submitted")

    def test_uncertain_submission_is_persisted_and_never_automatically_retried(self):
        rpc = FakeRpc(fail=True)
        self.hub.connected_owner = lambda *args: rpc
        with self.assertRaises(TimeoutError):
            self.hub.send("local", "thread-1", "完成任务")
        self.assertEqual(self.hub.store.outbox()[0]["state"], "unknown")
        self.hub.flush("local", "thread-1", rpc)
        self.assertEqual(len(rpc.calls), 1)

    def test_tool_result_survives_restart_and_preserves_exact_call_id(self):
        self.hub.store.cached_tool("tool-123", {"success": True, "result": "已保存待发"})
        reopened = Store(Path(self.temp.name) / "hub.sqlite")
        self.assertEqual(reopened.cached_tool("tool-123")["result"], "已保存待发")
        reopened.db.close()


class RpcTests(unittest.TestCase):
    def test_interleaved_event_is_not_lost_while_waiting_for_response(self):
        source = """import sys,json
for line in sys.stdin:
 m=json.loads(line)
 if 'id' not in m: continue
 print(json.dumps({'method':'test/event','params':{'text':'中文'}}),flush=True)
 print(json.dumps({'id':m['id'],'result':{'ok':True}}),flush=True)
"""
        events = []
        received = threading.Event()
        def record(_, event):
            events.append(event)
            received.set()
        rpc = Rpc([sys.executable, "-u", "-c", source], on_event=record)
        try:
            self.assertTrue(rpc.call("test/read", {})["ok"])
            self.assertTrue(received.wait(1))
            self.assertTrue(any(e["params"]["text"] == "中文" for e in events))
        finally:
            rpc.close()


if __name__ == "__main__":
    unittest.main()
