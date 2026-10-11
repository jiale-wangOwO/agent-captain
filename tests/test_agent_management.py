"""Connected-agent management contracts, isolated registry and fake transport."""
import base64
import json
import sqlite3
import tempfile
import threading
import types
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from terminal_hub import Hub
from terminal_client import BrokerError
from store import Store
from tests.test_terminal_hub import FakeTerminals
from tests.test_controller_scope import Controllers
from app import create_server
from urllib.request import Request, ProxyHandler, build_opener
from urllib.error import HTTPError

TERMINAL = "ceohub-0123456789abcdef01234567"
IDENTITY = {"name": TERMINAL, "session": "$11", "created": 1234, "pane": "%20", "pane_pid": 777777, "pane_start_ticks": 888888}


class AgentManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.terminals = FakeTerminals()
        self.hub = Hub(self.temp.name, terminals=self.terminals)
        self.hub.hosts = {"local": {"transport": "local", "label": "Local machine"},
            "remote": {"transport": "ssh", "target": "fake-ssh-target", "label": "Remote machine"}}
        self.terminals.add(TERMINAL, "remote")
        self.hub.store.remember("remote", "worker", "Old title", active=True, mode="terminal", terminal_id=TERMINAL, cwd="/work/project")
        self.probes = []
        self.answer = {"ok": True, "exists": True, "identity": IDENTITY}
        self.hub._probe_remote_terminal = self.probe

    def tearDown(self):
        self.hub.close()
        self.temp.cleanup()

    def probe(self, host, terminal):
        self.probes.append((host, terminal))
        return self.answer

    def disconnected(self):
        self.terminals.records[TERMINAL].update(connected=False, state="exited", agent_ready=False)

    def creates(self):
        return [c for c in self.terminals.calls if c[0] == "create"]

    def test_metadata_changes_only_alias_and_pinned_and_persists(self):
        self.hub.store.setting(self.hub._output_key("remote", TERMINAL), {"session_id": "11111111-1111-4111-8111-111111111111", "method": "manual"})
        before = self.hub.store.agent("remote", "worker")
        after = self.hub.update_agent("remote", "worker", alias=" New title ", pinned=True, request_key="metadata-1")
        self.assertEqual(after["alias"], "New title")
        self.assertTrue(after["pinned"])
        self.assertEqual(after["host_label"], "Remote machine")
        stored = self.hub.store.agent("remote", "worker")
        self.assertEqual({k: v for k, v in before.items() if k not in ("alias", "pinned")}, {k: v for k, v in stored.items() if k not in ("alias", "pinned")})
        self.assertEqual(self.hub.get_agent("remote", "worker")["output_binding"]["session_id"], "11111111-1111-4111-8111-111111111111")
        db = Store(Path(self.temp.name) / "hub.sqlite")
        self.assertEqual(db.agent("remote", "worker")["pinned"], 1)
        db.close()
        self.assertEqual(self.terminals.writes, [])
        self.assertEqual(self.probes, [])

    def test_metadata_stable_key_replay_and_body_conflict(self):
        first = self.hub.update_agent("remote", "worker", alias="New", pinned=True, request_key="same")
        repeated = self.hub.update_agent("remote", "worker", alias="New", pinned=True, request_key="same")
        self.assertEqual(first["alias"], repeated["alias"])
        with self.assertRaises(ValueError):
            self.hub.update_agent("remote", "worker", alias="Different", pinned=True, request_key="same")

    def test_metadata_validation_rejects_controls_empty_oversized_and_non_boolean(self):
        for alias in ("", "  ", "bad\nname", "x" * 201):
            with self.assertRaises(ValueError):
                self.hub.update_agent("remote", "worker", alias=alias)
        for value in (0, 1, "true", [], {}):
            with self.assertRaises(ValueError):
                self.hub.update_agent("remote", "worker", pinned=value)
        with self.assertRaises(ValueError):
            self.hub.update_agent("remote", "worker")

    def test_ui_metadata_keeps_stored_activation_but_open_worker_is_automatically_managed(self):
        self.hub.store.set_active("remote", "worker", False)
        result = self.hub.update_agent("remote", "worker", pinned=True)
        self.assertTrue(result["active"])
        self.assertTrue(result["pinned"])
        self.assertEqual(self.hub.store.agent("remote", "worker")["active"], 0)

    def test_plugin_metadata_and_reconnect_ignore_worker_grants_but_enforce_controller_identity(self):
        self.hub.controllers.shutdown()
        self.hub.controllers = Controllers(self.hub)
        self.hub.controllers.grants["a"] = {"remote::worker"}
        self.hub.controllers.grants["b"] = set()
        updated = self.hub.plugin_call("update_agent", {"agent_id": "remote::worker", "alias": "Scoped", "request_key": "a-update"}, controller_id="a")
        self.assertEqual(updated["alias"], "Scoped")
        # Both authenticated ready managers can use connected workers without
        # selecting old per-worker grants; revoking manager identity still
        # fences every tool, independent of automatic worker access.
        updated_b = self.hub.plugin_call("update_agent", {"agent_id": "remote::worker", "alias": "Shared", "request_key": "b-update"}, controller_id="b")
        self.assertEqual(updated_b["alias"], "Shared")
        self.hub.controllers.active.remove("b")
        for tool, kwargs in (("update_agent", {"alias": "No"}), ("reconnect_agent", {})):
            with self.assertRaises(PermissionError):
                self.hub.plugin_call(tool, {"agent_id": "remote::worker", "request_key": "b-request", **kwargs}, controller_id="b")
        self.hub.store.set_active("remote", "worker", False)
        result = self.hub.plugin_call("reconnect_agent", {"agent_id": "remote::worker", "request_key": "inactive"}, controller_id="a")
        self.assertTrue(result["active"])
        self.assertEqual(result["recovery"]["code"], "already_connected")
        self.assertEqual(self.probes, [])
        self.assertEqual(self.creates(), [])

    def test_connected_reconnect_noop_never_probes_or_inputs(self):
        result = self.hub.reconnect_agent("remote", "worker", request_key="connected")
        self.assertTrue(result["connected"])
        self.assertEqual(result["recovery"]["code"], "already_connected")
        self.assertEqual(result["operation_state"], "done")
        self.assertEqual(self.probes, [])
        self.assertEqual(self.creates(), [])
        self.assertEqual(self.terminals.writes, [])

    def test_connected_noop_key_remains_noop_after_disconnect_and_rejects_other_target(self):
        self.hub.reconnect_agent("remote", "worker", request_key="noop-key")
        self.disconnected()
        replay = self.hub.reconnect_agent("remote", "worker", request_key="noop-key")
        self.assertEqual(replay["operation_state"], "done")
        self.assertFalse(replay["connected"])
        self.assertEqual(self.creates(), [])
        self.hub.store.remember("remote", "other", "Other", active=True, mode="terminal", terminal_id=TERMINAL, cwd="/work/project")
        with self.assertRaises(ValueError):
            self.hub.reconnect_agent("remote", "other", request_key="noop-key")

    def test_human_typing_label_does_not_peek_or_guess_task_progress(self):
        self.terminals.records[TERMINAL]["human_busy"] = True
        result = self.hub.get_agent("remote", "worker")
        self.assertTrue(result["human_busy"])
        self.assertEqual(result["management"]["status"], "human_busy")
        self.assertEqual(result["management"]["label"], "你正在输入")
        self.assertFalse(result["management"]["needs_attention"])
        self.assertEqual(self.probes, [])

    def test_unconfirmed_recovery_warns_even_if_broker_cached_connected_ready(self):
        for code in ("original_session_missing", "original_session_changed", "reconnect_unknown"):
            self.hub.store.setting(self.hub._recovery_key("remote", "worker"),
                {"code": code, "message": "Verify original display", "next_action": "inspect_connection"})
            result = self.hub.get_agent("remote", "worker")
            self.assertTrue(result["connected"], "Physical cached connection must not be falsified")
            self.assertTrue(result["agent_ready"])
            self.assertEqual(result["management"]["tone"], "warning")
            self.assertTrue(result["management"]["needs_attention"])
            self.assertEqual(result["management"]["primary_action"], "inspect_connection")
            self.assertNotEqual(result["management"]["status"], "ready")

    def test_disconnected_reconnect_exact_original_and_no_model_input(self):
        self.disconnected()
        result = self.hub.reconnect_agent("remote", "worker", request_key="reconnect-1")
        self.assertTrue(result["connected"])
        self.assertEqual(result["terminal_id"], TERMINAL)
        self.assertEqual(result["cwd"], "/work/project")
        self.assertEqual(result["operation_state"], "done")
        self.assertEqual(self.probes, [("remote", TERMINAL)] * 2)
        body = self.creates()[0][1]
        self.assertEqual(body["session_id"], TERMINAL)
        self.assertTrue(body["cwd"].startswith("/.__pronhub_existing__/" + TERMINAL + "/"))
        encoded = body["cwd"].rsplit("/", 1)[1]
        self.assertEqual(json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))), IDENTITY)
        self.assertEqual(self.hub.store.agent("remote", "worker")["cwd"], "/work/project")
        self.assertEqual(self.terminals.writes, [])

    def test_missing_original_tmux_fails_closed_and_never_creates_shell(self):
        self.disconnected()
        self.answer = {"ok": True, "exists": False, "reason": "session_missing"}
        result = self.hub.reconnect_agent("remote", "worker", request_key="missing")
        self.assertEqual(result["recovery"]["code"], "original_session_missing")
        self.assertEqual(result["operation_state"], "blocked")
        self.assertNotIn("error", result)
        self.assertEqual(self.creates(), [])
        self.assertEqual(self.terminals.writes, [])

    def test_ui_input_is_fenced_only_during_unconfirmed_display_attachment(self):
        self.disconnected()
        create = self.terminals.create
        def fenced(body):
            with self.assertRaisesRegex(ValueError, "正在核验"):
                self.hub.terminal_input("remote", "worker", "fake-keypress")
            return create(body)
        with patch.object(self.terminals, "create", side_effect=fenced):
            self.hub.reconnect_agent("remote", "worker", request_key="fence")
        self.assertEqual(self.terminals.writes, [])
        self.hub.terminal_input("remote", "worker", "human-message")
        self.assertEqual(self.terminals.writes[-1][1], "input")

    def test_route_failure_is_safe_and_cached_same_key_never_retries(self):
        self.disconnected()
        self.answer = {"ok": False, "exists": False, "reason": "route_unavailable"}
        result = self.hub.reconnect_agent("remote", "worker", request_key="route")
        self.assertEqual(result["recovery"]["code"], "route_unavailable")
        repeated = self.hub.reconnect_agent("remote", "worker", request_key="route")
        self.assertEqual(repeated["operation_state"], "blocked")
        self.assertEqual(len(self.probes), 1)
        self.assertEqual(self.creates(), [])

    def test_closed_and_close_unknown_never_probe_or_attach(self):
        for mode in ("closed", "closing", "close_unknown"):
            self.hub.store.remember("remote", "worker", "Old title", mode=mode)
            result = self.hub.reconnect_agent("remote", "worker", request_key="closed-" + mode)
            self.assertEqual(result["recovery"]["code"], "closed")
        self.assertEqual(self.probes, [])
        self.assertEqual(self.creates(), [])

    def test_local_exited_is_not_restarted(self):
        self.hub.store.remember("local", "local-worker", "Local", active=True, mode="terminal", terminal_id=TERMINAL, cwd=str(Path(self.temp.name)))
        self.disconnected()
        result = self.hub.reconnect_agent("local", "local-worker", request_key="local")
        self.assertEqual(result["recovery"]["code"], "local_reconnect_unsupported")
        self.assertEqual(self.creates(), [])

    def test_identity_changed_after_attach_never_claims_recovery_or_closes_worker(self):
        self.disconnected()
        with patch.object(self.hub, "_probe_remote_terminal", side_effect=[self.answer,
            {"ok": True, "exists": True, "identity": IDENTITY | {"session": "$12", "pane_pid": 777778}}]):
            result = self.hub.reconnect_agent("remote", "worker", request_key="changed")
        self.assertEqual(result["recovery"]["code"], "original_session_changed")
        self.assertEqual(result["operation_state"], "blocked")
        self.assertEqual(self.terminals.writes, [])

    def test_concurrent_reconnect_attaches_at_most_once(self):
        self.disconnected()
        results = []
        barrier = threading.Barrier(3)
        def run(index):
            barrier.wait()
            results.append(self.hub.reconnect_agent("remote", "worker", request_key="parallel-" + str(index)))
        threads = [threading.Thread(target=run, args=(n,)) for n in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(results), 2)
        self.assertEqual(len(self.creates()), 1)
        self.assertEqual(self.terminals.writes, [])

    def test_cached_presentation_is_shared_without_remote_probes(self):
        one = self.hub.get_agent("remote", "worker")
        listed = next(a for a in self.hub.attached_agents() if a["agent_id"] == one["agent_id"])
        state = next(a for a in self.hub.state()["agents"] if a["agent_id"] == one["agent_id"])
        self.assertEqual(one["management"], listed["management"])
        self.assertEqual(one["management"], state["management"])
        self.assertEqual(one["management"]["status"], "ready")
        self.assertEqual(self.probes, [])
        self.disconnected()
        self.assertEqual(self.hub.get_agent("remote", "worker")["management"]["primary_action"], "reconnect")

    def test_read_only_probe_uses_only_selected_session_and_no_stderr_disclosure(self):
        # Restore the real implementation but fake every SSH subprocess.
        self.hub._probe_remote_terminal = types.MethodType(Hub._probe_remote_terminal, self.hub)
        reply = types.SimpleNamespace(returncode=0, stdout="CEO_HUB_PTY_RESULT=" + json.dumps(self.answer), stderr="")
        with patch("terminal_hub.subprocess.run", return_value=reply) as run:
            result = self.hub._probe_remote_terminal("remote", TERMINAL)
        self.assertEqual(result["identity"], IDENTITY)
        argv = run.call_args.args[0]
        self.assertEqual(argv[-2], "fake-ssh-target")
        self.assertIn("probe --session " + TERMINAL, argv[-1])
        self.assertNotIn("new-session", argv[-1])
        with patch("terminal_hub.subprocess.run", return_value=types.SimpleNamespace(returncode=1, stdout="", stderr="PRIVATE_SECRET")):
            result = self.hub._probe_remote_terminal("remote", TERMINAL)
        self.assertNotIn("PRIVATE_SECRET", json.dumps(result))

    def test_additive_legacy_agents_migration_defaults_pinned_without_rewriting_identity(self):
        path = Path(self.temp.name) / "legacy.sqlite"
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("create table agents(host text,thread_id text,alias text,owned integer,primary key(host,thread_id))")
            db.execute("insert into agents values('remote','old','Old',0)")
        store = Store(path)
        try:
            row = store.agent("remote", "old")
            self.assertEqual(row["pinned"], 0)
            self.assertEqual((row["host"], row["thread_id"], row["alias"]), ("remote", "old", "Old"))
        finally:
            store.close()

    def test_probe_auth_and_host_key_failures_use_safe_categories(self):
        self.hub._probe_remote_terminal = types.MethodType(Hub._probe_remote_terminal, self.hub)
        for text, expected in (("Permission denied (publickey) PRIVATE_DETAIL", "ssh_auth_required"),
                               ("Host key verification failed PRIVATE_DETAIL", "ssh_host_key_required")):
            with patch("terminal_hub.subprocess.run", return_value=types.SimpleNamespace(returncode=1, stdout="", stderr=text)):
                result = self.hub._probe_remote_terminal("remote", TERMINAL)
            self.assertEqual(result["reason"], expected)
            self.assertNotIn("PRIVATE_DETAIL", json.dumps(result))

    def test_ui_http_update_and_reconnect_return_flattened_agent(self):
        server = create_server(self.hub, 0, token="fake-browser-token")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        opener = build_opener(ProxyHandler({}))
        try:
            headers = {"Origin": server.origin, "Cookie": "ceo_session=fake-browser-token", "Content-Type": "application/json"}
            for path, body in (("/api/agents/update", {"host": "remote", "thread_id": "worker", "alias": "HTTP title", "pinned": True}),
                               ("/api/agents/reconnect", {"host": "remote", "thread_id": "worker", "request_key": "http-reconnect"})):
                with opener.open(Request(server.origin + path, data=json.dumps(body).encode(), headers=headers), timeout=5) as reply:
                    result = json.load(reply)
                self.assertEqual(result["agent_id"], "remote::worker")
                self.assertEqual(result["alias"], "HTTP title")
                self.assertTrue(result["pinned"])
                self.assertIn("management", result)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)

    def test_ui_initial_state_returns_recent_tail_without_deleting_history_or_changing_plugin_cursor(self):
        self.hub._stop.set()
        self.hub._watcher.join(3)
        with self.hub.store.lock, self.hub.store.db:
            self.hub.store.db.executemany("insert into events(time,kind,data) values(?,?,?)", (
                (1000 + n, "agent_updated", json.dumps({"host": "remote", "thread_id": "worker", "index": n})) for n in range(450)))
        state = self.hub.state(after=0)
        self.assertEqual(len(state["events"]), 400)
        self.assertEqual(state["events"][0]["data"]["index"], 50)
        self.assertEqual(state["events"][-1]["data"]["index"], 449)
        self.assertEqual(state["cursor"], state["events"][-1]["id"])
        self.assertEqual(self.hub.store.db.execute("select count(*) from events").fetchone()[0], 450)
        original = self.hub.plugin_events(after=0)
        self.assertEqual(original["events"][0]["data"]["index"], 0)
        self.assertEqual(original["events"][-1]["data"]["index"], 399)
        self.assertEqual(self.hub.state(after=440)["events"][0]["id"], 441)

    def service_fixture(self):
        status = {"state": "offline", "code": "bound_owner_offline", "running": False, "can_recover": True,
                  "pid": 12345, "version": "0.3.0", "recovery_id": "c" * 32, "token": "PRIVATE_CAPABILITY", "descriptor_file": "PRIVATE_PATH"}
        self.service_calls = []
        self.terminals.bound_owner_status = lambda: dict(status)
        def recover(**kwargs):
            self.service_calls.append(kwargs)
            self.terminals.records.clear()
            status.update(state="ready", code="bound_owner_recovered", running=True, can_recover=False,
                          previous_pid=12345, pid=67890, owner_recovered=True, recovery_id="a" * 32,
                          previous_recovery_id="c" * 32, started_empty=True)
            return dict(status)
        self.terminals.recover_bound_owner = recover
        self.terminals.request = lambda path, **kwargs: {"sessions": self.terminals.sessions()}
        return status

    def make_unknown(self, key="legacy-unknown"):
        self.disconnected()
        status = self.terminals.bound_owner_status()
        with patch.object(self.terminals, "bound_owner_status", return_value=status | {"state": "ready", "running": True}), \
             patch.object(self.terminals, "create", side_effect=RuntimeError("PRIVATE_FAILURE")):
            result = self.hub.reconnect_agent("remote", "worker", request_key=key)
        self.assertEqual(result["operation_state"], "unknown")
        return result

    def test_service_status_is_read_only_and_never_returns_private_capabilities(self):
        self.service_fixture()
        status = self.hub.terminal_service_status()
        self.assertTrue(status["can_recover"])
        self.assertTrue(status["recovery_available"])
        self.assertEqual(status["state"], "offline")
        self.assertNotIn("PRIVATE_", json.dumps(status))
        self.assertNotIn("descriptor_file", status)
        self.assertEqual(self.service_calls, [])
        self.assertEqual(self.probes, [])
        self.assertEqual(self.creates(), [])
        self.assertEqual(self.terminals.writes, [])

    def test_service_recovery_requires_explicit_boolean_and_only_recovers_owner(self):
        self.service_fixture()
        for confirm in (None, False, 1, "true"):
            with self.assertRaises(ValueError):
                self.hub.recover_terminal_service(confirm=confirm)
        result = self.hub.recover_terminal_service(confirm=True, expected_owner={"pid": 12345, "version": "0.3.0"})
        self.assertTrue(result["running"])
        self.assertTrue(result["owner_recovered"])
        self.assertFalse(result["sessions_restored"])
        self.assertEqual(self.service_calls, [{"confirm": True, "expected_owner": {"pid": 12345, "version": "0.3.0"}}])
        self.assertEqual(self.hub.store.agent("remote", "worker")["terminal_id"], TERMINAL)
        self.assertEqual(self.hub.store.agent("remote", "worker")["active"], 1)
        self.assertEqual(self.probes, [])
        self.assertEqual(self.creates(), [])
        self.assertEqual(self.terminals.writes, [])

    def test_unconfirmed_service_start_keeps_unknown_and_does_not_expose_errors(self):
        self.service_fixture()
        with patch.object(self.terminals, "recover_bound_owner", side_effect=RuntimeError("PRIVATE_CAPABILITY_AND_PATH")):
            result = self.hub.recover_terminal_service(confirm=True)
        self.assertEqual(result["state"], "unknown")
        self.assertFalse(result["running"])
        self.assertFalse(result["owner_recovered"])
        self.assertNotIn("PRIVATE_", json.dumps(result))
        self.assertIsNone(self.hub.store.setting("terminal-owner-recovery-proof"))
        self.assertEqual(self.creates(), [])
        self.assertEqual(self.terminals.writes, [])

    def test_known_pre_attach_owner_failure_is_blocked_and_retryable_same_key(self):
        status = self.service_fixture()
        self.disconnected()
        with patch.object(self.terminals, "create", side_effect=BrokerError("PRIVATE_ERROR", "bound_transport_unavailable")):
            result = self.hub.reconnect_agent("remote", "worker", request_key="known-owner-failure")
        self.assertEqual(result["operation_state"], "blocked")
        self.assertEqual(result["recovery"]["code"], "local_display_unavailable")
        self.assertTrue(result["recovery"]["retryable"])
        self.assertEqual(result["recovery"]["next_action"], "recover_display_service")
        self.assertEqual(result["recovery"]["request_origin"], "ui")
        self.assertNotIn("PRIVATE_", json.dumps(result))
        count = len(self.probes)
        repeated = self.hub.reconnect_agent("remote", "worker", request_key="known-owner-failure")
        self.assertEqual(repeated["operation_state"], "blocked")
        self.assertEqual(len(self.probes), count, "Offline repeated keys must not retry attachment")
        self.hub.recover_terminal_service(confirm=True)
        retried = self.hub.reconnect_agent("remote", "worker", request_key="known-owner-failure")
        self.assertEqual(retried["operation_state"], "done")
        self.assertTrue(retried["connected"])
        self.assertEqual(len(self.creates()), 1)
        self.assertEqual(self.terminals.writes, [])
        self.assertEqual(status["pid"], 67890)

    def test_post_attach_owner_error_stays_unknown_and_same_key_never_replays(self):
        self.service_fixture()
        self.disconnected()
        with patch.object(self.terminals, "snapshot", side_effect=BrokerError("PRIVATE_ERROR", "bound_transport_unavailable")):
            result = self.hub.reconnect_agent("remote", "worker", request_key="post-attach")
        self.assertEqual(result["operation_state"], "unknown")
        self.assertEqual(result["recovery"]["code"], "reconnect_unknown")
        self.hub.reconnect_agent("remote", "worker", request_key="post-attach")
        self.assertEqual(len(self.creates()), 1)
        self.assertEqual(self.terminals.writes, [])

    def test_started_operation_is_not_forged_completed_by_same_key_request(self):
        key = "in-flight"
        fingerprint = self.hub._reconnect_fingerprint("remote", "worker", TERMINAL, "ui", None)
        self.hub.store.begin_operation("agent-reconnect:" + key, fingerprint)
        result = self.hub.reconnect_agent("remote", "worker", request_key=key)
        self.assertEqual(result["operation_state"], "unknown")
        row = self.hub.store.db.execute("select state,result from operations where request_key=?", ("agent-reconnect:" + key,)).fetchone()
        self.assertEqual(row["state"], "started")
        self.assertIsNone(row["result"])
        self.assertEqual(self.creates(), [])

    def test_legacy_unknown_reconciliation_is_explicit_absence_proven_and_preserves_key(self):
        self.service_fixture()
        self.make_unknown()
        self.hub.recover_terminal_service(confirm=True)
        self.assertEqual(self.hub.get_agent("remote", "worker")["recovery"]["code"], "reconnect_unknown", "Service recovery must not reset old operation results")
        self.assertTrue(self.hub.get_agent("remote", "worker")["recovery"]["reconcile_available"])
        with self.assertRaises(ValueError):
            self.hub.reconcile_reconnect("remote", "worker", "legacy-unknown")
        before = len(self.probes)
        result = self.hub.reconcile_reconnect("remote", "worker", "legacy-unknown", confirm=True)
        self.assertEqual(result["operation_state"], "blocked")
        self.assertTrue(result["recovery"]["retryable"])
        self.assertEqual(len(self.probes), before)
        self.assertEqual(self.creates(), [])
        self.assertEqual(self.terminals.writes, [])
        self.assertEqual(self.hub.store.db.execute("select count(*) from operations where request_key='agent-reconnect:legacy-unknown'").fetchone()[0], 1)
        repeated = self.hub.reconcile_reconnect("remote", "worker", "legacy-unknown", confirm=True)
        self.assertEqual(repeated["recovery"], result["recovery"])
        retried = self.hub.reconnect_agent("remote", "worker", request_key="legacy-unknown")
        self.assertEqual(retried["operation_state"], "done")
        self.assertEqual(len(self.creates()), 1)
        self.assertEqual(self.terminals.writes, [])

    def test_unknown_without_confirmed_owner_recovery_never_unlocks(self):
        status = self.service_fixture()
        self.make_unknown()
        status.update(state="ready", running=True)
        self.terminals.records.clear()
        with self.assertRaises(ValueError):
            self.hub.reconcile_reconnect("remote", "worker", "legacy-unknown", confirm=True)
        self.assertEqual(self.creates(), [])
        self.assertEqual(self.hub.reconnect_agent("remote", "worker", request_key="legacy-unknown")["operation_state"], "unknown")

    def test_reconcile_requires_exact_target_old_done_operation_owner_and_absence(self):
        status = self.service_fixture()
        self.make_unknown()
        self.hub.recover_terminal_service(confirm=True)
        self.hub.store.remember("remote", "other", "Other", active=True, mode="terminal", terminal_id=TERMINAL, cwd="/work/project")
        with self.assertRaises(ValueError):
            self.hub.reconcile_reconnect("remote", "other", "legacy-unknown", confirm=True)
        self.terminals.add(TERMINAL, "remote")
        with self.assertRaises(ValueError):
            self.hub.reconcile_reconnect("remote", "worker", "legacy-unknown", confirm=True)
        self.terminals.records.clear()
        status["pid"] = 78901
        with self.assertRaises(ValueError):
            self.hub.reconcile_reconnect("remote", "worker", "legacy-unknown", confirm=True)
        status["pid"] = 67890
        status["recovery_id"] = "b" * 32
        with self.assertRaises(ValueError):
            self.hub.reconcile_reconnect("remote", "worker", "legacy-unknown", confirm=True)
        self.assertEqual(self.creates(), [])
        self.assertEqual(self.terminals.writes, [])

    def test_ui_reconciliation_cannot_rearm_a_controller_operation(self):
        self.service_fixture()
        self.disconnected()
        self.hub.controllers.shutdown()
        self.hub.controllers = Controllers(self.hub)
        self.hub.controllers.grants["a"] = {"remote::worker"}
        with patch.object(self.terminals, "create", side_effect=RuntimeError("Unconfirmed attachment")):
            result = self.hub.plugin_call("reconnect_agent", {"agent_id": "remote::worker", "request_key": "controller-unknown"}, controller_id="a")
        self.assertEqual(result["operation_state"], "unknown")
        self.assertEqual(result["recovery"]["request_origin"], "plugin")
        self.hub.recover_terminal_service(confirm=True)
        with self.assertRaises(ValueError):
            self.hub.reconcile_reconnect("remote", "worker", "controller-unknown", confirm=True)
        self.assertEqual(self.creates(), [])
        self.assertEqual(self.terminals.writes, [])

    def test_legacy_unknown_without_owner_generation_remains_unknown_after_recovery(self):
        status = self.service_fixture()
        status["recovery_id"] = None
        self.make_unknown()
        row = self.hub.store.db.execute("select result from operations where request_key='agent-reconnect:legacy-unknown'").fetchone()
        self.assertNotIn("owner_stamp", json.loads(row[0]))
        self.hub.recover_terminal_service(confirm=True)
        self.assertFalse(self.hub.get_agent("remote", "worker")["recovery"].get("reconcile_available", False))
        with self.assertRaises(ValueError):
            self.hub.reconcile_reconnect("remote", "worker", "legacy-unknown", confirm=True)
        self.assertEqual(json.loads(self.hub.store.db.execute("select result from operations where request_key='agent-reconnect:legacy-unknown'").fetchone()[0])["state"], "unknown")
        self.assertEqual(self.creates(), [])

    def test_unknown_stamp_must_match_real_previous_owner_generation(self):
        self.service_fixture()
        self.make_unknown()
        recover = self.terminals.recover_bound_owner
        self.terminals.recover_bound_owner = lambda **kwargs: recover(**kwargs) | {"previous_recovery_id": "d" * 32}
        self.hub.recover_terminal_service(confirm=True)
        with self.assertRaises(ValueError):
            self.hub.reconcile_reconnect("remote", "worker", "legacy-unknown", confirm=True)
        self.assertEqual(self.creates(), [])
        self.assertEqual(self.terminals.writes, [])

    def test_receipt_only_marks_current_attempt_not_all_historical_unknown_keys(self):
        self.service_fixture()
        self.make_unknown("historical-unknown")
        self.make_unknown("current-unknown")
        self.hub.recover_terminal_service(confirm=True)
        proof = self.hub.store.setting("terminal-owner-recovery-proof")
        self.assertEqual(set(proof["operations"]), {"agent-reconnect:current-unknown"})
        with self.assertRaises(ValueError):
            self.hub.reconcile_reconnect("remote", "worker", "historical-unknown", confirm=True)
        result = self.hub.reconcile_reconnect("remote", "worker", "current-unknown", confirm=True)
        self.assertEqual(result["operation_state"], "blocked")
        self.assertEqual(self.creates(), [])
        self.assertEqual(self.terminals.writes, [])

    def test_reconcile_unavailable_registry_is_not_assumed_empty(self):
        self.service_fixture()
        self.make_unknown()
        self.hub.recover_terminal_service(confirm=True)
        self.terminals.request = lambda *args, **kwargs: (_ for _ in ()).throw(BrokerError("PRIVATE_DETAIL", "unavailable"))
        with self.assertRaises(ValueError) as error:
            self.hub.reconcile_reconnect("remote", "worker", "legacy-unknown", confirm=True)
        self.assertNotIn("PRIVATE_", str(error.exception))
        row = self.hub.store.db.execute("select result from operations where request_key='agent-reconnect:legacy-unknown'").fetchone()
        self.assertEqual(json.loads(row[0])["state"], "unknown")
        self.assertEqual(self.creates(), [])

    def test_completed_unknown_after_recovery_cannot_use_prior_owner_receipt(self):
        self.service_fixture()
        self.hub.recover_terminal_service(confirm=True)
        self.terminals.add(TERMINAL, "remote")
        self.make_unknown("fresh-unknown")
        self.terminals.records.clear()
        with self.assertRaises(ValueError):
            self.hub.reconcile_reconnect("remote", "worker", "fresh-unknown", confirm=True)
        self.assertEqual(self.creates(), [])

    def test_reconciled_same_key_fresh_unknown_cannot_reuse_old_recovery_proof(self):
        self.service_fixture()
        self.make_unknown()
        self.hub.recover_terminal_service(confirm=True)
        self.hub.reconcile_reconnect("remote", "worker", "legacy-unknown", confirm=True)
        with patch.object(self.terminals, "create", side_effect=RuntimeError("New attachment became uncertain")):
            result = self.hub.reconnect_agent("remote", "worker", request_key="legacy-unknown")
        self.assertEqual(result["operation_state"], "unknown")
        with self.assertRaises(ValueError):
            self.hub.reconcile_reconnect("remote", "worker", "legacy-unknown", confirm=True)
        self.assertEqual(self.creates(), [])
        self.assertEqual(self.terminals.writes, [])

    def test_service_http_endpoint_is_browser_only_origin_and_confirm_protected(self):
        self.service_fixture()
        server = create_server(self.hub, 0, token="fake-browser-token")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        opener = build_opener(ProxyHandler({}))
        try:
            url = server.origin + "/api/terminal-service/recover"
            valid = {"Origin": server.origin, "Cookie": "ceo_session=fake-browser-token", "Content-Type": "application/json"}
            for headers in ({"Content-Type": "application/json"}, {**valid, "Origin": "http://evil.invalid"},
                            {"Authorization": "Bearer fake-browser-token", "Content-Type": "application/json"}):
                with self.assertRaises(HTTPError) as error:
                    opener.open(Request(url, data=b'{"confirm":true}', headers=headers), timeout=5)
                self.assertEqual(error.exception.code, 403)
                with self.assertRaises(HTTPError) as error:
                    opener.open(Request(server.origin + "/api/agents/reconcile-reconnect", data=b'{"host":"remote","thread_id":"worker","request_key":"unknown","confirm":true}', headers=headers), timeout=5)
                self.assertEqual(error.exception.code, 403)
            with self.assertRaises(HTTPError) as error:
                opener.open(Request(url, data=b'{}', headers=valid), timeout=5)
            self.assertEqual(error.exception.code, 400)
            with self.assertRaises(HTTPError) as error:
                opener.open(Request(server.origin + "/api/agents/reconcile-reconnect", data=b'{"host":"remote","thread_id":"worker","request_key":"unknown"}', headers=valid), timeout=5)
            self.assertEqual(error.exception.code, 400)
            self.assertEqual(self.service_calls, [])
            with opener.open(Request(server.origin + "/api/terminal-service/status", headers={"Cookie": valid["Cookie"]}), timeout=5) as reply:
                self.assertTrue(json.load(reply)["can_recover"])
            with self.assertRaises(HTTPError):
                opener.open(Request(server.origin + "/api/terminal-service/status"), timeout=5)
            with opener.open(Request(url, data=b'{"confirm":true}', headers=valid), timeout=5) as reply:
                self.assertTrue(json.load(reply)["running"])
            with self.assertRaises(HTTPError) as error:
                opener.open(Request(server.origin + "/api/plugin", data=b'{"tool":"recover_terminal_service","arguments":{"confirm":true}}',
                    headers={"Authorization": "Bearer fake-browser-token", "Content-Type": "application/json"}), timeout=5)
            self.assertEqual(error.exception.code, 400)
            self.assertEqual(len(self.service_calls), 1)
            self.assertEqual(self.probes, [])
            self.assertEqual(self.creates(), [])
            self.assertEqual(self.terminals.writes, [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)


if __name__ == "__main__":
    unittest.main()
