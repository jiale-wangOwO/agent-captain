"""Deterministic per-route read admission; no SSH, sleeps, models or PTYs."""
import copy
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from agent_captain.core.terminal_hub import Hub, HistoryReadError
from tests.test_terminal_hub import FakeTerminals


class ReadAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.terminals = FakeTerminals()
        self.hub = Hub(Path(self.temp.name) / "hub", terminals=self.terminals)
        self.addCleanup(self.hub.close)
        self.hub._stop.set()
        self.hub._watcher.join(3)
        self.hub.hosts = {
            "remote": {"transport": "ssh", "target": "shared-fixture-route", "label": "Remote"},
            "alias": {"transport": "ssh", "target": "shared-fixture-route", "label": "Same target alias"},
            "other": {"transport": "ssh", "target": "independent-fixture-route", "label": "Other"},
            "local": {"transport": "local", "target": "", "label": "Local"},
        }
        self.identities = {}
        for index, host in enumerate(self.hub.hosts, 1):
            terminal = "ceohub-" + format(index, "024x")
            session = "11111111-2222-4333-8444-" + format(index, "012x")
            self.identities[host] = {"state": "bound", "binding_evidence": "held_rollout", "terminal_id": terminal,
                "session_id": session, "rollout_path": "/fixture/.codex/sessions/selected.jsonl", "codex_home": "/fixture/.codex",
                "process_identity": {"pid": 4200 + index, "start_ticks": index},
                "historical": False, "live_session_verified": True}
            self.terminals.add(terminal, host=host)
            self.hub.store.remember(host, "worker", "Fixture", mode="terminal", terminal_id=terminal)
            self.hub.store.setting(self.hub._output_key(host, terminal), copy.deepcopy(self.identities[host]))
        self.gates = {}
        self.history_calls = []
        self.calls_lock = threading.Lock()
        self.threads = []
        self.history_patch = patch.object(self.hub, "history", side_effect=self.history)
        self.history_patch.start()
        self.addCleanup(self.history_patch.stop)
        # Drain every held worker while its no-network history replacement is
        # still installed, including assertion-failure cleanup paths.
        self.addCleanup(self.stop_threads)

    def stop_threads(self):
        for gate in self.gates.values():
            gate["release"].set()
        for thread in self.threads:
            thread.join(5)
            self.assertFalse(thread.is_alive(), "Every fixture worker must terminate")

    def gate(self, host, entries=1):
        value = {"entered": [threading.Event() for _ in range(entries)], "release": threading.Event(), "count": 0, "error": None}
        self.gates[host] = value
        return value

    def history(self, host, request):
        with self.calls_lock:
            self.history_calls.append((host, copy.deepcopy(request)))
            gate = self.gates.get(host)
            entry = gate["count"] if gate else None
            if gate:
                gate["count"] += 1
        if gate and entry < len(gate["entered"]):
            gate["entered"][entry].set()
            if not gate["release"].wait(5):
                raise AssertionError("Test failed to release the held read")
            if gate["error"] is not None:
                raise gate["error"]
        binding = copy.deepcopy(self.identities[host])
        output = {"id": binding["session_id"], "source": "codex_rollout", "messages": [{"role": "assistant", "text": "FIXTURE OUTPUT"}],
                  "history_complete": True, "content_complete": True}
        if self.hub.host(host)["transport"] == "ssh":
            if request["op"] != "read_bound_output":
                raise AssertionError("SSH reads must remain one semantic transaction")
            return {"state": "bound", "binding": binding, "output": output}
        if request["op"] == "bind_output":
            return binding
        if request["op"] == "read_output":
            return output
        raise AssertionError("Unexpected fixture history operation")

    def start(self, host="remote", **options):
        result = {"done": threading.Event(), "value": None, "error": None}
        def run():
            try:
                result["value"] = self.hub.read_agent(host, "worker", **options)
            except BaseException as error:
                result["error"] = error
            finally:
                result["done"].set()
        thread = threading.Thread(target=run, name="read-admission-fixture", daemon=True)
        result["thread"] = thread
        self.threads.append(thread)
        thread.start()
        return result

    def finish(self, result):
        self.assertTrue(result["done"].wait(2), "An admitted fixture read should finish after release")
        result["thread"].join(2)
        self.assertFalse(result["thread"].is_alive())

    def assert_read_only(self):
        self.assertEqual(self.terminals.writes, [])
        self.assertFalse(any(call[0] in ("create", "write", "close") for call in self.terminals.calls))

    def test_same_target_busy_fresh_cursor_and_after_calls_return_without_more_work(self):
        gate = self.gate("remote")
        winner = self.start()
        self.assertTrue(gate["entered"][0].wait(2))
        terminal = self.identities["remote"]["terminal_id"]
        binding_key = self.hub._output_key("remote", terminal)
        saved = copy.deepcopy(self.hub.store.setting(binding_key))
        snapshots = sum(call[0] == "snapshot" for call in self.terminals.calls)
        for options in ({}, {"cursor": "private-original-page"}, {"after_cursor": "private-original-after"}):
            with self.subTest(options=options), patch.object(self.hub.store, "setting", wraps=self.hub.store.setting) as settings:
                blocked = self.start(**options)
                self.assertTrue(blocked["done"].wait(1), "The busy call must return while the winner remains blocked")
                self.assertFalse(winner["done"].is_set())
                self.assertIsInstance(blocked["error"], HistoryReadError)
                self.assertEqual(blocked["error"].reason_code, "ssh_read_in_progress")
                self.assertIn("原游标", str(blocked["error"]))
                self.assertIn("结束", str(blocked["error"]))
                self.assertNotIn("private-original", str(blocked["error"]))
                self.assertEqual(len(self.history_calls), 1)
                self.assertEqual(sum(call[0] == "snapshot" for call in self.terminals.calls), snapshots)
                self.assertFalse(any(len(call.args) > 1 for call in settings.call_args_list), "The rejected read cannot update any binding")
                self.assertEqual(self.hub.store.setting(binding_key), saved)
                self.assert_read_only()
        gate["release"].set()
        self.finish(winner)
        self.assertIsNone(winner["error"])

    def test_aliases_with_the_same_target_share_admission(self):
        gate = self.gate("remote")
        winner = self.start()
        self.assertTrue(gate["entered"][0].wait(2))
        blocked = self.start("alias", cursor="same-target-alias-page")
        self.assertTrue(blocked["done"].wait(1))
        self.assertIsInstance(blocked["error"], HistoryReadError)
        self.assertEqual(blocked["error"].reason_code, "ssh_read_in_progress")
        self.assertEqual([host for host, _ in self.history_calls], ["remote"])
        self.assertFalse(any(call[0] == "snapshot" and call[1] == self.identities["alias"]["terminal_id"] for call in self.terminals.calls))
        gate["release"].set()
        self.finish(winner)
        self.assert_read_only()

    def test_success_releases_admission_for_a_later_read(self):
        gate = self.gate("remote")
        winner = self.start()
        self.assertTrue(gate["entered"][0].wait(2))
        gate["release"].set()
        self.finish(winner)
        later = self.hub.read_agent("remote", "worker", after_cursor="unchanged-original-after")
        self.assertEqual(later["messages"][0]["text"], "FIXTURE OUTPUT")
        self.assertEqual(len(self.history_calls), 2)
        self.assertEqual(self.history_calls[-1][1]["after_cursor"], "unchanged-original-after")
        self.assert_read_only()

    def test_raised_cursor_failure_also_releases_admission_without_losing_the_cursor(self):
        gate = self.gate("remote")
        gate["error"] = HistoryReadError("ssh_timeout")
        winner = self.start(cursor="original-page-stays-selected")
        self.assertTrue(gate["entered"][0].wait(2))
        gate["release"].set()
        self.finish(winner)
        self.assertIsInstance(winner["error"], ValueError)
        gate["error"] = None
        later = self.hub.read_agent("remote", "worker", cursor="original-page-stays-selected")
        self.assertEqual(later["messages"][0]["text"], "FIXTURE OUTPUT")
        self.assertEqual([request["cursor"] for _, request in self.history_calls], ["original-page-stays-selected"] * 2)
        self.assert_read_only()

    def test_snapshot_exception_releases_admission_before_any_history(self):
        with patch.object(self.terminals, "snapshot", side_effect=RuntimeError("fixture snapshot failed")):
            with self.assertRaisesRegex(RuntimeError, "fixture snapshot failed"):
                self.hub.read_agent("remote", "worker")
        self.assertEqual(self.history_calls, [])
        later = self.hub.read_agent("remote", "worker")
        self.assertEqual(later["messages"][0]["text"], "FIXTURE OUTPUT")
        self.assert_read_only()

    def test_independent_targets_can_read_in_parallel_without_holding_registry_lock(self):
        first_gate, second_gate = self.gate("remote"), self.gate("other")
        first = self.start("remote")
        self.assertTrue(first_gate["entered"][0].wait(2))
        second = self.start("other")
        self.assertTrue(second_gate["entered"][0].wait(2), "A different SSH target must not queue behind the first")
        acquired = threading.Event()
        def take_registry():
            with self.hub.registry_lock:
                acquired.set()
        thread = threading.Thread(target=take_registry, name="read-admission-registry-fixture", daemon=True)
        self.threads.append(thread)
        thread.start()
        self.assertTrue(acquired.wait(1), "Read network waits cannot hold the mutation registry lock")
        self.assertFalse(first["done"].is_set())
        self.assertFalse(second["done"].is_set())
        first_gate["release"].set()
        second_gate["release"].set()
        self.finish(first)
        self.finish(second)
        self.assertIsNone(first["error"])
        self.assertIsNone(second["error"])
        self.assert_read_only()

    def test_local_readers_are_not_limited_by_ssh_admission(self):
        gate = self.gate("local", entries=2)
        first, second = self.start("local"), self.start("local")
        self.assertTrue(gate["entered"][0].wait(2))
        self.assertTrue(gate["entered"][1].wait(2), "Local history readers can both reach their first bind")
        self.assertFalse(first["done"].is_set())
        self.assertFalse(second["done"].is_set())
        gate["release"].set()
        self.finish(first)
        self.finish(second)
        self.assertIsNone(first["error"])
        self.assertIsNone(second["error"])
        self.assert_read_only()

    def test_scope_lookup_still_precedes_the_busy_route_guard(self):
        gate = self.gate("remote")
        winner = self.start()
        self.assertTrue(gate["entered"][0].wait(2))
        with self.assertRaises(ValueError) as caught:
            self.hub.read_agent("remote", "not-enrolled", plugin=True)
        self.assertNotIsInstance(caught.exception, HistoryReadError)
        self.assertEqual(len(self.history_calls), 1)
        gate["release"].set()
        self.finish(winner)
        self.assert_read_only()


if __name__ == "__main__":
    unittest.main()
