"""Human-only native-worker continuity checks, without real SSH or PTYs."""
import copy
import hashlib
import json
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener
from unittest.mock import Mock, patch

from app import create_server
import mcp_bridge
from terminal_client import BrokerError
from terminal_hub import Hub
from tests.test_terminal_hub import FakeTerminals


TERMINAL = "ceohub-0123456789abcdef01234567"
SESSION = "11111111-2222-4333-8444-555555555555"
KEY = "legacy-plugin-original"
TMUX = {"name": TERMINAL, "session": "$0", "created": 1000,
        "pane": "%0", "pane_pid": 81126, "pane_start_ticks": 236237}
PROCESS = {"platform": "linux", "terminal_id": TERMINAL, "pane_id": "%0", "pane_pid": 81126,
           "pane_start_ticks": 236237, "pid": 87725, "start_ticks": 251867, "pgid": 87278,
           "tty_nr": 34818, "tty": "/dev/pts/2", "uid": 1003}


class WorkerContinuityFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.terminals = FakeTerminals()
        self.hub = Hub(self.temp.name, terminals=self.terminals)
        self.hub._stop.set()
        self.hub._watcher.join(3)
        self.hub.hosts = {"remote": {"transport": "ssh", "target": "fake316", "label": "Remote"}}
        row = self.terminals.add(TERMINAL, "remote")
        row.update(connected=False, state="exited", exit_code=255, human_busy=False, agent_ready=False)
        self.terminals.records[TERMINAL] = row
        self.hub.store.remember("remote", "worker", "Original", mode="terminal", terminal_id=TERMINAL, cwd="/project")
        self.owner = {"pid": 335884, "version": "0.3.0", "recovery_id": None}
        self.receipt = {"owner": self.owner, "reference_sha256": "a" * 64, "descriptor_sha256": "b" * 64,
                        "reference_identity": (1, 2, 3, 4, 5), "descriptor_identity": (1, 6, 7, 8, 9),
                        "owner_pid_state": "alive", "display_pid_state": "dead", "session": row}
        self.terminals.ended_display_snapshot = Mock(side_effect=lambda _: copy.deepcopy(self.receipt))
        self.terminals.bound_owner_status = lambda: self.owner | {"state": "ready", "running": True}
        self.binding = {"state": "bound", "binding_evidence": "held_rollout", "source": "cli",
                        "live_session_verified": True, "terminal_id": TERMINAL, "session_id": SESSION,
                        "process_identity": PROCESS, "rollout_identity": {"device": 200, "inode": 900},
                        "codex_home": "/home/user/.codex", "rollout_path": "/home/user/.codex/sessions/exact.jsonl",
                        "cwd": "/project", "process_cwd": "/project"}
        self.fresh = {key: copy.deepcopy(value) for key, value in self.binding.items()
                      if key not in ("live_session_verified", "terminal_id")}
        self.hub.history = Mock(side_effect=lambda *_: copy.deepcopy(self.fresh))
        self.hub._probe_remote_terminal = Mock(return_value={"ok": True, "exists": True, "identity": TMUX})
        self.binding_key = self.hub._output_key("remote", TERMINAL)
        self.hub.store.setting(self.binding_key, self.binding)
        self.operation = "agent-reconnect:" + KEY
        self.fingerprint = self.hub._reconnect_fingerprint("remote", "worker", TERMINAL, "plugin", None)
        self.previous = {"state": "unknown", "recovery": {"code": "reconnect_unknown", "message": "Original unknown",
                         "request_key": KEY, "request_origin": "plugin", "next_action": "inspect_connection"}}
        self.hub.store.begin_operation(self.operation, self.fingerprint)
        self.hub.store.finish_operation(self.operation, self.previous)
        self.recovery_key = self.hub._recovery_key("remote", "worker")
        self.hub.store.setting(self.recovery_key, self.previous["recovery"])

    def tearDown(self):
        self.hub.close()
        self.temp.cleanup()

    def reconcile(self):
        return self.hub.reconcile_original_worker("remote", "worker", KEY, confirm=True)

    def stored(self):
        row = self.hub.store.db.execute("select fingerprint,state,result from operations where request_key=?", (self.operation,)).fetchone()
        return dict(row) | {"result": json.loads(row["result"]) if row["result"] else None}

    def assert_fenced(self):
        with self.assertRaises((ValueError, PermissionError)) as error:
            self.reconcile()
        self.assertNotIn("PRIVATE_SECRET", str(error.exception))
        self.assertEqual(self.stored()["result"], self.previous)
        self.assertFalse(any(call[0] == "create" for call in self.terminals.calls))
        self.assertEqual(self.terminals.writes, [])


class WorkerContinuityTests(WorkerContinuityFixture, unittest.TestCase):
    def test_exact_native_continuity_without_marker_is_explicit_and_preserves_original_origin(self):
        result = self.reconcile()
        self.assertEqual(result["operation_state"], "blocked")
        recovery = result["recovery"]
        self.assertEqual(recovery["reconciliation_kind"], "worker_continuity")
        self.assertEqual(recovery["binding_basis"], "saved_binding_at_maintenance")
        self.assertIn("与已保存完整绑定一致", recovery["message"])
        self.assertIn("并非原请求时冻结的证据", recovery["message"])
        self.assertNotIn("精确会话连续性", recovery["message"])
        self.assertEqual(recovery["maintenance_actor"], "ui")
        self.assertEqual(recovery["original_origin"], "plugin")
        self.assertEqual(recovery["request_origin"], "plugin")
        self.assertEqual(self.stored()["fingerprint"], self.fingerprint)
        self.assertEqual(self.stored()["result"]["reconciliation_identity"], TMUX)
        self.assertEqual(self.stored()["result"]["reconciliation_worker_binding"], self.binding)
        self.assertEqual(self.terminals.ended_display_snapshot.call_count, 2)
        self.assertEqual(self.hub.history.call_count, 2)
        self.assertEqual(self.hub._probe_remote_terminal.call_count, 1)
        for call in self.hub.history.call_args_list:
            self.assertEqual(call.args, ("remote", {"op": "bind_output", "terminal_id": TERMINAL}))
        audit_key = "agent-reconnect-reconciliation:" + hashlib.sha256(self.operation.encode()).hexdigest()
        audit = self.hub.store.setting(audit_key)[0]
        self.assertEqual(audit["previous_result"], self.previous)
        self.assertEqual(audit["original_binding"], self.binding)
        self.assertEqual(audit["anchored_current_identity"], TMUX)
        self.assertEqual(audit["binding_basis"], "saved_binding_at_maintenance")
        self.assertIs(audit["original_request_binding_frozen"], False)
        self.assertFalse(any(call[0] == "create" for call in self.terminals.calls))
        self.assertEqual(self.terminals.writes, [])
        self.assertNotIn("process_identity", result)
        self.assertNotIn("reconciliation_identity", result)
        self.assertNotIn("reconciliation_worker_binding", result)
        self.assertNotIn("original_binding", result)
        count = self.hub.history.call_count
        self.assertEqual(self.reconcile()["recovery"], recovery)
        self.assertEqual(self.hub.history.call_count, count, "Same confirmed result is read back, not re-executed")

    def test_confirmation_is_exact_boolean_before_any_readonly_io(self):
        for value in (False, None, 1, "true"):
            with self.subTest(confirm=value), self.assertRaises(ValueError):
                self.hub.reconcile_original_worker("remote", "worker", KEY, confirm=value)
        self.assertEqual(self.hub.history.call_count, 0)
        self.assertEqual(self.terminals.ended_display_snapshot.call_count, 0)

    def test_existing_ui_marker_reconcile_cannot_be_used_for_plugin_key(self):
        with self.assertRaises(ValueError):
            self.hub.reconcile_reconnect("remote", "worker", KEY, confirm=True)
        self.assertEqual(self.stored()["result"], self.previous)
        self.assertEqual(self.hub.history.call_count, 0)

    def test_scoped_controller_fingerprint_is_not_a_legacy_maintenance_target(self):
        scoped = self.hub._reconnect_fingerprint("remote", "worker", TERMINAL, "plugin", "another-controller")
        with self.hub.store.db:
            self.hub.store.db.execute("update operations set fingerprint=? where request_key=?", (scoped, self.operation))
        self.assert_fenced()
        self.assertEqual(self.hub.history.call_count, 0)

    def test_ui_origin_fingerprint_is_not_impersonated_as_plugin(self):
        ui = self.hub._reconnect_fingerprint("remote", "worker", TERMINAL, "ui", None)
        with self.hub.store.db:
            self.hub.store.db.execute("update operations set fingerprint=? where request_key=?", (ui, self.operation))
        self.assert_fenced()

    def test_wrong_current_origin_key_or_state_stays_unknown(self):
        for change in ({"request_key": "another-key"}, {"request_origin": "ui"}, {"code": "route_unavailable"}):
            with self.subTest(change=change):
                self.hub.store.setting(self.recovery_key, self.previous["recovery"] | change)
                self.assert_fenced()

    def test_started_operation_is_never_reconciled(self):
        with self.hub.store.db:
            self.hub.store.db.execute("update operations set state='started',result=null where request_key=?", (self.operation,))
        with self.assertRaises(ValueError):
            self.reconcile()
        self.assertEqual(self.stored()["state"], "started")
        self.assertIsNone(self.stored()["result"])
        self.assertEqual(self.hub.history.call_count, 0)

    def test_missing_original_binding_is_not_replaced_by_fresh_or_newest_session(self):
        with self.hub.store.db:
            self.hub.store.db.execute("delete from settings where key=?", (self.binding_key,))
        self.assert_fenced()
        self.assertEqual(self.hub.history.call_count, 0)

    def test_weak_historical_partial_wrong_terminal_or_provider_binding_is_rejected(self):
        for change in ({"historical": True}, {"historical": None}, {"live_session_verified": False},
                       {"live_session_verified": 1}, {"binding_evidence": "explicit_resume"}, {"state": "resolved"},
                       {"terminal_id": "ceohub-ffffffffffffffffffffffff"}, {"source": "subagent"},
                       {"session_id": "not-exact"}, {"process_identity": {"pid": 87725}},
                       {"rollout_identity": {}}, {"codex_home": None}, {"rollout_path": "relative.jsonl"}):
            with self.subTest(change=change):
                self.hub.store.setting(self.binding_key, self.binding | change)
                self.assert_fenced()
        self.assertEqual(self.hub.history.call_count, 0)

    def test_process_identity_requires_full_immutable_native_and_pane_fields(self):
        for change in ({"pid": True}, {"uid": True}, {"start_ticks": 0}, {"pane_start_ticks": None},
                       {"tty": "PRIVATE_SECRET"}, {"terminal_id": "other"}, {"platform": "windows"}):
            with self.subTest(change=change):
                self.hub.store.setting(self.binding_key, self.binding | {"process_identity": PROCESS | change})
                self.assert_fenced()

    def test_closed_history_or_local_target_is_rejected(self):
        for mode in ("closed", "closing", "close_unknown", "history"):
            with self.subTest(mode=mode):
                self.hub.store.remember("remote", "worker", "Original", mode=mode)
                self.assert_fenced()
        self.hub.store.remember("remote", "worker", "Original", mode="terminal")
        self.hub.hosts["remote"]["transport"] = "local"
        self.assert_fenced()
        self.assertEqual(self.hub.history.call_count, 0)

    def test_pid_only_empty_registry_auth_failures_or_alive_display_are_rejected(self):
        original = copy.deepcopy(self.receipt)
        for change in ({"owner_pid_state": "unknown"}, {"owner_pid_state": "dead"},
                       {"display_pid_state": "alive"}, {"display_pid_state": "unknown"},
                       {"reference_identity": None}, {"descriptor_sha256": "not-a-proof"}, {"session": None}):
            with self.subTest(change=change):
                self.receipt = original | change
                self.assert_fenced()
        self.receipt = original
        for change in ({"connected": True}, {"state": "unknown"}, {"exit_code": None},
                       {"human_busy": True}, {"host": "other"}, {"target": "other"}, {"pid": None}):
            with self.subTest(change=change):
                self.receipt["session"] = original["session"] | change
                self.assert_fenced()

    def test_owner_stamp_must_match_original_authenticated_owner(self):
        self.previous["owner_stamp"] = {"pid": 12345, "recovery_id": None}
        self.hub.store.finish_operation(self.operation, self.previous)
        self.assert_fenced()

    def test_fresh_bind_must_hold_exact_original_session_process_and_rollout(self):
        for change in ({"state": "unbound"}, {"binding_evidence": "explicit_resume"},
                       {"session_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"},
                       {"process_identity": PROCESS | {"start_ticks": 251868}},
                       {"process_identity": PROCESS | {"pane_start_ticks": 236238}},
                       {"process_identity": PROCESS | {"uid": 1004}},
                       {"rollout_identity": {"device": 200, "inode": 901}},
                       {"rollout_path": "/home/user/.codex/sessions/other.jsonl"},
                       {"process_cwd": "/other-project"}):
            with self.subTest(change=change):
                self.hub.history.side_effect = lambda *_, change=change: self.fresh | change
                self.assert_fenced()

    def test_current_tmux_must_anchor_the_original_pane_process_not_just_the_name(self):
        for change in ({"name": "ceohub-ffffffffffffffffffffffff"}, {"pane": "%1"},
                       {"pane_pid": 81127}, {"pane_start_ticks": 236238}, {"created": True}):
            with self.subTest(change=change):
                self.hub._probe_remote_terminal.return_value = {"ok": True, "exists": True, "identity": TMUX | change}
                self.assert_fenced()

    def test_second_binding_check_rejects_in_tui_session_or_native_process_switch(self):
        for changed in (self.fresh | {"session_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"},
                        self.fresh | {"process_identity": PROCESS | {"pid": 99999}},
                        self.fresh | {"rollout_identity": {"device": 200, "inode": 999}}):
            with self.subTest(changed=changed):
                self.hub.history.side_effect = [copy.deepcopy(self.fresh), changed]
                self.assert_fenced()

    def test_before_after_owner_file_row_and_display_pid_state_must_be_exactly_equal(self):
        for mutate in (lambda x: x["owner"].update(pid=12345), lambda x: x.update(reference_identity=(1, 999, 3, 4, 5)),
                       lambda x: x.update(descriptor_identity=(1, 1000, 7, 8, 9)),
                       lambda x: x["session"].update(pid=5678), lambda x: x["session"].update(connected=True)):
            with self.subTest(mutate=mutate):
                after = copy.deepcopy(self.receipt)
                mutate(after)
                self.terminals.ended_display_snapshot.side_effect = [copy.deepcopy(self.receipt), after]
                self.assert_fenced()

    def test_raw_backend_errors_are_redacted_and_do_not_unlock(self):
        self.hub.history.side_effect = RuntimeError("PRIVATE_SECRET")
        self.assert_fenced()
        self.hub.history.side_effect = lambda *_: copy.deepcopy(self.fresh)
        self.terminals.ended_display_snapshot.side_effect = BrokerError("PRIVATE_SECRET")
        self.assert_fenced()

    def test_saved_binding_cas_change_during_read_keeps_original_unknown(self):
        def changed(*_):
            self.hub.store.setting(self.binding_key, self.binding | {"live_session_verified": False})
            return copy.deepcopy(self.fresh)
        self.hub.history.side_effect = changed
        self.assert_fenced()

    def test_operation_cas_change_is_not_overwritten(self):
        def changed(*_):
            self.hub.store.finish_operation(self.operation, {"state": "done", "recovery": {"code": "newer"}})
            return copy.deepcopy(self.fresh)
        self.hub.history.side_effect = changed
        with self.assertRaises(ValueError):
            self.reconcile()
        self.assertEqual(self.stored()["result"]["recovery"]["code"], "newer")
        self.assertEqual(self.hub.store.setting(self.recovery_key), self.previous["recovery"])

    def test_current_recovery_cas_change_rolls_back_operation_update(self):
        newer = self.previous["recovery"] | {"request_key": "newer"}
        def changed(*_):
            self.hub.store.setting(self.recovery_key, newer)
            return copy.deepcopy(self.fresh)
        self.hub.history.side_effect = changed
        with self.assertRaises(ValueError):
            self.reconcile()
        self.assertEqual(self.stored()["result"], self.previous)
        self.assertEqual(self.hub.store.setting(self.recovery_key), newer)

    def test_machine_configuration_or_registry_closure_during_read_is_rejected(self):
        def changed(*_):
            self.hub.hosts["remote"]["target"] = "other"
            return copy.deepcopy(self.fresh)
        self.hub.history.side_effect = changed
        self.assert_fenced()
        self.hub.hosts["remote"]["target"] = "fake316"
        def closed(*_):
            self.hub.store.remember("remote", "worker", "Original", mode="closed")
            return copy.deepcopy(self.fresh)
        self.hub.history.side_effect = closed
        self.assert_fenced()

    def test_same_plugin_key_retry_pins_current_anchored_tmux_without_business_input(self):
        self.reconcile()
        result = self.hub.reconnect_agent("remote", "worker", origin="plugin", request_key=KEY)
        self.assertEqual(result["operation_state"], "done")
        self.assertEqual(result["recovery"]["request_origin"], "plugin")
        creates = [call for call in self.terminals.calls if call[0] == "create"]
        self.assertEqual(len(creates), 1)
        self.assertEqual(self.hub.history.call_count, 4, "Native worker is checked twice during reconciliation and around actual attachment")
        self.assertTrue(creates[0][1]["cwd"].startswith("/.__pronhub_existing__/" + TERMINAL + "/"))
        self.assertEqual(self.terminals.writes, [])

    def test_ui_origin_cannot_retry_reconciled_plugin_operation(self):
        self.reconcile()
        with self.assertRaises(ValueError):
            self.hub.reconnect_agent("remote", "worker", origin="ui", request_key=KEY)
        self.assertFalse(any(call[0] == "create" for call in self.terminals.calls))

    def test_recreated_tmux_after_continuity_check_is_rejected_at_retry(self):
        self.reconcile()
        self.hub._probe_remote_terminal.return_value = {"ok": True, "exists": True, "identity": TMUX | {"created": 1001}}
        result = self.hub.reconnect_agent("remote", "worker", origin="plugin", request_key=KEY)
        self.assertEqual(result["recovery"]["code"], "original_session_changed")
        self.assertFalse(any(call[0] == "create" for call in self.terminals.calls))
        self.assertEqual(self.terminals.writes, [])

    def test_fresh_unknown_after_retry_does_not_keep_old_continuity_receipt(self):
        self.reconcile()
        with patch.object(self.terminals, "snapshot", side_effect=BrokerError("PRIVATE_SECRET")):
            result = self.hub.reconnect_agent("remote", "worker", origin="plugin", request_key=KEY)
        self.assertEqual(result["operation_state"], "unknown")
        self.assertNotIn("reconciliation_identity", self.stored()["result"])
        self.assertNotIn("reconciliation_worker_binding", self.stored()["result"])
        count = len([call for call in self.terminals.calls if call[0] == "create"])
        repeated = self.hub.reconnect_agent("remote", "worker", origin="plugin", request_key=KEY)
        self.assertEqual(repeated["operation_state"], "unknown")
        self.assertEqual(len([call for call in self.terminals.calls if call[0] == "create"]), count)

    def test_retry_requires_private_native_worker_binding_not_only_public_kind(self):
        self.reconcile()
        value = self.stored()["result"]
        value.pop("reconciliation_worker_binding")
        self.hub.store.finish_operation(self.operation, value)
        result = self.hub.reconnect_agent("remote", "worker", origin="plugin", request_key=KEY)
        self.assertEqual(result["operation_state"], "unknown")
        self.assertFalse(any(call[0] == "create" for call in self.terminals.calls))
        self.assertEqual(self.terminals.writes, [])

    def test_native_process_replaced_in_same_pane_before_retry_is_not_attached(self):
        self.reconcile()
        self.hub.history.side_effect = lambda *_: self.fresh | {"process_identity": PROCESS | {"pid": 88888, "start_ticks": 300000}}
        result = self.hub.reconnect_agent("remote", "worker", origin="plugin", request_key=KEY)
        self.assertEqual(result["recovery"]["code"], "original_session_changed")
        self.assertFalse(any(call[0] == "create" for call in self.terminals.calls))
        self.assertEqual(self.terminals.writes, [])

    def test_in_tui_session_switch_before_retry_is_not_treated_as_original_worker(self):
        self.reconcile()
        self.hub.history.side_effect = lambda *_: self.fresh | {"session_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"}
        result = self.hub.reconnect_agent("remote", "worker", origin="plugin", request_key=KEY)
        self.assertEqual(result["recovery"]["code"], "original_session_changed")
        self.assertFalse(any(call[0] == "create" for call in self.terminals.calls))

    def test_pre_attachment_native_read_failure_keeps_exact_private_receipts_for_same_key(self):
        self.reconcile()
        self.hub.history.side_effect = RuntimeError("PRIVATE_SECRET")
        result = self.hub.reconnect_agent("remote", "worker", origin="plugin", request_key=KEY)
        self.assertEqual(result["recovery"]["code"], "route_unavailable")
        self.assertTrue(result["recovery"]["retryable"])
        self.assertEqual(self.stored()["result"]["reconciliation_identity"], TMUX)
        self.assertEqual(self.stored()["result"]["reconciliation_worker_binding"], self.binding)
        self.assertNotIn("PRIVATE_SECRET", json.dumps(result))
        self.hub.history.side_effect = lambda *_: self.fresh | {"process_identity": PROCESS | {"start_ticks": 400000}}
        rejected = self.hub.reconnect_agent("remote", "worker", origin="plugin", request_key=KEY)
        self.assertEqual(rejected["recovery"]["code"], "original_session_changed")
        self.assertFalse(any(call[0] == "create" for call in self.terminals.calls))

    def test_post_attachment_native_change_does_not_claim_done_or_replay_attachment(self):
        self.reconcile()
        self.hub.history.side_effect = [copy.deepcopy(self.fresh), self.fresh | {"process_identity": PROCESS | {"pid": 99999}}]
        result = self.hub.reconnect_agent("remote", "worker", origin="plugin", request_key=KEY)
        self.assertEqual(result["operation_state"], "blocked")
        self.assertEqual(result["recovery"]["code"], "original_session_changed")
        self.assertNotIn("reconciliation_worker_binding", self.stored()["result"])
        self.assertEqual(len([call for call in self.terminals.calls if call[0] == "create"]), 1)
        again = self.hub.reconnect_agent("remote", "worker", origin="plugin", request_key=KEY)
        self.assertEqual(again["recovery"]["code"], "original_session_changed")
        self.assertEqual(len([call for call in self.terminals.calls if call[0] == "create"]), 1)
        self.assertEqual(self.terminals.writes, [])

    def test_post_attachment_native_read_failure_is_unknown_and_consumes_old_receipts(self):
        self.reconcile()
        self.hub.history.side_effect = [copy.deepcopy(self.fresh), RuntimeError("PRIVATE_SECRET")]
        result = self.hub.reconnect_agent("remote", "worker", origin="plugin", request_key=KEY)
        self.assertEqual(result["operation_state"], "unknown")
        self.assertNotIn("reconciliation_identity", self.stored()["result"])
        self.assertNotIn("reconciliation_worker_binding", self.stored()["result"])
        self.assertNotIn("PRIVATE_SECRET", json.dumps(result))
        self.assertEqual(len([call for call in self.terminals.calls if call[0] == "create"]), 1)
        self.assertEqual(self.terminals.writes, [])

    def test_successful_same_key_result_is_idempotent_without_extra_native_reads(self):
        self.reconcile()
        first = self.hub.reconnect_agent("remote", "worker", origin="plugin", request_key=KEY)
        count = self.hub.history.call_count
        again = self.hub.reconnect_agent("remote", "worker", origin="plugin", request_key=KEY)
        self.assertEqual(again["operation_state"], "done")
        self.assertEqual(again["recovery"], first["recovery"])
        self.assertEqual(self.hub.history.call_count, count)
        self.assertEqual(len([call for call in self.terminals.calls if call[0] == "create"]), 1)

    def test_mcp_tool_list_and_plugin_dispatch_do_not_expose_global_maintenance(self):
        self.assertNotIn("reconcile_original_worker", {tool["name"] for tool in mcp_bridge.TOOLS})
        with self.assertRaises((ValueError, PermissionError)):
            self.hub.plugin_call("reconcile_original_worker", {"agent_id": "remote::worker", "request_key": KEY, "confirm": True})
        self.assertEqual(self.hub.history.call_count, 0)


class WorkerContinuityHTTPTests(WorkerContinuityFixture, unittest.TestCase):
    """Global UI authority is separate from every MCP bearer capability."""
    def setUp(self):
        super().setUp()
        self.server = create_server(self.hub, 0, token="fixture-global-browser-token")
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.opener = build_opener(ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(3)
        super().tearDown()

    def request(self, headers, body=None, path="/api/agents/reconcile-original-worker"):
        body = body if body is not None else {"host": "remote", "thread_id": "worker", "request_key": KEY, "confirm": True}
        return self.opener.open(Request(self.server.origin + path, data=json.dumps(body).encode(),
                                       headers={"Content-Type": "application/json"} | headers), timeout=5)

    def test_http_cookie_and_exact_origin_global_ui_confirmation_are_required(self):
        valid = {"Cookie": "ceo_session=fixture-global-browser-token", "Origin": self.server.origin}
        for headers in ({}, {"Cookie": valid["Cookie"]}, {"Origin": valid["Origin"]},
                        {"Cookie": valid["Cookie"], "Origin": "http://attacker.invalid"},
                        {"Authorization": "Bearer fixture-global-browser-token"},
                        {"Authorization": "Bearer fixture-controller-token", "Origin": valid["Origin"]}):
            with self.subTest(headers=headers), self.assertRaises(HTTPError) as error:
                self.request(headers)
            self.assertEqual(error.exception.code, 403)
        self.assertEqual(self.hub.history.call_count, 0)
        with self.assertRaises(HTTPError) as error:
            self.request(valid, {"host": "remote", "thread_id": "worker", "request_key": KEY})
        self.assertEqual(error.exception.code, 400)
        self.assertEqual(self.hub.history.call_count, 0)
        with self.request(valid) as response:
            result = json.load(response)
        self.assertEqual(result["recovery"]["request_origin"], "plugin")
        self.assertEqual(result["recovery"]["maintenance_actor"], "ui")

    def test_global_plugin_bearer_cannot_call_maintenance_via_plugin_tool(self):
        with self.assertRaises(HTTPError) as error:
            self.request({"Authorization": "Bearer fixture-global-browser-token"},
                         {"tool": "reconcile_original_worker", "arguments": {"agent_id": "remote::worker", "request_key": KEY, "confirm": True}},
                         path="/api/plugin")
        self.assertEqual(error.exception.code, 400)
        self.assertEqual(self.hub.history.call_count, 0)


if __name__ == "__main__":
    unittest.main()
