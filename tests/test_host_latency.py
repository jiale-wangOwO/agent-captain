"""Passive SSH response observations; only inert subprocess and clocks run."""
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

from agent_captain.core.terminal_hub import HistoryReadError, Hub
from tests.test_terminal_hub import FakeTerminals


SECRET = "PRIVATE_DIAGNOSTIC_DO_NOT_EXPORT"


def reply(result=None, *, returncode=0, stderr="", stdout=None):
    return types.SimpleNamespace(returncode=returncode, stderr=stderr,
        stdout=json.dumps({"ok": True, "result": result or {"home": "/fixture", "history_exists": True}}) if stdout is None else stdout)


class HostLatencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.terminals = FakeTerminals()
        self.hub = Hub(Path(self.temp.name) / "hub", terminals=self.terminals)
        self.addCleanup(self.hub.close)
        self.hub._stop.set()
        self.hub._watcher.join(3)
        self.hub.hosts = {"local": {"transport": "local", "target": "", "label": "Local"},
                          "remote": {"transport": "ssh", "target": "fixture-route", "label": "Remote"}}
        self.clock = {"mono": 100.0, "wall": 1000.0}
        self.monotonic = patch("agent_captain.core.terminal_hub.time.monotonic", side_effect=lambda: self.clock["mono"])
        self.wall = patch("agent_captain.core.terminal_hub.time.time", side_effect=lambda: self.clock["wall"])
        self.monotonic.start()
        self.wall.start()
        self.addCleanup(self.monotonic.stop)
        self.addCleanup(self.wall.stop)

    def advance(self, seconds):
        self.clock["mono"] += seconds
        self.clock["wall"] += seconds

    def latency(self, host="remote"):
        return next(row["latency"] for row in self.hub.state()["hosts"] if row["id"] == host)

    def successful_read(self, seconds=2.5, request=None):
        def run(*_, **__):
            self.advance(seconds)
            return reply()
        with patch("agent_captain.core.terminal_hub.subprocess.run", side_effect=run) as mocked:
            value = self.hub.history("remote", request or {"op": "probe"})
        return value, mocked

    def test_state_is_only_cached_and_reports_unknown_or_local_without_fabricated_zero(self):
        with patch("agent_captain.core.terminal_hub.subprocess.run", side_effect=AssertionError("no SSH in state")) as run:
            remote, local = self.latency(), self.latency("local")
        run.assert_not_called()
        self.assertEqual(remote, {"state": "unknown", "metric": "ssh_response", "last_ms": None,
            "last_success_at": None, "last_attempt_at": None, "reason_code": None})
        self.assertEqual(local, remote | {"state": "local", "metric": "local"})
        self.assertEqual(self.terminals.writes, [])

    def test_success_records_existing_request_response_time_using_monotonic_not_wall_clock(self):
        def run(*_, **__):
            self.clock["mono"] += 2.75
            self.clock["wall"] -= 100  # wall adjustments do not affect duration
            return reply()
        with patch("agent_captain.core.terminal_hub.subprocess.run", side_effect=run) as mocked:
            self.hub.history("remote", {"op": "probe"})
        self.assertEqual(mocked.call_count, 1, "Recording adds no measurement SSH")
        self.assertEqual(self.latency(), {"state": "measured", "metric": "ssh_response", "last_ms": 2750,
            "last_success_at": 900.0, "last_attempt_at": 1000.0, "reason_code": None})

    def test_latest_failure_is_marked_failed_and_retains_only_explicitly_stale_success(self):
        self.successful_read(2.5)
        previous = self.latency()
        def run(*_, **__):
            self.advance(5)
            return reply(returncode=255, stdout=SECRET, stderr="kex_exchange_identification: " + SECRET)
        with patch("agent_captain.core.terminal_hub.subprocess.run", side_effect=run):
            with self.assertRaises(HistoryReadError):
                self.hub.history("remote", {"op": "probe"})
        failed = self.latency()
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(failed["reason_code"], "ssh_handshake_failed")
        self.assertEqual(failed["last_ms"], previous["last_ms"])
        self.assertEqual(failed["last_success_at"], previous["last_success_at"])
        self.assertEqual(failed["last_attempt_at"], 1002.5)
        self.assertNotIn(SECRET, json.dumps(self.hub.state()))

    def test_first_failure_has_no_success_measurement_or_synthetic_zero(self):
        with patch("agent_captain.core.terminal_hub.subprocess.run", side_effect=subprocess.TimeoutExpired(SECRET, 45)):
            with self.assertRaises(HistoryReadError):
                self.hub.history("remote", {"op": "probe"})
        value = self.latency()
        self.assertEqual(value["state"], "failed")
        self.assertEqual(value["reason_code"], "ssh_timeout")
        self.assertIsNone(value["last_ms"])
        self.assertIsNone(value["last_success_at"])

    def test_fixed_failure_codes_cover_auth_hostkey_protocol_and_remote_failures(self):
        for response, expected in ((reply(returncode=255, stderr="Permission denied (publickey) " + SECRET), "ssh_authentication_failed"),
                                   (reply(returncode=255, stderr="Host key verification failed " + SECRET), "ssh_host_key_failed"),
                                   (reply(stdout=SECRET), "protocol_invalid"),
                                   (reply(stdout=json.dumps({"ok": False, "error": SECRET})), "remote_read_failed")):
            with self.subTest(expected=expected):
                with patch("agent_captain.core.terminal_hub.subprocess.run", return_value=response):
                    with self.assertRaises(HistoryReadError):
                        self.hub.history("remote", {"op": "probe"})
                self.assertEqual(self.latency()["reason_code"], expected)
                self.assertNotIn(SECRET, json.dumps(self.latency()))

    def test_retry_success_measures_whole_history_and_preserves_deadlines_and_attempt_trace(self):
        calls = []
        def run(*_, **kwargs):
            calls.append(kwargs["timeout"])
            self.advance(3 if len(calls) == 1 else 2)
            if len(calls) == 1:
                raise subprocess.TimeoutExpired(SECRET, kwargs["timeout"])
            return reply({"state": "unbound", "reason": "no_exact_session_evidence"})
        with patch("agent_captain.core.terminal_hub.subprocess.run", side_effect=run):
            result = self.hub.history("remote", {"op": "read_bound_output"})
        self.assertEqual(calls, [75, 45])
        self.assertEqual(result["read_transport"]["attempts"], 2)
        self.assertEqual([attempt["reason_code"] for attempt in result["read_transport"]["attempt_trace"]], ["ssh_timeout", "ok"])
        self.assertEqual(result["read_transport"]["elapsed_ms"], 5000)
        self.assertEqual(self.latency(), {"state": "measured", "metric": "ssh_response", "last_ms": 5000,
            "last_success_at": 1005.0, "last_attempt_at": 1000.0, "reason_code": None})

    def test_all_existing_history_operations_are_observed_without_extra_subprocesses(self):
        operations = ("probe", "find", "read", "bind_output", "resolve_output", "read_output", "read_bound_output")
        for index, operation in enumerate(operations, 1):
            with self.subTest(operation=operation):
                _, mocked = self.successful_read(index / 10, {"op": operation})
                self.assertEqual(mocked.call_count, 1)
                self.assertEqual(self.latency()["last_ms"], index * 100)
        self.assertEqual(len(self.hub._host_latencies), 1)

    def test_connect_host_naturally_updates_measurement_once(self):
        def run(*_, **__):
            self.advance(1.25)
            return reply()
        with patch("agent_captain.core.terminal_hub.subprocess.run", side_effect=run) as mocked:
            value = self.hub.connect_host("remote")
        self.assertTrue(value["connected"])
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(self.latency()["last_ms"], 1250)

    def test_local_catalog_never_runs_ssh_or_records_zero_ms(self):
        with patch("agent_captain.core.terminal_hub.catalog.main", return_value={"home": "/fixture"}) as catalog, \
                patch("agent_captain.core.terminal_hub.subprocess.run", side_effect=AssertionError("no local SSH")) as run:
            self.assertEqual(self.hub.history("local", {"op": "probe"}), {"home": "/fixture"})
        catalog.assert_called_once()
        run.assert_not_called()
        self.assertEqual(self.latency("local")["state"], "local")
        self.assertIsNone(self.latency("local")["last_ms"])
        self.assertNotIn("local", self.hub._host_latencies)

    def test_label_update_preserves_measurement_but_route_or_transport_change_clears_it(self):
        self.successful_read()
        measured = self.latency()
        self.hub.upsert_host({"id": "remote", "transport": "ssh", "target": "fixture-route", "label": "Renamed"})
        self.assertEqual(self.latency(), measured)
        self.hub.upsert_host({"id": "remote", "transport": "ssh", "target": "new-fixture-route", "label": "Renamed"})
        self.assertEqual(self.latency()["state"], "unknown")
        self.assertIsNone(self.latency()["last_ms"])
        self.successful_read(1)
        self.hub.upsert_host({"id": "remote", "transport": "local", "target": "", "label": "Local replacement"})
        self.assertEqual(self.latency()["state"], "local")
        self.assertIsNone(self.latency()["last_ms"])

    def test_old_inflight_result_cannot_repopulate_a_changed_then_restored_route(self):
        def run(*_, **__):
            self.hub.upsert_host({"id": "remote", "transport": "ssh", "target": "temporary-route"})
            self.hub.upsert_host({"id": "remote", "transport": "ssh", "target": "fixture-route"})
            self.advance(2)
            return reply()
        with patch("agent_captain.core.terminal_hub.subprocess.run", side_effect=run):
            self.hub.history("remote", {"op": "probe"})
        self.assertEqual(self.latency()["state"], "unknown")
        self.assertIsNone(self.latency()["last_ms"])

    def test_old_inflight_failure_cannot_overwrite_a_newer_success_observation(self):
        entered, release = threading.Event(), threading.Event()
        errors = []
        def run(*_, **__):
            if threading.current_thread().name == "older-fixture-read":
                entered.set()
                if not release.wait(3):
                    raise AssertionError("Fixture was not released")
                self.advance(4)
                raise subprocess.TimeoutExpired(SECRET, 45)
            self.advance(2)
            return reply()
        def older():
            try:
                self.hub.history("remote", {"op": "probe"})
            except HistoryReadError as error:
                errors.append(error.reason_code)
        with patch("agent_captain.core.terminal_hub.subprocess.run", side_effect=run):
            thread = threading.Thread(target=older, name="older-fixture-read")
            thread.start()
            try:
                self.assertTrue(entered.wait(3))
                self.hub.history("remote", {"op": "probe"})
                latest = self.latency()
            finally:
                release.set()
                thread.join(3)
                self.assertFalse(thread.is_alive())
        self.assertEqual(errors, ["ssh_timeout"])
        self.assertEqual(self.latency(), latest)
        self.assertEqual(latest["state"], "measured")

    def test_state_returns_a_copy_and_cannot_corrupt_private_observation_cache(self):
        self.successful_read(1)
        value = self.latency()
        value.update(state="failed", reason_code=SECRET, last_ms=999999)
        self.assertEqual(self.latency()["state"], "measured")
        self.assertEqual(self.latency()["last_ms"], 1000)
        self.assertIsNone(self.latency()["reason_code"])


if __name__ == "__main__":
    unittest.main()
