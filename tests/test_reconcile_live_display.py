"""Resolve uncertain live attachment receipts without SSH writes or reattach."""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from agent_captain.core.terminal_hub import Hub
from agent_captain.transport.terminal_client import BrokerError, TerminalClient
from tests.test_terminal_hub import FakeTerminals


TERMINAL = "ceohub-0123456789abcdef01234567"
KEY = "same-original-ui-request"
IDENTITY = {"name": TERMINAL, "session": "$11", "created": 1234,
            "pane": "%20", "pane_pid": 777777, "pane_start_ticks": 888888}
SSH = str(Path(sys.executable).with_name("ssh.exe" if os.name == "nt" else "ssh"))


def marker(identity=IDENTITY):
    return "/.__pronhub_existing__/" + TERMINAL + "/" + base64.urlsafe_b64encode(
        json.dumps(identity, sort_keys=True).encode()).decode().rstrip("=")


class LiveDisplayReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.terminals = FakeTerminals()
        self.hub = Hub(self.temp.name, terminals=self.terminals)
        self.hub._stop.set()
        self.hub._watcher.join(3)
        self.hub.hosts = {"remote": {"transport": "ssh", "target": "fake316", "label": "Remote"}}
        record = self.terminals.add(TERMINAL, "remote")
        record.update(cwd=marker(), connected=True, state="agent", human_busy=False, created_at=123,
                      display_identity={"birth": "win-filetime:12345", "executable": SSH})
        self.terminals.records[TERMINAL] = record
        self.hub.store.remember("remote", "worker", "Original", mode="terminal", terminal_id=TERMINAL, cwd="/project")
        self.owner = {"pid": 67890, "version": "0.3.0", "recovery_id": "c" * 32}
        self.receipt = {"owner": self.owner, "reference_sha256": "a" * 64, "descriptor_sha256": "b" * 64,
                        "reference_identity": (1, 2, 3, 4, 5), "descriptor_identity": (1, 6, 7, 8, 9),
                        "owner_pid_state": "alive", "display_pid_state": "alive",
                        "display_identity": {"birth": "win-filetime:12345", "executable": SSH},
                        "session": {key: record.get(key) for key in
                                    ("id", "host", "transport", "target", "cwd", "pid", "connected", "created_at", "display_identity")}}
        self.terminals.bound_owner_status = Mock(return_value=self.owner | {"state": "ready", "running": True})
        self.terminals.live_display_snapshot = Mock(side_effect=lambda _: copy.deepcopy(self.receipt))
        self.hub._probe_remote_terminal = Mock(return_value={"ok": True, "exists": True, "identity": IDENTITY})
        self.operation = "agent-reconnect:" + KEY
        self.fingerprint = self.hub._reconnect_fingerprint("remote", "worker", TERMINAL, "ui", None)
        self.previous = {"state": "unknown", "owner_stamp": {key: self.owner[key] for key in ("pid", "recovery_id")},
                         "recovery": {"code": "reconnect_unknown", "message": "Original uncertain attachment",
                                      "request_key": KEY, "request_origin": "ui", "next_action": "inspect_connection"}}
        self.hub.store.begin_operation(self.operation, self.fingerprint)
        self.hub.store.finish_operation(self.operation, self.previous)
        self.recovery_key = self.hub._recovery_key("remote", "worker")
        self.hub.store.setting(self.recovery_key, self.previous["recovery"])

    def tearDown(self):
        self.hub.close()
        self.temp.cleanup()

    def reconcile(self):
        return self.hub.reconcile_reconnect("remote", "worker", KEY, confirm=True)

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

    def test_proven_existing_display_records_done_and_audits_unknown_without_retry(self):
        result = self.reconcile()
        self.assertEqual(result["operation_state"], "done")
        self.assertEqual(result["recovery"]["reconciliation_kind"], "live_display")
        self.assertEqual(result["recovery"]["next_action"], "open_terminal")
        self.assertFalse(result["recovery"]["retryable"])
        self.assertTrue(result["agent_ready"])
        self.assertEqual(self.stored()[1]["state"], "done")
        self.assertEqual(self.terminals.live_display_snapshot.call_count, 2)
        self.assertEqual(self.hub._probe_remote_terminal.call_count, 2)
        audit_key = "agent-reconnect-reconciliation:" + hashlib.sha256(self.operation.encode()).hexdigest()
        audit = self.hub.store.setting(audit_key)
        self.assertEqual(audit[0]["previous_result"], self.previous)
        self.assertEqual(audit[0]["remote_identity"], IDENTITY)
        self.assertEqual(self.reconcile()["recovery"], result["recovery"])
        self.assertEqual(self.terminals.live_display_snapshot.call_count, 2)
        repeated = self.hub.reconnect_agent("remote", "worker", request_key=KEY)
        self.assertEqual(repeated["operation_state"], "done")
        self.assertFalse(any(call[0] == "create" for call in self.terminals.calls))
        self.assertEqual(self.terminals.writes, [])
        for private in ("descriptor_sha256", ".__pronhub_existing__", "win-filetime"):
            self.assertNotIn(private, json.dumps(result))

    def test_metadata_offers_only_readonly_check_without_live_or_remote_reads(self):
        view = self.hub.get_agent("remote", "worker")
        self.assertTrue(view["recovery"]["reconcile_available"])
        self.assertEqual(view["recovery"]["reconcile_kind"], "live_display")
        self.assertEqual(view["recovery"]["reconcile_request_keys"], [KEY])
        self.assertEqual(self.stored(), ("done", self.previous))
        self.terminals.live_display_snapshot.assert_not_called()
        self.hub._probe_remote_terminal.assert_not_called()

    def test_confirmation_is_required_before_live_or_remote_reads(self):
        with self.assertRaises(ValueError):
            self.hub.reconcile_reconnect("remote", "worker", KEY)
        self.terminals.live_display_snapshot.assert_not_called()
        self.hub._probe_remote_terminal.assert_not_called()

    def test_same_key_reconnect_without_reconciliation_does_not_reattach(self):
        self.assertEqual(self.hub.reconnect_agent("remote", "worker", request_key=KEY)["operation_state"], "unknown")
        self.terminals.live_display_snapshot.assert_not_called()
        self.hub._probe_remote_terminal.assert_not_called()
        self.assertFalse(any(call[0] == "create" for call in self.terminals.calls))

    def test_missing_or_changed_original_owner_generation_remains_unknown(self):
        for stamp in (None, {}, {"pid": 67890, "recovery_id": None},
                      {"pid": 67891, "recovery_id": "c" * 32}, {"pid": 67890, "recovery_id": "d" * 32}):
            with self.subTest(stamp=stamp):
                self.previous["owner_stamp"] = stamp
                self.hub.store.finish_operation(self.operation, self.previous)
                self.assert_fenced()

    def test_other_origin_or_different_selected_request_is_not_reconciled(self):
        for change in ({"request_origin": "plugin"}, {"request_key": "newer-key"}, {"message": "newer-result"}):
            with self.subTest(change=change):
                self.hub.store.setting(self.recovery_key, self.previous["recovery"] | change)
                self.assert_fenced()

    def test_other_fingerprint_and_started_operation_remain_fenced(self):
        with self.hub.store.db:
            self.hub.store.db.execute("update operations set fingerprint='other' where request_key=?", (self.operation,))
        self.assert_fenced()
        with self.hub.store.db:
            self.hub.store.db.execute("update operations set fingerprint=?,state='started',result=null where request_key=?",
                                      (self.fingerprint, self.operation))
        with self.assertRaises(ValueError):
            self.reconcile()
        self.assertEqual(self.stored(), ("started", None))

    def test_live_record_requires_original_machine_connection_pid_and_marker(self):
        original = copy.deepcopy(self.receipt["session"])
        for change in ({"host": "other"}, {"target": "other"}, {"transport": "local"}, {"connected": False},
                       {"pid": None}, {"pid": True}, {"cwd": "/project"}, {"cwd": marker() + "="},
                       {"display_identity": None}, {"display_identity": {"birth": "win-filetime:54321", "executable": SSH}},
                       {"cwd": marker(IDENTITY | {"created": True})}):
            with self.subTest(change=change):
                self.receipt["session"] = original | change
                self.assert_fenced()
        self.receipt["session"] = original

    def test_owner_and_local_ssh_process_must_be_definitively_alive(self):
        for key in ("owner_pid_state", "display_pid_state"):
            for state in ("dead", "unknown", None):
                with self.subTest(key=key, state=state):
                    self.receipt[key] = state
                    self.assert_fenced()
            self.receipt[key] = "alive"
        for process in ({"birth": "unknown", "executable": SSH}, {"birth": "win-filetime:12345", "executable": sys.executable}):
            self.receipt["display_identity"] = process
            self.assert_fenced()

    def test_either_remote_probe_failure_or_identity_change_preserves_unknown(self):
        original = {"ok": True, "exists": True, "identity": IDENTITY}
        for changed in ({"ok": False, "exists": False}, {"ok": True, "exists": False},
                        original | {"identity": IDENTITY | {"created": 5678}},
                        original | {"identity": IDENTITY | {"pane_start_ticks": 999999}}):
            for replies in ([changed], [original, changed]):
                with self.subTest(replies=replies):
                    self.hub._probe_remote_terminal = Mock(side_effect=replies)
                    self.assert_fenced()

    def test_broker_reference_generation_or_local_display_birth_races_remain_unknown(self):
        original = copy.deepcopy(self.receipt)
        mutations = [lambda x: x["owner"].update(pid=67891), lambda x: x["owner"].update(recovery_id="d" * 32),
                     lambda x: x.update(reference_sha256="e" * 64), lambda x: x.update(descriptor_sha256="f" * 64),
                     lambda x: x.update(reference_identity=(1, 2, 3, 4, 6)), lambda x: x["session"].update(pid=4243),
                     lambda x: x["session"].update(connected=False), lambda x: x["display_identity"].update(birth="win-filetime:12346")]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                after = copy.deepcopy(original)
                mutate(after)
                self.terminals.live_display_snapshot = Mock(side_effect=[copy.deepcopy(original), after])
                self.assert_fenced()

    def test_unavailable_receipts_and_probe_exceptions_never_leak_diagnostics(self):
        self.terminals.live_display_snapshot = Mock(side_effect=BrokerError("PRIVATE_SECRET"))
        self.assert_fenced()
        self.terminals.live_display_snapshot = Mock(side_effect=lambda _: copy.deepcopy(self.receipt))
        self.hub._probe_remote_terminal = Mock(side_effect=RuntimeError("PRIVATE_SECRET"))
        self.assert_fenced()

    def test_operation_cas_race_is_preserved_and_selected_update_rolls_back(self):
        newer = {"state": "done", "recovery": {"code": "newer-result"}}
        def probe(*_):
            self.hub.store.finish_operation(self.operation, newer)
            return {"ok": True, "exists": True, "identity": IDENTITY}
        self.hub._probe_remote_terminal = Mock(side_effect=probe)
        with self.assertRaises(ValueError):
            self.reconcile()
        self.assertEqual(self.stored(), ("done", newer))
        self.assertEqual(self.hub.store.setting(self.recovery_key), self.previous["recovery"])

    def test_selected_recovery_cas_race_rolls_back_completed_operation(self):
        newer = self.previous["recovery"] | {"request_key": "newer-key"}
        def probe(*_):
            self.hub.store.setting(self.recovery_key, newer)
            return {"ok": True, "exists": True, "identity": IDENTITY}
        self.hub._probe_remote_terminal = Mock(side_effect=probe)
        self.assert_fenced()
        self.assertEqual(self.hub.store.setting(self.recovery_key), newer)

    def test_changed_enrollment_or_host_config_during_check_is_not_overwritten(self):
        for mutate in (lambda: self.hub.store.remember("remote", "worker", "Changed", terminal_id="ceohub-111111111111111111111111"),
                       lambda: self.hub.hosts["remote"].update(target="other")):
            with self.subTest(mutate=mutate):
                def probe(*_):
                    mutate()
                    return {"ok": True, "exists": True, "identity": IDENTITY}
                self.hub._probe_remote_terminal = Mock(side_effect=probe)
                self.assert_fenced()
                self.hub.store.remember("remote", "worker", "Original", terminal_id=TERMINAL)
                self.hub.hosts["remote"]["target"] = "fake316"

    def legacy_plugin_request(self, controller_id=None):
        self.previous["recovery"]["request_origin"] = "plugin"
        self.fingerprint = self.hub._reconnect_fingerprint("remote", "worker", TERMINAL, "plugin", controller_id)
        with self.hub.store.db:
            self.hub.store.db.execute("update operations set fingerprint=?,result=? where request_key=?",
                                      (self.fingerprint, json.dumps(self.previous), self.operation))
        self.hub.store.setting(self.recovery_key, self.previous["recovery"])

    def test_legacy_plugin_live_check_preserves_source_and_finishes_without_binding_or_retry(self):
        self.legacy_plugin_request()
        view = self.hub.get_agent("remote", "worker")
        self.assertEqual(view["recovery"]["next_action"], "reconcile_original_worker")
        self.assertEqual(view["recovery"]["reconcile_kind"], "live_display")
        with self.assertRaises(ValueError):
            self.reconcile()
        result = self.hub.reconcile_original_worker("remote", "worker", KEY, confirm=True)
        self.assertEqual(result["operation"], "reconcile_original_worker")
        self.assertEqual(result["operation_state"], "done")
        recovery = result["recovery"]
        self.assertEqual(recovery["request_origin"], "plugin")
        self.assertEqual(recovery["original_origin"], "plugin")
        self.assertEqual(recovery["maintenance_actor"], "ui")
        self.assertFalse(recovery["retryable"])
        self.assertEqual(recovery["reconciliation_kind"], "live_display")
        audit_key = "agent-reconnect-reconciliation:" + hashlib.sha256(self.operation.encode()).hexdigest()
        self.assertEqual(self.hub.store.setting(audit_key)[0]["previous_result"], self.previous)
        self.assertEqual(self.hub.reconcile_original_worker("remote", "worker", KEY, confirm=True)["recovery"], recovery)
        repeated = self.hub.reconnect_agent("remote", "worker", origin="plugin", request_key=KEY)
        self.assertEqual(repeated["operation_state"], "done")
        self.assertEqual(self.terminals.live_display_snapshot.call_count, 2)
        self.assertEqual(self.hub._probe_remote_terminal.call_count, 2)
        self.assertEqual(self.terminals.writes, [])
        self.assertFalse(any(call[0] == "create" for call in self.terminals.calls))

    def test_controller_specific_live_request_cannot_be_reconciled_as_legacy_plugin(self):
        self.legacy_plugin_request(controller_id="other-controller")
        self.assertFalse(self.hub.get_agent("remote", "worker")["recovery"].get("reconcile_available", False))
        with self.assertRaises(ValueError):
            self.hub.reconcile_original_worker("remote", "worker", KEY, confirm=True)
        self.assertEqual(self.stored(), ("done", self.previous))
        self.terminals.live_display_snapshot.assert_not_called()
        self.hub._probe_remote_terminal.assert_not_called()

    def recovered_plugin_request(self, controller_id=None):
        self.legacy_plugin_request(controller_id)
        new_owner = {"pid": 67891, "version": "0.3.0", "recovery_id": "d" * 32}
        def recover(**_):
            self.terminals.records.clear()
            self.terminals.bound_owner_status.return_value = new_owner | {"state": "ready", "running": True}
            return new_owner | {"state": "ready", "running": True, "owner_recovered": True, "started_empty": True,
                                "previous_pid": self.owner["pid"], "previous_recovery_id": self.owner["recovery_id"]}
        self.terminals.recover_bound_owner = Mock(side_effect=recover)
        self.terminals.request = Mock(return_value={"sessions": []})
        self.hub.recover_terminal_service(confirm=True)
        return self.hub.store.setting("terminal-owner-recovery-proof")

    def test_dead_owner_proof_can_reconcile_only_original_legacy_plugin_key(self):
        proof = self.recovered_plugin_request()
        self.assertEqual(proof["operations"], {self.operation: self.fingerprint})
        self.assertEqual(proof["previous_recovery_id"], self.owner["recovery_id"])
        view = self.hub.get_agent("remote", "worker")
        self.assertEqual(view["recovery"]["next_action"], "reconcile_original_worker")
        result = self.hub.reconcile_original_worker("remote", "worker", KEY, confirm=True)
        self.assertEqual(result["operation_state"], "blocked")
        self.assertEqual(result["recovery"]["reconciliation_kind"], "owner_recovery")
        self.assertEqual(result["recovery"]["request_origin"], "plugin")
        self.assertTrue(result["recovery"]["retryable"])
        self.terminals.live_display_snapshot.assert_not_called()
        self.hub._probe_remote_terminal.assert_not_called()
        self.assertFalse(any(call[0] == "create" for call in self.terminals.calls))
        audit_key = "agent-reconnect-reconciliation:" + hashlib.sha256(self.operation.encode()).hexdigest()
        self.assertEqual(self.hub.store.setting(audit_key)[0]["previous_result"], self.previous)
        self.assertEqual(self.hub.reconcile_original_worker("remote", "worker", KEY, confirm=True)["recovery"], result["recovery"])
        retried = self.hub.reconnect_agent("remote", "worker", origin="plugin", request_key=KEY)
        self.assertEqual(retried["operation_state"], "done")
        self.assertEqual(len([call for call in self.terminals.calls if call[0] == "create"]), 1)
        self.assertEqual(self.terminals.writes, [])
        self.assertEqual(self.hub.store.setting("terminal-owner-recovery-proof")["operations"], {})

    def test_dead_owner_proof_never_selects_controller_specific_plugin_fingerprint(self):
        proof = self.recovered_plugin_request(controller_id="other-controller")
        self.assertEqual(proof["operations"], {})
        with self.assertRaises(ValueError):
            self.hub.reconcile_original_worker("remote", "worker", KEY, confirm=True)
        self.assertEqual(self.stored(), ("done", self.previous))

    def test_recovered_plugin_requires_empty_authenticated_registry_and_exact_generation(self):
        proof = self.recovered_plugin_request()
        for registry in ({"sessions": [{"id": "other"}]}, {"sessions": None}, {}, None):
            with self.subTest(registry=registry):
                self.terminals.request.return_value = registry
                with self.assertRaises(ValueError):
                    self.hub.reconcile_original_worker("remote", "worker", KEY, confirm=True)
                self.assertEqual(self.stored(), ("done", self.previous))
        self.terminals.request.return_value = {"sessions": []}
        self.terminals.bound_owner_status.return_value["recovery_id"] = "e" * 32
        with self.assertRaises(ValueError):
            self.hub.reconcile_original_worker("remote", "worker", KEY, confirm=True)
        self.assertEqual(self.stored(), ("done", self.previous))
        self.hub.store.setting("terminal-owner-recovery-proof", proof | {"previous_recovery_id": "f" * 32})
        with self.assertRaises(ValueError):
            self.hub.reconcile_original_worker("remote", "worker", KEY, confirm=True)
        self.assertEqual(self.stored(), ("done", self.previous))

    def test_recovered_plugin_proof_race_and_selected_request_race_are_not_overwritten(self):
        proof = self.recovered_plugin_request()
        newer = self.previous["recovery"] | {"request_key": "newer-key"}
        def registry(*_, **__):
            self.hub.store.setting(self.recovery_key, newer)
            return {"sessions": []}
        self.terminals.request.side_effect = registry
        with self.assertRaises(ValueError):
            self.hub.reconcile_original_worker("remote", "worker", KEY, confirm=True)
        self.assertEqual(self.stored(), ("done", self.previous))
        self.assertEqual(self.hub.store.setting(self.recovery_key), newer)
        self.hub.store.setting(self.recovery_key, self.previous["recovery"])
        def changed_proof(*_, **__):
            self.hub.store.setting("terminal-owner-recovery-proof", proof | {"operations": {}})
            return {"sessions": []}
        self.terminals.request.side_effect = changed_proof
        with self.assertRaises(ValueError):
            self.hub.reconcile_original_worker("remote", "worker", KEY, confirm=True)
        self.assertEqual(self.stored(), ("done", self.previous))


class LiveDisplaySnapshotTests(unittest.TestCase):
    def setUp(self):
        self.client = TerminalClient()
        self.record = {"id": TERMINAL, "pid": 4242, "host": "remote", "transport": "ssh", "target": "fake316",
                       "connected": True, "cwd": marker(), "created_at": 123, "current_seq": 999,
                       "display_identity": {"birth": "win-filetime:12345", "executable": SSH}}
        self.context = {"data": {"pid": 67890, "version": "0.3.0", "recovery_id": "c" * 32, "token": "PRIVATE_SECRET"},
                        "reference": {"sha256": "a" * 64, "identity": (1, 2, 3, 4, 5)},
                        "descriptor": {"sha256": "b" * 64, "identity": (1, 6, 7, 8, 9)}}
        self.client._bound_context = Mock(return_value=self.context)
        self.client._bound_check = Mock(return_value={"state": "ready"})
        self.client._owner_state = Mock(return_value="alive")
        self.client.request = Mock(return_value={"sessions": [self.record]})
        self.process = {"state": "alive", "birth": "win-filetime:12345", "executable": SSH}
        self.patch = patch("agent_captain.transport.terminal_client.observe_process", return_value=self.process)
        self.observe = self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_authenticates_both_sides_and_returns_only_stable_attachment_evidence(self):
        result = self.client.live_display_snapshot(TERMINAL)
        self.assertEqual(self.client._bound_check.call_count, 2)
        self.client.request.assert_called_once_with("/sessions", timeout=3)
        self.observe.assert_called_once_with(4242)
        self.assertEqual(result["display_identity"], {key: self.process[key] for key in ("birth", "executable")})
        self.assertNotIn("current_seq", result["session"])
        self.assertNotIn("PRIVATE_SECRET", json.dumps(result))

    def test_empty_duplicate_malformed_or_disconnected_registry_is_rejected(self):
        for records in ([], [self.record, self.record], [None], [self.record | {"connected": False}],
                        [self.record | {"pid": True}], [self.record | {"transport": "local"}], None):
            with self.subTest(records=records):
                self.client.request.return_value = {"sessions": records}
                with self.assertRaises(BrokerError):
                    self.client.live_display_snapshot(TERMINAL)

    def test_missing_or_changed_frozen_child_birth_cannot_be_replaced_by_current_pid(self):
        for frozen in (None, {}, self.record["display_identity"] | {"birth": "win-filetime:54321"},
                       self.record["display_identity"] | {"executable": sys.executable}):
            with self.subTest(frozen=frozen):
                self.client.request.return_value = {"sessions": [self.record | {"display_identity": frozen}]}
                with self.assertRaises(BrokerError):
                    self.client.live_display_snapshot(TERMINAL)

    def test_failed_auth_unavailable_registry_or_dead_process_does_not_create_absence_proof(self):
        for checks in ([{"state": "blocked"}], [{"state": "ready"}, {"state": "blocked"}]):
            self.client._bound_check = Mock(side_effect=checks)
            with self.assertRaises(BrokerError):
                self.client.live_display_snapshot(TERMINAL)
        self.client._bound_check = Mock(return_value={"state": "ready"})
        self.client.request.side_effect = BrokerError("PRIVATE_SECRET")
        with self.assertRaises(BrokerError) as error:
            self.client.live_display_snapshot(TERMINAL)
        self.assertNotIn("PRIVATE_SECRET", str(error.exception))
        self.client.request.side_effect = None
        for process in ({"state": "dead"}, {"state": "unknown"}, self.process | {"executable": sys.executable}):
            self.observe.return_value = process
            with self.assertRaises(BrokerError):
                self.client.live_display_snapshot(TERMINAL)


if __name__ == "__main__":
    unittest.main()
