"""Isolated management contracts; never launch Codex, SSH, or real workers."""
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


class FakeRpc:
    closed = False

    def __init__(self, fail_send=False):
        self.calls = []
        self.created = 0
        self.fail_send = fail_send

    def call(self, method, params):
        self.calls.append((method, params))
        if method == "model/list":
            return {"data": [{"id": "fake-model", "model": "fake-model", "isDefault": True}]}
        if method == "thread/start":
            self.created += 1
            return {"thread": {"id": f"new-{self.created}", "turns": [], "status": {"type": "idle"}}}
        if method == "turn/start":
            if self.fail_send:
                raise TimeoutError("write response lost")
            return {"turn": {"id": f"turn-{len(self.calls)}"}}
        if method == "thread/name/set":
            return {}
        raise AssertionError(f"Unexpected fake RPC operation: {method}")


class ManagementBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.hub = Hub(self.temp.name)
        self.hub.hosts = {"local": {"transport": "local", "label": "Local", "live": False},
                          "316": {"transport": "ssh", "target": "fake-server", "label": "Server", "live": False}}
        self.history_calls = []

        def history(host, request):
            self.history_calls.append((host, request))
            return {"id": request.get("thread_id"), "title": "工作对话", "messages": [{"text": "已完成检查"}]}

        self.hub.history = history

    def tearDown(self):
        self.hub.close()
        self.temp.cleanup()

    def register(self, thread="thread-1", active=True, access="control", rpc=None):
        self.hub.store.remember("local", thread, "Work", active=active, access=access)
        if rpc:
            self.hub.owners[("local", thread)] = rpc
        return f"local::{thread}"

    def plugin_send(self, agent="local::thread-1", text="请继续修这个 bug。", key="action-1"):
        return self.hub.plugin_call("send_agent_message", {"agent_id": agent, "text": text, "request_key": key})

    def test_startup_does_not_discover_resume_or_launch_a_model(self):
        self.hub.close()
        with patch("legacy_appserver_hub.Rpc", side_effect=AssertionError("no model process")), \
             patch("legacy_appserver_hub.subprocess.run", side_effect=AssertionError("no SSH scan")), \
             patch("legacy_appserver_hub.catalog.main", side_effect=AssertionError("no history scan")):
            self.hub = Hub(self.temp.name)
            self.assertEqual(self.hub.servers, {})
            self.assertEqual(self.hub.live_servers, {})
            self.assertEqual(self.hub.owners, {})
            self.assertEqual(self.hub.plugin_call("list_active_agents", {})["agents"], [])

    def test_inactive_agents_are_hidden_and_cannot_be_read_or_sent_by_plugin(self):
        rpc = FakeRpc()
        agent = self.register(active=False, rpc=rpc)
        self.assertEqual(self.hub.plugin_call("list_active_agents", {})["agents"], [])
        for tool in ("get_agent", "read_agent"):
            with self.subTest(tool=tool), self.assertRaises(PermissionError):
                self.hub.plugin_call(tool, {"agent_id": agent})
        with self.assertRaises(PermissionError):
            self.plugin_send(agent)
        self.assertEqual(self.history_calls, [])
        self.assertEqual(rpc.calls, [])
        self.assertEqual(self.hub.store.outbox(), [])
        # Activation limits the plugin, not direct human access in the UI.
        self.assertEqual(self.hub.read_agent("local", "thread-1")["messages"][0]["text"], "已完成检查")

    def test_observe_access_allows_history_but_disallows_ui_and_plugin_writes(self):
        rpc = FakeRpc()
        agent = self.register(access="observe", rpc=rpc)
        self.assertEqual(self.hub.plugin_call("read_agent", {"agent_id": agent})["access"], "observe")
        with self.assertRaises(PermissionError):
            self.plugin_send(agent)
        with self.assertRaises(PermissionError):
            self.hub.send("local", "thread-1", "做修改")
        self.assertEqual(rpc.calls, [])
        self.assertEqual(self.hub.store.outbox(), [])

    def test_send_deduplicates_replay_and_rejects_reusing_a_key_for_different_text(self):
        rpc = FakeRpc()
        self.register(rpc=rpc)
        first = self.plugin_send()
        replay = self.plugin_send()
        self.assertEqual(first["id"], replay["id"])
        self.assertEqual(first["state"], "submitted")
        self.assertEqual(len(rpc.calls), 1)
        self.assertEqual(rpc.calls[0][1]["input"], [{"type": "text", "text": "请继续修这个 bug。"}])
        with self.assertRaises(ValueError):
            self.plugin_send(text="完全不同的任务")
        self.assertEqual(len(rpc.calls), 1)

    def test_concurrent_replays_submit_exactly_one_worker_message(self):
        rpc = FakeRpc()
        self.register(rpc=rpc)
        barrier = threading.Barrier(5)
        results, errors = [], []

        def send():
            try:
                barrier.wait(timeout=2)
                results.append(self.plugin_send())
            except Exception as error:
                errors.append(error)

        workers = [threading.Thread(target=send, daemon=True) for _ in range(4)]
        for worker in workers:
            worker.start()
        barrier.wait(timeout=2)
        for worker in workers:
            worker.join(timeout=3)
        self.assertTrue(all(not worker.is_alive() for worker in workers), "concurrent send deadlocked")
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 4)
        self.assertEqual(len({row["id"] for row in results}), 1)
        self.assertEqual(len(rpc.calls), 1)

    def test_message_replay_after_restart_does_not_resume_or_resubmit(self):
        rpc = FakeRpc()
        self.register(rpc=rpc)
        first = self.plugin_send()
        self.hub.close()
        self.hub = Hub(self.temp.name)
        self.hub.server = lambda *args, **kwargs: self.fail("replay must not resume a worker")
        replay = self.plugin_send()
        self.assertEqual(replay["id"], first["id"])
        self.assertEqual(replay["state"], "submitted")
        self.assertEqual(self.hub.owners, {})
        self.assertEqual(len(rpc.calls), 1)

    def test_uncertain_send_is_durable_and_never_retries_worker_on_replay(self):
        rpc = FakeRpc(fail_send=True)
        self.register(rpc=rpc)
        with self.assertRaises(TimeoutError):
            self.plugin_send()
        replay = self.plugin_send()
        self.assertEqual(replay["state"], "unknown")
        self.assertEqual(len(rpc.calls), 1)
        self.assertEqual(self.hub.plugin_call("get_delivery", {"request_id": replay["id"]})["state"], "unknown")

    def test_deactivation_revokes_only_unsent_plugin_queue_and_ui_still_controls(self):
        rpc = FakeRpc()
        self.register(rpc=rpc)
        self.hub.active[("local", "thread-1")] = "busy-turn"
        delegated = self.plugin_send()
        direct = self.hub.send("local", "thread-1", "人类直接发出的任务", request_key="ui-1")
        self.assertEqual(delegated["state"], "queued")
        self.assertEqual(direct["state"], "queued")
        self.hub.activate_agent("local", "thread-1", False)
        self.assertEqual(self.hub.store.delivery(delegated["id"])["state"], "scope_revoked")
        self.assertEqual(self.hub.store.delivery(direct["id"])["state"], "queued")
        self.hub.active.clear()
        self.hub.flush("local", "thread-1", rpc)
        self.assertEqual(rpc.calls[0][1]["input"], [{"type": "text", "text": "人类直接发出的任务"}])
        self.assertEqual(self.hub.store.delivery(direct["id"])["state"], "submitted")
        with self.assertRaises(PermissionError):
            self.hub.plugin_call("get_delivery", {"request_id": delegated["id"]})

    def test_plugin_creation_requires_ui_connected_host_and_is_idempotent(self):
        rpc = FakeRpc()
        self.hub.server = lambda *args, **kwargs: rpc
        args = {"host": "316", "cwd": "/workspace/project", "title": "Bugfix", "request_key": "create-1"}
        with self.assertRaises(PermissionError):
            self.hub.plugin_call("create_agent", args)
        self.assertEqual(rpc.calls, [])
        self.hub.connections["316"] = {"connected": True}
        first = self.hub.plugin_call("create_agent", args)
        replay = self.hub.plugin_call("create_agent", args)
        self.assertEqual(first, replay)
        self.assertEqual(first["agent_id"], "316::new-1")
        self.assertTrue(first["active"])
        self.assertEqual(first["access"], "control")
        self.assertEqual(rpc.created, 1)
        with self.assertRaises(ValueError):
            self.hub.plugin_call("create_agent", {**args, "title": "Different"})
        self.assertEqual(rpc.created, 1)

    def test_create_replay_preserves_uncertain_initial_message_delivery(self):
        rpc = FakeRpc(fail_send=True)
        self.hub.server = lambda *args, **kwargs: rpc
        args = {"host": "local", "cwd": "C:/workspace/project", "title": "Bugfix", "text": "请处理问题", "request_key": "create-with-message"}
        with self.assertRaises(TimeoutError):
            self.hub.plugin_call("create_agent", args)
        replay = self.hub.plugin_call("create_agent", args)
        self.assertEqual(rpc.created, 1)
        self.assertEqual(replay["delivery"]["state"], "unknown")
        self.assertEqual(sum(method == "turn/start" for method, _ in rpc.calls), 1)

    def test_completion_updates_old_submission_even_after_more_than_100_new_rows(self):
        rpc = FakeRpc()
        self.register(rpc=rpc)
        delivery = self.plugin_send()
        for index in range(105):
            self.hub.store.enqueue("local", "other-thread", "other message", request_key=f"later-{index}")
        self.hub.stopping = True  # Completion need not schedule a real/background flush in this test.
        try:
            self.hub.worker_event("local", rpc, {"method": "turn/completed", "params": {"threadId": "thread-1", "turn": {"id": delivery["turn_id"], "status": "completed"}}})
        finally:
            self.hub.stopping = False
        self.assertEqual(self.hub.store.delivery(delivery["id"])["state"], "completed")

    def test_event_poll_filters_inactive_agents_and_retains_a_forward_cursor(self):
        self.register("active-thread", active=True)
        self.register("inactive-thread", active=False)
        self.hub.record_event("host_connected", host="316", connected=True)
        self.hub.record_event("worker_completed", host="local", thread_id="inactive-thread", turn={"id": "secret-turn"})
        self.hub.record_event("worker_completed", host="local", thread_id="active-thread", turn={"id": "visible-turn"})
        result = self.hub.plugin_call("poll_events", {})
        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(result["events"][0]["data"]["thread_id"], "active-thread")
        self.assertEqual(self.hub.plugin_call("poll_events", {"after": result["cursor"]})["events"], [])


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
