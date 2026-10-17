"""History catalog contracts used by the terminal-first connection service."""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_captain.sessions import catalog


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


class IndexedHistoryTests(unittest.TestCase):
    def test_exact_thread_read_uses_database_without_catalog_or_filesystem_scan(self):
        with tempfile.TemporaryDirectory() as temporary:
            codex_home = Path(temporary)
            rollout = codex_home / "sessions" / "specific.jsonl"
            rollout.parent.mkdir()
            rollout.write_text(json.dumps({"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "目标对话的实际输出"}]}}) + "\n", encoding="utf-8")
            database = codex_home / "state_5.sqlite"
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute("create table threads(id text primary key,rollout_path text,cwd text,title text,updated_at integer,source text,archived integer)")
                connection.execute("insert into threads values(?,?,?,?,?,?,?)", ("wanted-id", str(rollout), "/workspace/project", "Wanted", 1, "appServer", 0))
                connection.executemany("insert into threads values(?,?,?,?,?,?,?)", [(f"other-{i}", "/should-not-be-read.jsonl", "/other", "Other", i + 2, "appServer", 0) for i in range(1100)])
            before = database.read_bytes()
            with patch.dict(os.environ, {"CODEX_HOME": temporary}), \
                 patch.object(catalog, "catalog", side_effect=AssertionError("no global catalog")), \
                 patch.object(catalog, "_scan", side_effect=AssertionError("no filesystem scan")):
                result = catalog.read("wanted-id")
                self.assertEqual(result["messages"][0]["text"], "目标对话的实际输出")
                with self.assertRaises(ValueError):
                    catalog.read("not-in-index")
            self.assertEqual(database.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
