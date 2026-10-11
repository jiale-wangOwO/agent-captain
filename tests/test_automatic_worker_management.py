"""Connection-based management policy; fake transport, no runtime mutation."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from store import Store
from terminal_client import BrokerError
from terminal_hub import Hub, RETRYABLE
from tests.test_terminal_hub import FakeTerminals


TERMINAL = "ceohub-0123456789abcdef01234567"


class AutomaticWorkerManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.terminals = FakeTerminals()
        self.hub = Hub(self.temp.name, terminals=self.terminals)
        self.hub.hosts = {"local": {"transport": "local", "label": "Local"},
                          "316": {"transport": "ssh", "target": "fake316", "label": "316"}}
        self.hub._stop.set()
        self.hub._watcher.join(3)

    def tearDown(self):
        self.hub.close()
        self.temp.cleanup()

    def register(self, name="worker", *, mode="terminal", active=False, host="local", terminal=TERMINAL, cwd="/work/project"):
        if terminal:
            self.terminals.add(terminal, host)
        return self.hub.store.remember(host, name, "Human chosen name", active=active,
                                       mode=mode, terminal_id=terminal, cwd=cwd)

    def test_open_bound_workers_ignore_activation_and_include_disconnected_metadata(self):
        live = self.register()
        disconnected = self.register("remote", host="316", terminal="ceohub-1123456789abcdef01234567")
        self.terminals.records[disconnected["terminal_id"]].update(connected=False, state="exited", agent_ready=False)
        with patch.object(self.hub, "history", side_effect=AssertionError("No scans")), \
             patch.object(self.terminals, "create", side_effect=AssertionError("No starts")):
            rows = self.hub.managed_worker_rows()
            listing = self.hub.plugin_call("list_active_agents", {})
        self.assertEqual({row["thread_id"] for row in rows}, {"worker", "remote"})
        self.assertEqual(listing["effective_worker_count"], 2)
        self.assertEqual(listing["worker_access"], "automatic")
        self.assertTrue(all(row["active"] for row in listing["agents"]))
        remote = next(row for row in listing["agents"] if row["host"] == "316")
        self.assertFalse(remote["connected"])
        self.assertEqual(remote["management"]["primary_action"], "reconnect")
        self.assertEqual(self.hub.store.agent("local", "worker")["active"], 0)
        self.assertEqual(self.terminals.writes, [])

    def test_predicate_excludes_history_malformed_and_closed_even_if_old_active(self):
        for mode in ("history", "closed", "closing", "close_unknown"):
            row = self.register(mode=mode, active=True)
            self.assertFalse(self.hub.is_managed_worker(row))
            self.assertEqual(self.hub.plugin_call("list_active_agents", {})["agents"], [])
            with self.assertRaises(PermissionError):
                self.hub.plugin_call("get_agent", {"agent_id": "local::worker"})
        for terminal in (None, "", "malformed", "ceohub-abc", "ceohub-" + "a" * 49):
            row = self.register(active=True, terminal=terminal)
            # remember(None) preserves identity by design; explicit malformed
            # metadata is tested as a row predicate rather than clearing it.
            row["terminal_id"] = terminal
            self.assertFalse(self.hub.is_managed_worker(row))
        self.assertFalse(self.hub.is_managed_worker(None))
        self.assertEqual(self.terminals.writes, [])

    def test_controller_identity_and_workspace_cannot_be_enrolled(self):
        row = self.register("manager")
        for identity, workspace in ((True, False), (False, True)):
            with patch.object(self.hub.controllers, "is_controller_reference", side_effect=
                              lambda host, thread, cwd=None: (identity and thread == "manager") or (workspace and cwd == "/work/project")):
                self.assertFalse(self.hub.is_managed_worker(row))
                self.assertEqual(self.hub.managed_worker_rows(), [])
                self.assertEqual(self.hub.plugin_call("list_active_agents", {})["agents"], [])
                with self.assertRaises(PermissionError):
                    self.hub.get_agent("local", "manager", plugin=True)
        self.assertEqual(self.terminals.writes, [])

    def test_activation_compatibility_cannot_revoke_or_submit_input(self):
        self.register()
        pending = self.hub.store.enqueue("local", "worker", "pending", origin="plugin", request_key="existing")
        before = self.hub.store.agent("local", "worker")
        cursor = self.hub.store.event_cursor()
        for active in (True, False):
            result = self.hub.activate_agent("local", "worker", active)
            self.assertTrue(result["active"])
            self.assertEqual(result["activation_compatibility"]["requested_active"], active)
            self.assertEqual(result["activation_compatibility"]["policy"], "connected_workers")
        self.assertEqual(self.hub.store.agent("local", "worker"), before)
        self.assertEqual(self.hub.store.delivery(pending)["state"], "queued")
        self.assertEqual(self.hub.store.event_cursor(), cursor)
        self.assertEqual(self.terminals.writes, [])

    def test_names_are_metadata_only_with_same_id_binding_and_pins_after_restart(self):
        self.register()
        self.hub.store.setting(self.hub._output_key("local", TERMINAL), {"session_id": "exact-session"})
        updated = self.hub.plugin_call("update_agent", {"agent_id": "local::worker", "alias": "316 / pyCircuit", "pinned": True, "request_key": "rename"})
        before = self.hub.store.agent("local", "worker")
        self.hub.close()
        self.hub = Hub(self.temp.name, terminals=self.terminals)
        got = self.hub.plugin_call("get_agent", {"agent_id": "local::worker"})
        self.assertEqual(got["alias"], "316 / pyCircuit")
        self.assertEqual(got["agent_id"], updated["agent_id"])
        self.assertEqual(got["terminal_id"], TERMINAL)
        self.assertEqual(got["output_binding"]["session_id"], "exact-session")
        self.assertTrue(got["pinned"])
        self.assertEqual(self.hub.store.agent("local", "worker"), before)
        self.assertEqual(self.terminals.writes, [])

    def test_enrollment_does_not_bypass_foreground_human_busy_or_unknown_delivery(self):
        self.register()
        args = {"agent_id": "local::worker", "text": "ordinary user task", "request_key": "task"}
        for code, state in (("waiting_for_agent", "waiting_for_agent"), ("human_busy", "human_busy")):
            with patch.object(self.terminals, "write", side_effect=BrokerError("Blocked", code, 409)):
                result = self.hub.plugin_call("send_agent_message", args)
            self.assertEqual(result["state"], state)
        with patch.object(self.terminals, "write", side_effect=TimeoutError("Uncertain")):
            result = self.hub.plugin_call("send_agent_message", args)
        self.assertEqual(result["state"], "unknown")
        replay = self.hub.plugin_call("send_agent_message", args)
        self.assertEqual(replay["id"], result["id"])
        self.assertEqual(replay["state"], "unknown")
        self.assertEqual(self.terminals.writes, [])

    def test_policy_upgrade_fences_all_old_inactive_plugin_pending_without_transport(self):
        self.hub.close()
        store = Store(Path(self.temp.name) / "hub.sqlite")
        with store.lock, store.db:
            store.db.execute("delete from settings where key='automatic-worker-management-v1'")
        store.remember("local", "old", "Preserved old name", active=False, mode="terminal", terminal_id=TERMINAL, cwd="C:/old")
        store.update_agent_metadata("local", "old", pinned=True)
        original = store.agent("local", "old")
        requests = []
        for index in range(145):
            key = "old-" + str(index)
            request = store.enqueue("local", "old", "unsent old task " + str(index), origin="plugin", request_key=key)
            store.update_outbox(request, RETRYABLE[index % len(RETRYABLE)])
            requests.append((request, key))
        retained = []
        for state in ("submitted", "unknown", "submitting", "scope_revoked", "cancelled"):
            request = store.enqueue("local", "old", "retained", origin="plugin", request_key="retained-" + state)
            store.update_outbox(request, state)
            retained.append((request, state))
        human = store.enqueue("local", "old", "human", origin="ui", request_key="human")
        store.close()
        with patch.object(self.terminals, "create", side_effect=AssertionError("Migration must not start")), \
             patch.object(self.terminals, "write", side_effect=AssertionError("Migration must not replay")), \
             patch("terminal_hub.subprocess.run", side_effect=AssertionError("Migration must not SSH")):
            self.hub = Hub(self.temp.name, terminals=self.terminals)
        self.assertEqual(self.hub.store.agent("local", "old"), original)
        for request, key in requests:
            delivery = self.hub.store.delivery(request)
            self.assertEqual(delivery["state"], "not_sent")
            self.assertEqual(delivery["request_key"], key)
            self.assertIn("unsent old task", delivery["text"])
        for request, state in retained:
            self.assertEqual(self.hub.store.delivery(request)["state"], state)
        self.assertEqual(self.hub.store.delivery(human)["state"], "queued")
        audits = self.hub.store.events(kinds=("management_policy_input_fenced",))
        self.assertEqual(len(audits), 145)
        self.assertEqual({event["data"]["request_id"] for event in audits}, {request for request, _ in requests})
        self.assertNotIn("unsent old task", json.dumps(audits))
        self.hub.close()
        self.hub = Hub(self.temp.name, terminals=self.terminals)
        self.assertEqual(len(self.hub.store.events(kinds=("management_policy_input_fenced",))), 145)
        self.assertEqual(self.terminals.writes, [])


if __name__ == "__main__":
    unittest.main()
