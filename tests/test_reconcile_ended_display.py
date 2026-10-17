"""Explicit terminated-display reconciliation; no real SSH, PTY or models."""
import base64
import copy
import hashlib
import json
import tempfile
import unittest
from unittest.mock import patch

from terminal_client import BrokerError, TerminalClient
from terminal_hub import Hub
from tests.test_terminal_hub import FakeTerminals


TERMINAL = "ceohub-0123456789abcdef01234567"
IDENTITY = {"name": TERMINAL, "session": "$11", "created": 1234,
            "pane": "%20", "pane_pid": 777777, "pane_start_ticks": 888888}
KEY = "original-ui-unknown"


def marker(identity=IDENTITY):
    return "/.__pronhub_existing__/" + TERMINAL + "/" + base64.urlsafe_b64encode(
        json.dumps(identity, sort_keys=True).encode()).decode().rstrip("=")


class EndedDisplayReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.terminals = FakeTerminals()
        self.hub = Hub(self.temp.name, terminals=self.terminals)
        self.hub._stop.set()
        self.hub._watcher.join(3)
        self.hub.hosts = {"remote": {"transport": "ssh", "target": "fake-ssh-target", "label": "Remote"}}
        row = self.terminals.add(TERMINAL, "remote")
        row.update(target="fake-ssh-target", connected=False, state="exited", agent_ready=False,
                   human_busy=False, exit_code=255, cwd=marker())
        self.terminals.records[TERMINAL] = row
        self.hub.store.remember("remote", "worker", "Original", mode="terminal", terminal_id=TERMINAL, cwd="/project")
        self.owner = {"pid": 67890, "version": "0.3.0", "recovery_id": None}
        self.receipt = {"owner": self.owner, "reference_sha256": "a" * 64,
                        "descriptor_sha256": "b" * 64, "reference_identity": (1, 2, 3, 4, 5),
                        "descriptor_identity": (1, 6, 7, 8, 9), "owner_pid_state": "alive",
                        "session": row, "display_pid_state": "dead"}
        self.terminals.bound_owner_status = lambda: self.owner | {"state": "ready", "running": True}
        self.receipts = []
        def snapshot(terminal_id):
            self.assertEqual(terminal_id, TERMINAL)
            self.receipts.append(terminal_id)
            return copy.deepcopy(self.receipt)
        self.terminals.ended_display_snapshot = snapshot
        self.probes = []
        def probe(host, terminal_id):
            self.probes.append((host, terminal_id))
            return {"ok": True, "exists": True, "identity": dict(IDENTITY)}
        self.hub._probe_remote_terminal = probe
        self.operation = "agent-reconnect:" + KEY
        self.fingerprint = self.hub._reconnect_fingerprint("remote", "worker", TERMINAL, "ui", None)
        self.previous = {"state": "unknown", "recovery": {"code": "reconnect_unknown", "message": "Original uncertain receipt",
                         "request_key": KEY, "request_origin": "ui", "next_action": "inspect_connection"}}
        self.hub.store.begin_operation(self.operation, self.fingerprint)
        self.hub.store.finish_operation(self.operation, self.previous)
        self.recovery_key = self.hub._recovery_key("remote", "worker")
        self.hub.store.setting(self.recovery_key, self.previous["recovery"])

    def tearDown(self):
        self.hub.close()
        self.temp.cleanup()

    def reconcile(self, **kwargs):
        return self.hub.reconcile_reconnect("remote", "worker", KEY, confirm=True, **kwargs)

    def stored(self):
        row = self.hub.store.db.execute("select state,result from operations where request_key=?", (self.operation,)).fetchone()
        return row["state"], json.loads(row["result"]) if row["result"] else None

    def assert_fenced(self):
        with self.assertRaises(ValueError) as error:
            self.reconcile()
        self.assertNotIn("PRIVATE_SECRET", str(error.exception))
        self.assertEqual(self.stored(), ("done", self.previous))
        self.assertFalse(any(call[0] == "create" for call in self.terminals.calls))
        self.assertEqual(self.terminals.writes, [])

    def test_confirmed_same_owner_ended_display_preserves_original_unknown_audit(self):
        result = self.reconcile()
        self.assertEqual(result["operation_state"], "blocked")
        self.assertTrue(result["recovery"]["retryable"])
        self.assertEqual(result["recovery"]["reconciliation_kind"], "ended_display")
        self.assertIn("可能已执行", result["recovery"]["message"])
        self.assertEqual(self.receipts, [TERMINAL, TERMINAL])
        self.assertEqual(self.probes, [("remote", TERMINAL)])
        self.assertFalse(any(call[0] == "create" for call in self.terminals.calls))
        self.assertEqual(self.terminals.writes, [])
        rendered = json.dumps(result)
        self.assertNotIn(".__pronhub_existing__", rendered)
        self.assertNotIn("descriptor_sha256", rendered)
        audit_key = "agent-reconnect-reconciliation:" + hashlib.sha256(self.operation.encode()).hexdigest()
        audit = self.hub.store.setting(audit_key)
        self.assertEqual(audit[0]["previous_result"], self.previous)
        count = len(self.receipts)
        self.assertEqual(self.reconcile()["recovery"], result["recovery"])
        self.assertEqual(len(self.receipts), count, "Repeated reconciliation only retrieves the durable result")
        # The same key, and only the same key, may now perform the original
        # existing-attachment request. No business text is sent.
        retried = self.hub.reconnect_agent("remote", "worker", request_key=KEY)
        self.assertEqual(retried["operation_state"], "done")
        creates = [call for call in self.terminals.calls if call[0] == "create"]
        self.assertEqual(len(creates), 1)
        self.assertTrue(creates[0][1]["cwd"].startswith("/.__pronhub_existing__/"))
        self.assertEqual(self.terminals.writes, [])
        self.assertEqual(self.hub.store.setting(audit_key)[0]["previous_result"], self.previous)

    def test_confirmation_required_before_any_receipt_or_remote_query(self):
        with self.assertRaises(ValueError):
            self.hub.reconcile_reconnect("remote", "worker", KEY)
        self.assertEqual(self.receipts, [])
        self.assertEqual(self.probes, [])

    def test_cached_exact_ui_ended_marker_offers_confirmed_check_not_automatic_unlock(self):
        view = self.hub.get_agent("remote", "worker")
        self.assertTrue(view["recovery"]["reconcile_available"])
        self.assertEqual(view["recovery"]["reconcile_request_keys"], [KEY])
        self.assertEqual(view["recovery"]["next_action"], "reconcile_reconnect")
        self.assertEqual(self.stored(), ("done", self.previous))
        self.assertEqual(self.receipts, [])
        self.assertEqual(self.probes, [])
        self.assertNotIn(".__pronhub_existing__", json.dumps(view))

    def test_cached_candidates_reject_plugin_missing_marker_running_or_wrong_target(self):
        original = copy.deepcopy(self.terminals.records[TERMINAL])
        for change in ({"connected": True}, {"state": "agent"}, {"human_busy": True}, {"cwd": "/project"},
                       {"target": "other-target"}, {"host": "other"}, {"transport": "local"}, {"exit_code": None}):
            with self.subTest(change=change):
                self.terminals.records[TERMINAL] = original | change
                self.assertFalse(self.hub.get_agent("remote", "worker")["recovery"].get("reconcile_available", False))
        self.terminals.records[TERMINAL] = original
        self.hub.store.setting(self.recovery_key, self.previous["recovery"] | {"request_origin": "plugin"})
        self.assertFalse(self.hub.get_agent("remote", "worker")["recovery"].get("reconcile_available", False))
        self.assertEqual(self.receipts, [])
        self.assertEqual(self.probes, [])

    def test_original_key_replay_without_reconciliation_stays_unknown(self):
        result = self.hub.reconnect_agent("remote", "worker", request_key=KEY)
        self.assertEqual(result["operation_state"], "unknown")
        self.assertEqual(self.receipts, [])
        self.assertEqual(self.probes, [])

    def test_nonterminal_or_closed_target_is_not_reconciled(self):
        self.hub.store.remember("remote", "worker", "Original", mode="closed")
        with self.assertRaises((ValueError, PermissionError)):
            self.reconcile()
        self.assertEqual(self.receipts, [])

    def test_changed_current_ui_request_does_not_unlock_historical_unknown(self):
        self.hub.store.setting(self.recovery_key, self.previous["recovery"] | {"request_key": "newer-key"})
        self.assert_fenced()
        self.assertEqual(self.receipts, [])

    def test_plugin_result_with_ui_fingerprint_is_still_rejected(self):
        self.previous["recovery"]["request_origin"] = "plugin"
        self.hub.store.finish_operation(self.operation, self.previous)
        self.hub.store.setting(self.recovery_key, self.previous["recovery"])
        self.assert_fenced()

    def test_different_agent_or_controller_fingerprint_is_rejected(self):
        fp = self.hub._reconnect_fingerprint("remote", "worker", TERMINAL, "plugin", "controller-a")
        with self.hub.store.db:
            self.hub.store.db.execute("update operations set fingerprint=? where request_key=?", (fp, self.operation))
        self.assert_fenced()

    def test_started_operation_stays_in_flight(self):
        with self.hub.store.db:
            self.hub.store.db.execute("update operations set state='started',result=null where request_key=?", (self.operation,))
        with self.assertRaises(ValueError):
            self.reconcile()
        self.assertEqual(self.stored(), ("started", None))
        self.assertEqual(self.receipts, [])

    def test_legacy_no_marker_is_rejected(self):
        self.receipt["session"]["cwd"] = "/project"
        self.assert_fenced()
        self.assertEqual(self.probes, [])

    def test_bad_canonical_marker_or_identity_is_rejected(self):
        for value in (marker() + "=", marker() + "/extra", "/.__pronhub_existing__/wrong/PRIVATE_SECRET", marker(IDENTITY | {"created": True})):
            with self.subTest(value=value):
                self.receipt["session"]["cwd"] = value
                self.assert_fenced()

    def test_alive_or_unknown_pid_cannot_be_treated_as_dead(self):
        for state in ("alive", "unknown", None):
            with self.subTest(state=state):
                self.receipt["display_pid_state"] = state
                self.assert_fenced()

    def test_running_draft_unknown_exit_or_wrong_machine_is_rejected(self):
        original = copy.deepcopy(self.receipt["session"])
        for change in ({"connected": True}, {"state": "unknown"}, {"human_busy": True},
                       {"human_busy": None}, {"exit_code": None}, {"exit_code": True}, {"pid": None},
                       {"id": "ceohub-000000000000000000000000"}, {"host": "other"},
                       {"transport": "local"}, {"target": "other-server"}):
            with self.subTest(change=change):
                self.receipt["session"] = original | change
                self.assert_fenced()
        self.receipt["session"] = original

    def test_owner_stamp_must_match_current_authenticated_owner(self):
        self.previous["owner_stamp"] = {"pid": self.owner["pid"] + 1, "recovery_id": None}
        self.hub.store.finish_operation(self.operation, self.previous)
        self.assert_fenced()

    def test_remote_original_identity_missing_changed_or_route_unavailable_is_rejected(self):
        for reply in ({"ok": False, "exists": False}, {"ok": True, "exists": False},
                      {"ok": True, "exists": True, "identity": IDENTITY | {"created": 5678}},
                      {"ok": True, "exists": True, "identity": IDENTITY | {"pane_pid": 777778}},
                      {"ok": True, "exists": True, "identity": IDENTITY | {"pane_start_ticks": 888889}}):
            with self.subTest(reply=reply):
                self.hub._probe_remote_terminal = lambda *_: reply
                self.assert_fenced()

    def test_owner_descriptor_reference_or_selected_row_changes_during_remote_read_stay_fenced(self):
        first = copy.deepcopy(self.receipt)
        mutations = [lambda x: x["owner"].update(pid=67891),
                     lambda x: x["owner"].update(recovery_id="c" * 32),
                     lambda x: x.update(descriptor_sha256="d" * 64),
                     lambda x: x.update(reference_sha256="e" * 64),
                     lambda x: x.update(descriptor_identity=(1, 123, 7, 8, 9)),
                     lambda x: x.update(reference_identity=(1, 456, 3, 4, 5)),
                     lambda x: x.update(owner_pid_state="unknown"),
                     lambda x: x["session"].update(pid=5678),
                     lambda x: x["session"].update(connected=True),
                     lambda x: x.update(display_pid_state="alive")]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                second = copy.deepcopy(first)
                mutate(second)
                self.terminals.ended_display_snapshot = unittest.mock.Mock(side_effect=[copy.deepcopy(first), second])
                self.assert_fenced()

    def test_owner_must_be_definitively_alive_not_pid_only_health(self):
        for state in ("dead", "unknown", None):
            with self.subTest(state=state):
                self.receipt["owner_pid_state"] = state
                self.assert_fenced()

    def test_reconciliation_then_same_name_remote_recreation_is_not_attached(self):
        self.reconcile()
        self.hub._probe_remote_terminal = lambda *_: {"ok": True, "exists": True, "identity": IDENTITY | {"created": 5678}}
        result = self.hub.reconnect_agent("remote", "worker", request_key=KEY)
        self.assertEqual(result["recovery"]["code"], "original_session_changed")
        self.assertFalse(any(call[0] == "create" for call in self.terminals.calls))
        self.assertEqual(self.terminals.writes, [])
        audit_key = "agent-reconnect-reconciliation:" + hashlib.sha256(self.operation.encode()).hexdigest()
        self.assertEqual(self.hub.store.setting(audit_key)[0]["previous_result"], self.previous)

    def test_reconciled_display_connected_by_other_action_is_not_assumed_original(self):
        self.reconcile()
        self.terminals.records[TERMINAL].update(connected=True, state="agent", agent_ready=True)
        result = self.hub.reconnect_agent("remote", "worker", request_key=KEY)
        self.assertEqual(result["operation_state"], "unknown")
        self.assertFalse(any(call[0] == "create" for call in self.terminals.calls))
        self.assertEqual(self.terminals.writes, [])

    def test_known_route_failure_keeps_private_identity_for_same_key_retry(self):
        self.reconcile()
        self.hub._probe_remote_terminal = lambda *_: {"ok": False, "exists": False, "reason": "route_unavailable"}
        failed = self.hub.reconnect_agent("remote", "worker", request_key=KEY)
        self.assertEqual(failed["recovery"]["code"], "route_unavailable")
        self.assertEqual(self.stored()[1]["reconciliation_identity"], IDENTITY)
        self.assertNotIn("reconciliation_identity", failed)
        self.hub._probe_remote_terminal = lambda *_: {"ok": True, "exists": True, "identity": IDENTITY | {"pane": "%21"}}
        rejected = self.hub.reconnect_agent("remote", "worker", request_key=KEY)
        self.assertEqual(rejected["recovery"]["code"], "original_session_changed")
        self.assertFalse(any(call[0] == "create" for call in self.terminals.calls))

    def test_fresh_post_attachment_unknown_cannot_reuse_ended_receipt(self):
        self.reconcile()
        with patch.object(self.terminals, "snapshot", side_effect=BrokerError("PRIVATE_SECRET")):
            fresh = self.hub.reconnect_agent("remote", "worker", request_key=KEY)
        self.assertEqual(fresh["operation_state"], "unknown")
        self.assertNotIn("reconciliation_identity", self.stored()[1])
        count = len([call for call in self.terminals.calls if call[0] == "create"])
        repeated = self.hub.reconnect_agent("remote", "worker", request_key=KEY)
        self.assertEqual(repeated["operation_state"], "unknown")
        self.assertEqual(len([call for call in self.terminals.calls if call[0] == "create"]), count)
        self.assertEqual(self.terminals.writes, [])

    def test_failed_second_snapshot_has_safe_message_and_does_not_unlock(self):
        self.terminals.ended_display_snapshot = unittest.mock.Mock(side_effect=[copy.deepcopy(self.receipt), BrokerError("PRIVATE_SECRET")])
        self.assert_fenced()

    def test_operation_cas_race_is_not_overwritten_and_selected_recovery_rolls_back(self):
        def changing_probe(*_):
            self.hub.store.finish_operation(self.operation, {"state": "done", "recovery": {"code": "newer_result"}})
            return {"ok": True, "exists": True, "identity": IDENTITY}
        self.hub._probe_remote_terminal = changing_probe
        with self.assertRaises(ValueError):
            self.reconcile()
        self.assertEqual(self.stored()[1]["recovery"]["code"], "newer_result")
        self.assertEqual(self.hub.store.setting(self.recovery_key), self.previous["recovery"])

    def test_selected_recovery_cas_race_rolls_back_operation_update(self):
        newer = self.previous["recovery"] | {"request_key": "newer"}
        def changing_probe(*_):
            self.hub.store.setting(self.recovery_key, newer)
            return {"ok": True, "exists": True, "identity": IDENTITY}
        self.hub._probe_remote_terminal = changing_probe
        with self.assertRaises(ValueError):
            self.reconcile()
        self.assertEqual(self.stored(), ("done", self.previous))
        self.assertEqual(self.hub.store.setting(self.recovery_key), newer)


class EndedDisplaySnapshotTests(unittest.TestCase):
    def setUp(self):
        self.client = TerminalClient()
        self.record = {"id": TERMINAL, "pid": 4242, "connected": False}
        self.context = {"data": {"pid": 67890, "version": "0.3.0", "recovery_id": None, "token": "PRIVATE_SECRET"},
                        "reference": {"sha256": "a" * 64, "identity": (1, 2, 3, 4, 5)},
                        "descriptor": {"sha256": "b" * 64, "identity": (1, 6, 7, 8, 9)}}
        self.client._bound_context = lambda: self.context
        self.check = unittest.mock.Mock(return_value={"state": "ready"})
        self.client._bound_check = self.check
        self.client.request = unittest.mock.Mock(return_value={"sessions": [self.record]})
        self.client._pid_state = unittest.mock.Mock(side_effect=lambda pid: "alive" if pid == 67890 else "dead")

    def test_readonly_helper_authenticates_owner_before_and_after_exact_registry_query(self):
        result = self.client.ended_display_snapshot(TERMINAL)
        self.assertEqual(self.check.call_count, 2)
        self.client.request.assert_called_once_with("/sessions", timeout=3)
        self.assertEqual(self.client._pid_state.call_args_list, [unittest.mock.call(4242), unittest.mock.call(67890)])
        self.assertEqual(result["display_pid_state"], "dead")
        self.assertNotIn("PRIVATE_SECRET", json.dumps(result))

    def test_unavailable_registry_does_not_become_empty_absence_proof(self):
        self.client.request.side_effect = BrokerError("PRIVATE_SECRET")
        with self.assertRaises(BrokerError) as error:
            self.client.ended_display_snapshot(TERMINAL)
        self.assertNotIn("PRIVATE_SECRET", str(error.exception))

    def test_empty_duplicate_or_malformed_registry_is_rejected(self):
        for records in ([], [self.record, self.record], [None], [{"id": TERMINAL}], None):
            with self.subTest(records=records):
                self.client.request.return_value = {"sessions": records}
                with self.assertRaises(BrokerError):
                    self.client.ended_display_snapshot(TERMINAL)

    def test_health_auth_failure_or_changed_owner_during_query_is_rejected(self):
        for checks in ([{"state": "blocked"}], [{"state": "ready"}, {"state": "blocked"}]):
            with self.subTest(checks=checks):
                self.client._bound_check = unittest.mock.Mock(side_effect=checks)
                with self.assertRaises(BrokerError):
                    self.client.ended_display_snapshot(TERMINAL)


if __name__ == "__main__":
    unittest.main()
