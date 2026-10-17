"""Actual output reads preserve input and enrolled-terminal identity boundaries."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_captain.core.terminal_hub import Hub
from test_terminal_hub import FakeTerminals

SESSION = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
TERMINAL = "ceohub-0123456789abcdef01234567"


class ConversationHubTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.terminals = FakeTerminals()
        self.hub = Hub(self.temp.name, terminals=self.terminals)
        self.hub.hosts = {"local": {"transport": "local", "label": "Local"}}
        self.terminals.add(TERMINAL, state="agent", screen="ONLY REPORT TAIL")
        # A legacy registry ID is not proof of which session runs in its terminal.
        self.hub.store.remember("local", OTHER, "Work", active=True, mode="terminal", terminal_id=TERMINAL)
        self.identity = {"pid": 4242, "start_ticks": 100}
        self.binding = {"state": "bound", "binding_evidence": "held_rollout", "session_id": SESSION, "rollout_path": "/home/test/.codex/sessions/target.jsonl",
                        "codex_home": "/home/test/.codex", "process_identity": self.identity}
        self.calls = []
        self.probe = dict(self.binding)
        def history(host, request):
            self.calls.append((host, request))
            if request["op"] == "bind_output":
                return self.probe.copy()
            if request["op"] == "resolve_output":
                return {**self.binding, "state": "resolved", "session_id": request["session_id"]}
            if request["op"] == "read_output":
                return {"source": "codex_rollout", "messages": [{"role": "assistant", "text": "BEGIN\n" + "full report\n" * 1000 + "END"}],
                        "history_complete": True, "content_complete": True, "has_more": False,
                        "next_cursor": None, "after_cursor": "next-new", "read_issues": {}, "status": "completed_in_history"}
            raise AssertionError("No full-machine find/scan is allowed")
        self.patch = patch.object(self.hub, "history", side_effect=history)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.hub.close()
        self.temp.cleanup()

    def read(self, **args):
        return self.hub.plugin_call("read_agent", {"agent_id": "local::" + OTHER, **args})

    def test_actual_full_report_not_ansi_and_not_registry_id(self):
        result = self.read(limit=8)
        self.assertEqual(result["source"], "codex_rollout")
        self.assertEqual(result["screen"], "ONLY REPORT TAIL")
        self.assertTrue(result["messages"][0]["text"].startswith("BEGIN"))
        self.assertTrue(result["messages"][0]["text"].endswith("END"))
        self.assertGreater(len(result["messages"][0]["text"]), 6000)
        request = next(r for _, r in self.calls if r["op"] == "read_output")
        self.assertEqual(request["session_id"], SESSION)
        self.assertEqual(result["status"], "agent")
        self.assertEqual(result["conversation_status"], "completed_in_history")
        self.assertEqual(self.terminals.writes, [])

    def test_cursor_is_forwarded_only_to_bound_session(self):
        result = self.read(cursor="exact-page")
        request = next(r for _, r in self.calls if r["op"] == "read_output")
        self.assertEqual(request["cursor"], "exact-page")
        self.assertEqual(result["after_cursor"], "next-new")
        self.assertEqual(self.terminals.writes, [])

    def test_legacy_inactive_enrolled_worker_can_read_exact_output(self):
        self.hub.store.set_active("local", OTHER, False)
        result = self.read()
        self.assertEqual(result["source"], "codex_rollout")
        self.assertTrue(result["messages"][0]["text"].endswith("END"))
        self.assertEqual(self.terminals.writes, [])

    def test_history_only_record_performs_no_probe_or_read(self):
        self.hub.store.remember("local", OTHER, "Work", mode="observed")
        with self.assertRaises(PermissionError):
            self.read()
        self.assertEqual(self.calls, [])

    def test_closed_during_read_withholds_output(self):
        original = self.hub.history.side_effect
        def close_worker(host, request):
            result = original(host, request)
            if request["op"] == "read_output":
                self.hub.store.remember("local", OTHER, "Work", mode="closed")
            return result
        self.hub.history.side_effect = close_worker
        with self.assertRaises(PermissionError):
            self.read()

    def test_picker_unbound_screen_is_never_full_report(self):
        self.probe = {"state": "unbound", "reason": "no_exact_session_evidence", "process_identity": self.identity,
                      "codex_home": self.binding["codex_home"]}
        result = self.read()
        self.assertEqual(result["messages"], [])
        self.assertEqual(result["source"], "terminal")
        self.assertFalse(result["history_complete"])
        self.assertFalse(result["content_complete"])

    def test_explicit_association_does_not_send_keys_or_claim_live_session(self):
        self.probe = {"state": "unbound", "reason": "no_exact_session_evidence", "process_identity": self.identity,
                      "codex_home": self.binding["codex_home"]}
        association = self.hub.bind_output("local", OTHER, SESSION)
        self.assertEqual(association["output_binding"]["session_id"], SESSION)
        result = self.read()
        self.assertEqual(result["source"], "codex_rollout")
        self.assertFalse(result["output_binding"]["live_session_verified"])
        self.assertEqual(self.terminals.writes, [])
        self.assertEqual(self.hub.get_agent("local", OTHER)["output_binding"]["state"], "recorded")

    def test_changed_process_cannot_reuse_manual_association(self):
        self.test_explicit_association_does_not_send_keys_or_claim_live_session()
        self.probe["process_identity"] = {"pid": 4242, "start_ticks": 999}
        self.calls.clear()
        result = self.read()
        self.assertEqual(result["messages"], [])
        self.assertFalse(any(r["op"] == "read_output" for _, r in self.calls))

    def test_ambiguous_evidence_cannot_fall_back_to_manual_binding(self):
        self.test_explicit_association_does_not_send_keys_or_claim_live_session()
        self.probe["reason"] = "ambiguous_session_evidence"
        self.calls.clear()
        self.assertEqual(self.read()["messages"], [])
        self.assertFalse(any(r["op"] == "read_output" for _, r in self.calls))

    def test_binding_is_durable_but_not_live_verified_on_registry_read(self):
        self.read()
        self.hub.close()
        restored = Hub(self.temp.name, terminals=self.terminals)
        try:
            metadata = restored.get_agent("local", OTHER)
            self.assertEqual(metadata["output_binding"]["state"], "recorded")
            self.assertEqual(metadata["output_binding"]["session_id"], SESSION)
            self.assertNotIn("live_session_verified", metadata["output_binding"])
        finally:
            restored.close()

    def test_conflicting_explicit_id_is_not_associated(self):
        with self.assertRaises(ValueError):
            self.hub.bind_output("local", OTHER, OTHER)
        self.assertFalse(any(r["op"] == "resolve_output" for _, r in self.calls))
        self.assertEqual(self.terminals.writes, [])

    def test_initial_resume_argv_is_not_current_session_proof(self):
        self.probe["binding_evidence"] = "explicit_resume"
        result = self.read()
        self.assertEqual(result["source"], "codex_rollout")
        self.assertFalse(result["output_binding"]["live_session_verified"])

    def test_human_selected_id_takes_precedence_over_initial_resume_argv(self):
        self.probe["binding_evidence"] = "explicit_resume"
        association = self.hub.bind_output("local", OTHER, OTHER)
        self.assertEqual(association["output_binding"]["session_id"], OTHER)
        result = self.read()
        self.assertEqual(result["output_binding"]["session_id"], OTHER)
        self.assertFalse(result["output_binding"]["live_session_verified"])

    def test_same_process_switching_session_during_read_withholds_old_records(self):
        original = self.hub.history.side_effect
        def switched(host, request):
            result = original(host, request)
            if request["op"] == "read_output":
                self.probe["session_id"] = OTHER
            return result
        self.hub.history.side_effect = switched
        result = self.read()
        self.assertEqual(result["messages"], [])
        self.assertEqual(result["output_binding"]["state"], "unavailable")


if __name__ == "__main__":
    unittest.main()
