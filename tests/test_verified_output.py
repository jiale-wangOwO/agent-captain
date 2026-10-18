"""One-SSH read orchestration preserves worker identity and output boundaries."""
import copy
import json
import unittest
from unittest.mock import patch

from agent_captain.sessions import verified_output as verified


TERMINAL = "ceohub-0123456789abcdef01234567"
SESSION = "11111111-2222-4333-8444-555555555555"
OTHER = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
SECRET = "PRIVATE_EXCEPTION_OR_PARTIAL_OUTPUT"


class VerifiedOutputTests(unittest.TestCase):
    def setUp(self):
        self.identity = {"pid": 4242, "start_ticks": 100, "pane_pid": 1234, "pane_start_ticks": 99}
        self.binding = {"state": "bound", "binding_evidence": "held_rollout", "session_id": SESSION,
                        "rollout_path": "/private/.codex/sessions/exact.jsonl", "codex_home": "/private/.codex",
                        "process_identity": self.identity, "source": "cli", "cwd": "/project"}
        self.output = {"id": SESSION, "source": "codex_rollout", "messages": [{"role": "assistant", "text": SECRET}],
                       "next_cursor": "next-page", "after_cursor": "next-new", "content_complete": True,
                       "history_complete": True, "read_issues": {}, "include_tools": False}
        self.request = {"op": "read_bound_output", "terminal_id": TERMINAL, "saved_binding": None,
                        "historical_allowed": False, "limit": 18, "include_tools": False}
        self.sequence = []
        def bind(request):
            self.sequence.append("bind")
            self.assertEqual(request, {"op": "bind_output", "terminal_id": TERMINAL})
            return copy.deepcopy(self.binding)
        def read(request):
            self.sequence.append("read")
            return copy.deepcopy(self.output)
        self.binding_patch = patch.object(verified.session_binding, "main", side_effect=bind)
        self.read_patch = patch.object(verified.conversation_output, "main", side_effect=read)
        self.bind = self.binding_patch.start()
        self.read = self.read_patch.start()
        self.addCleanup(self.binding_patch.stop)
        self.addCleanup(self.read_patch.stop)

    def result(self, **changes):
        return verified.main(self.request | changes)

    def assert_unavailable(self, code):
        result = self.result()
        self.assertEqual(result, {"state": "unavailable", "reason_code": code})
        self.assertNotIn(SECRET, json.dumps(result))

    def test_live_output_is_bracketed_by_two_process_and_session_checks(self):
        result = self.result()
        self.assertEqual(self.sequence, ["bind", "read", "bind"])
        self.assertEqual(result["state"], "bound")
        self.assertEqual(result["output"], self.output)
        self.assertEqual(result["binding"]["terminal_id"], TERMINAL)
        self.assertTrue(result["binding"]["live_session_verified"])
        self.assertFalse(result["binding"]["historical"])
        self.assertNotIn("terminal_id", self.binding, "Input bindings are not mutated")

    def test_held_current_rollout_wins_over_saved_initial_resume_session(self):
        saved = self.binding | {"session_id": OTHER, "method": "human_selected_session", "historical": True}
        result = self.result(saved_binding=saved)
        self.assertEqual(result["binding"]["session_id"], SESSION)
        self.assertFalse(result["binding"]["historical"])
        self.assertEqual(self.read.call_args.args[0]["session_id"], SESSION)

    def test_changed_process_or_in_tui_session_switch_discards_all_output(self):
        for after in (self.binding | {"process_identity": self.identity | {"start_ticks": 101}},
                      self.binding | {"process_identity": self.identity | {"pane_start_ticks": 101}},
                      self.binding | {"session_id": OTHER}, {"state": "unbound", "reason": "process_changed"}):
            with self.subTest(after=after):
                self.bind.side_effect = [copy.deepcopy(self.binding), copy.deepcopy(after)]
                self.assert_unavailable("session_identity_changed")

    def test_post_probe_conflicting_evidence_discards_all_output(self):
        after = {"state": "unbound", "reason": "ambiguous_session_evidence", "process_identity": self.identity}
        self.bind.side_effect = [copy.deepcopy(self.binding), after]
        self.assert_unavailable("output_binding_invalid")

    def test_same_process_manual_saved_binding_is_not_claimed_as_current(self):
        saved = self.binding | {"historical": True, "method": "human_selected_session"}
        probe = {"state": "unbound", "reason": "no_exact_session_evidence", "process_identity": self.identity}
        self.bind.side_effect = [copy.deepcopy(probe), copy.deepcopy(probe)]
        result = self.result(saved_binding=saved)
        self.assertEqual(result["state"], "bound")
        self.assertEqual(result["binding"]["session_id"], SESSION)
        self.assertFalse(result["binding"]["live_session_verified"])
        self.assertFalse(result["binding"]["historical"], "A previous historical flag does not suppress live revalidation")
        self.assertEqual(self.bind.call_count, 2)

    def test_saved_same_process_choice_precedes_stale_explicit_resume_argv(self):
        saved = self.binding | {"method": "human_selected_session"}
        initial = self.binding | {"session_id": OTHER, "binding_evidence": "explicit_resume"}
        self.bind.side_effect = [copy.deepcopy(initial), copy.deepcopy(initial)]
        result = self.result(saved_binding=saved)
        self.assertEqual(result["binding"]["session_id"], SESSION)
        self.assertFalse(result["binding"]["live_session_verified"])
        self.assertFalse(result["binding"]["historical"])

    def test_explicit_resume_alone_is_readable_but_not_live_session_proof(self):
        initial = self.binding | {"binding_evidence": "explicit_resume"}
        self.bind.side_effect = [copy.deepcopy(initial), copy.deepcopy(initial)]
        result = self.result()
        self.assertEqual(result["state"], "bound")
        self.assertFalse(result["binding"]["live_session_verified"])
        self.assertEqual(self.bind.call_count, 2)

    def test_real_resolved_manual_binding_is_normalized_for_live_and_historical_choices(self):
        saved = {key: copy.deepcopy(value) for key, value in self.binding.items() if key != "binding_evidence"}
        saved.update(state="resolved", method="human_selected_session")
        original = copy.deepcopy(saved)
        no_exact = {"state": "unbound", "reason": "no_exact_session_evidence", "process_identity": self.identity}
        explicit = self.binding | {"session_id": OTHER, "binding_evidence": "explicit_resume"}
        unavailable = {"state": "unbound", "reason": "process_metadata_unavailable"}
        for probe, historical in ((no_exact, False), (explicit, False), (unavailable, True)):
            with self.subTest(probe=probe, historical=historical):
                self.bind.reset_mock()
                self.bind.side_effect = [copy.deepcopy(probe)] if historical else [copy.deepcopy(probe), copy.deepcopy(probe)]
                result = self.result(saved_binding=saved, historical_allowed=historical)
                self.assertEqual(result["state"], "bound")
                self.assertEqual(result["binding"]["state"], "bound")
                self.assertEqual(result["binding"]["session_id"], SESSION)
                self.assertEqual(result["binding"]["historical"], historical)
                self.assertFalse(result["binding"]["live_session_verified"])
                self.assertEqual(result["binding"]["method"], "human_selected_session")
                self.assertEqual(self.bind.call_count, 1 if historical else 2)
                self.assertEqual(saved, original, "Selection normalization does not mutate persisted caller data")

    def test_newly_held_post_probe_can_confirm_same_saved_session(self):
        initial = {"state": "unbound", "reason": "no_exact_session_evidence", "process_identity": self.identity}
        self.bind.side_effect = [initial, copy.deepcopy(self.binding)]
        result = self.result(saved_binding=self.binding | {"method": "human_selected_session"})
        self.assertTrue(result["binding"]["live_session_verified"])

    def test_changed_process_cannot_reuse_saved_binding(self):
        probe = {"state": "unbound", "reason": "no_exact_session_evidence",
                 "process_identity": self.identity | {"start_ticks": 101}}
        self.bind.side_effect = [probe]
        result = self.result(saved_binding=self.binding)
        self.assertEqual(result, {"state": "unbound", "reason": "no_exact_session_evidence"})
        self.read.assert_not_called()

    def test_historical_permission_or_foreground_shell_retains_nonlive_semantics_without_post_probe(self):
        for reason, allowed in (("process_metadata_unavailable", True), ("foreground_shell", False)):
            with self.subTest(reason=reason, allowed=allowed):
                self.bind.reset_mock()
                self.bind.side_effect = [{"state": "unbound", "reason": reason}]
                result = self.result(saved_binding=self.binding, historical_allowed=allowed)
                self.assertEqual(result["state"], "bound")
                self.assertTrue(result["binding"]["historical"])
                self.assertFalse(result["binding"]["live_session_verified"])
                self.assertEqual(self.bind.call_count, 1)

    def test_ambiguous_live_evidence_does_not_reuse_manual_binding(self):
        self.bind.side_effect = [{"state": "unbound", "reason": "ambiguous_session_evidence", "process_identity": self.identity}]
        self.assertEqual(self.result(saved_binding=self.binding), {"state": "unbound", "reason": "ambiguous_session_evidence"})
        self.read.assert_not_called()

    def test_cursor_after_cursor_limit_and_tool_mode_are_forwarded_without_reinterpretation(self):
        for paging in ({}, {"cursor": "opaque-cursor"}, {"after_cursor": "opaque-after"}):
            for include_tools in (False, True):
                with self.subTest(paging=paging, include_tools=include_tools):
                    result = self.result(limit=7, include_tools=include_tools, **paging)
                    request = self.read.call_args.args[0]
                    self.assertEqual(request, {"op": "read_output", "session_id": SESSION,
                        "rollout_path": self.binding["rollout_path"], "codex_home": self.binding["codex_home"],
                        "limit": 7, "include_tools": include_tools, **paging})
                    self.assertEqual(result["output"], self.output)

    def test_bad_request_or_combined_cursors_never_start_binding_or_output_read(self):
        for changes in ({"op": "other"}, {"terminal_id": "other-terminal"}, {"historical_allowed": 1},
                        {"saved_binding": "wrong"}, {"include_tools": 1}, {"limit": 0}, {"limit": True},
                        {"cursor": "first", "after_cursor": "second"}):
            with self.subTest(changes=changes):
                self.assertEqual(self.result(**changes), {"state": "unavailable", "reason_code": "remote_read_failed"})
        self.bind.assert_not_called()
        self.read.assert_not_called()

    def test_missing_binding_reason_is_sanitized_to_fixed_value(self):
        for reason in (SECRET, "private_lowercase_text", None):
            self.bind.side_effect = [{"state": "unbound", "reason": reason}]
            self.assertEqual(self.result(), {"state": "unbound", "reason": "session_identity_unavailable"})

    def test_output_session_or_source_mismatch_discards_payload(self):
        for output in (self.output | {"id": OTHER}, self.output | {"source": "terminal"}, None):
            self.read.side_effect = None
            self.read.return_value = output
            self.assert_unavailable("output_binding_invalid")

    def test_exceptions_at_any_stage_discard_partial_payload_and_raw_diagnostics(self):
        for phase in ("pre", "read", "post"):
            with self.subTest(phase=phase):
                self.bind.side_effect = ([RuntimeError(SECRET)] if phase == "pre" else
                                        [copy.deepcopy(self.binding), RuntimeError(SECRET)] if phase == "post" else
                                        lambda _: copy.deepcopy(self.binding))
                self.read.side_effect = ValueError(SECRET) if phase == "read" else lambda _: copy.deepcopy(self.output)
                self.assert_unavailable("remote_read_failed")


if __name__ == "__main__":
    unittest.main()
