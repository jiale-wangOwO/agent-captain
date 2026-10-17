"""Bounded slow-SSH reads and explicit pre-attachment retry, without live SSH."""
import importlib.util
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from agent_captain.integrations import mcp_bridge
from agent_captain.core.terminal_hub import Hub
from tests.test_terminal_hub import FakeTerminals
from tests.test_agent_management import TERMINAL, IDENTITY


class SlowSSHReconnectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.terminals = FakeTerminals()
        self.hub = Hub(self.temp.name, terminals=self.terminals)
        self.hub.hosts = {"remote": {"transport": "ssh", "target": "fake-ssh-target", "label": "Remote"}}
        self.terminals.add(TERMINAL, "remote")
        self.hub.store.remember("remote", "worker", "Worker", mode="terminal", terminal_id=TERMINAL, cwd="/work/project")
        self.terminals.records[TERMINAL].update(connected=False, state="exited", agent_ready=False)
        self.probes = []
        self.answer = {"ok": True, "exists": True, "identity": IDENTITY}
        self.hub._probe_remote_terminal = self.probe

    def tearDown(self):
        self.hub.close()
        self.temp.cleanup()

    def probe(self, host, terminal):
        self.probes.append((host, terminal))
        return self.answer

    def creates(self):
        return [call for call in self.terminals.calls if call[0] == "create"]

    def test_explicit_same_key_pre_attach_route_retry_remains_idempotent_after_success(self):
        self.answer = {"ok": False, "exists": False, "reason": "route_unavailable"}
        first = self.hub.reconnect_agent("remote", "worker", request_key="route")
        self.assertEqual(first["operation_state"], "blocked")
        self.assertTrue(first["recovery"]["retryable"])
        self.assertEqual(self.creates(), [])
        self.answer = {"ok": True, "exists": True, "identity": IDENTITY}
        recovered = self.hub.reconnect_agent("remote", "worker", request_key="route")
        self.assertEqual(recovered["operation_state"], "done")
        self.assertEqual(recovered["recovery"]["code"], "awaiting_foreground")
        self.assertEqual(len(self.probes), 3)
        self.assertEqual(len(self.creates()), 1)
        self.assertEqual(self.terminals.writes, [])
        self.hub.reconnect_agent("remote", "worker", request_key="route")
        self.assertEqual(len(self.probes), 3)
        self.assertEqual(len(self.creates()), 1)

    def test_known_legacy_pre_attach_route_receipt_can_retry_same_key(self):
        self.answer = {"ok": False, "exists": False, "reason": "route_unavailable"}
        first = self.hub.reconnect_agent("remote", "worker", request_key="legacy-route")
        recovery = dict(first["recovery"])
        recovery.pop("retryable")
        self.hub.store.finish_operation("agent-reconnect:legacy-route", {"state": "blocked", "recovery": recovery})
        self.answer = {"ok": True, "exists": True, "identity": IDENTITY}
        recovered = self.hub.reconnect_agent("remote", "worker", request_key="legacy-route")
        self.assertEqual(recovered["operation_state"], "done")
        self.assertEqual(len(self.creates()), 1)
        self.assertEqual(self.terminals.writes, [])

    def test_post_attach_route_failure_does_not_retry_same_key(self):
        with patch.object(self.terminals, "snapshot", return_value=dict(self.terminals.records[TERMINAL])):
            result = self.hub.reconnect_agent("remote", "worker", request_key="post-attach-route")
        self.assertEqual(result["operation_state"], "blocked")
        self.assertEqual(result["recovery"]["code"], "route_unavailable")
        count = len(self.probes)
        repeated = self.hub.reconnect_agent("remote", "worker", request_key="post-attach-route")
        self.assertEqual(repeated["operation_state"], "blocked")
        self.assertEqual(len(self.probes), count)
        self.assertEqual(len(self.creates()), 1)
        self.assertEqual(self.terminals.writes, [])

    def test_pre_attach_auth_or_hostkey_blocked_receipt_never_retries(self):
        for reason in ("ssh_auth_required", "ssh_host_key_required"):
            with self.subTest(reason=reason):
                self.answer = {"ok": False, "exists": False, "reason": reason}
                self.hub.reconnect_agent("remote", "worker", request_key=reason)
                count = len(self.probes)
                repeated = self.hub.reconnect_agent("remote", "worker", request_key=reason)
                self.assertEqual(repeated["recovery"]["code"], reason)
                self.assertEqual(len(self.probes), count)
        self.assertEqual(self.creates(), [])
        self.assertEqual(self.terminals.writes, [])

    def test_route_code_without_exact_pre_attach_receipt_never_retries(self):
        fingerprint = self.hub._reconnect_fingerprint("remote", "worker", TERMINAL, "ui", None)
        self.hub.store.begin_operation("agent-reconnect:unproven-route", fingerprint)
        self.hub.store.finish_operation("agent-reconnect:unproven-route", {"state": "blocked", "recovery": {
            "code": "route_unavailable", "message": "Unknown attachment stage", "next_action": "check_connection",
            "retryable": True, "request_key": "unproven-route", "request_origin": "ui"}})
        repeated = self.hub.reconnect_agent("remote", "worker", request_key="unproven-route")
        self.assertEqual(repeated["operation_state"], "blocked")
        self.assertEqual(self.probes, [])
        self.assertEqual(self.creates(), [])
        self.assertEqual(self.terminals.writes, [])

    def test_unknown_post_attach_probe_failure_never_replays_same_key(self):
        with patch.object(self.hub, "_probe_remote_terminal", side_effect=[self.answer,
                {"ok": False, "exists": False, "reason": "route_unavailable"}]) as probe:
            first = self.hub.reconnect_agent("remote", "worker", request_key="unknown-route")
            self.assertEqual(first["operation_state"], "unknown")
            repeated = self.hub.reconnect_agent("remote", "worker", request_key="unknown-route")
            self.assertEqual(repeated["operation_state"], "unknown")
            self.assertEqual(probe.call_count, 2)
        self.assertEqual(len(self.creates()), 1)
        self.assertEqual(self.terminals.writes, [])

    def test_pre_attach_receipt_must_match_same_key_and_origin(self):
        for key, receipt_key, origin in (("wrong-receipt-key", "another-key", "ui"),
                                         ("wrong-receipt-origin", "wrong-receipt-origin", "plugin")):
            with self.subTest(key=key):
                fingerprint = self.hub._reconnect_fingerprint("remote", "worker", TERMINAL, "ui", None)
                self.hub.store.begin_operation("agent-reconnect:" + key, fingerprint)
                self.hub.store.finish_operation("agent-reconnect:" + key, {"state": "blocked", "recovery": {
                    "code": "route_unavailable", "message": "SSH 路由暂不可达；请检查机器或恢复 SSH 上游/隧道路由，再由用户重试。不会自动重启服务。",
                    "next_action": "check_connection", "retryable": True,
                    "request_key": receipt_key, "request_origin": origin}})
                result = self.hub.reconnect_agent("remote", "worker", request_key=key)
                self.assertEqual(result["operation_state"], "blocked")
                self.assertEqual(self.probes, [])
                self.assertEqual(self.creates(), [])
                self.assertEqual(self.terminals.writes, [])


class SlowSSHMCPBudgetTests(unittest.TestCase):
    def service_timeout(self, name, arguments=None, bridge=mcp_bridge):
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = b'{"ok":true}'
        with patch.object(bridge, "read_connection", return_value=("http://127.0.0.1:39001", "fixture-capability-not-real")), \
                patch.object(bridge, "build_opener", return_value=opener):
            result = bridge.call_service(name, arguments or {})
        self.assertEqual(result, {"ok": True})
        opener.open.assert_called_once()
        return opener.open.call_args.kwargs["timeout"]

    def test_read_and_reconnect_have_distinct_bounded_ssh_budgets(self):
        for name, timeout in (("read_agent", 180), ("reconnect_agent", 240)):
            with self.subTest(tool=name):
                self.assertEqual(self.service_timeout(name, {"agent_id": "remote::worker", "request_key": "same-key"}), timeout)

    def test_reconnect_budget_covers_four_bounded_identity_checks_and_display_rpcs(self):
        ssh_checks = 4 * 45
        display_rpcs = 2 * 20
        registry_reads = 2 * 3
        self.assertEqual(ssh_checks + display_rpcs + registry_reads, 226)
        self.assertLess(ssh_checks + display_rpcs + registry_reads, self.service_timeout("reconnect_agent"))

    def test_machine_local_retained_plugin_source_uses_matching_budgets_without_reading_credentials(self):
        filename = (Path(__file__).resolve().parents[1] / ".runtime" / "plugin-marketplace-ceo-v0.7.0-release"
                    / "plugins" / "ceo-hub" / "mcp_bridge.py")
        if not filename.is_file():
            self.skipTest("Machine-local generated plugin source is not present in a clean checkout")
        spec = importlib.util.spec_from_file_location("slow_ssh_retained_plugin_fixture", filename)
        retained = importlib.util.module_from_spec(spec)
        # Script execution normally adds its own directory to sys.path. Do not
        # accidentally import a policy from the old flat checkout layout.
        with patch.dict(sys.modules), patch.object(sys, "path", [str(filename.parent), *sys.path]):
            spec.loader.exec_module(retained)
        for name in ("read_agent", "reconnect_agent", "send_message", "send_terminal_input", "update_agent", "poll_events"):
            with self.subTest(tool=name):
                self.assertEqual(self.service_timeout(name, bridge=retained), self.service_timeout(name))

    def test_deliveries_input_and_other_tools_keep_original_120_second_budget(self):
        for name in ("send_message", "send_terminal_input", "update_agent", "get_agent", "list_active_agents",
                     "create_terminal", "read_terminal_history", "get_delivery"):
            with self.subTest(tool=name):
                self.assertEqual(self.service_timeout(name), 120)

    def test_event_poll_keeps_its_original_distinct_budget(self):
        self.assertEqual(self.service_timeout("poll_events"), 65)
        self.assertEqual(self.service_timeout("poll_events", {"timeout_seconds": 30}), 65)


if __name__ == "__main__":
    unittest.main()
