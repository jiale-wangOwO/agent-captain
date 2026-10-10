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
from store import Store
from tests.test_terminal_hub import FakeTerminals
from tests.test_controller_scope import Controllers
from app import create_server
from urllib.request import Request, ProxyHandler, build_opener

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

    def test_ui_metadata_does_not_activate_inactive_worker(self):
        self.hub.store.set_active("remote", "worker", False)
        result = self.hub.update_agent("remote", "worker", pinned=True)
        self.assertFalse(result["active"])
        self.assertTrue(result["pinned"])

    def test_plugin_metadata_and_reconnect_enforce_activation_and_scope(self):
        self.hub.controllers.shutdown()
        self.hub.controllers = Controllers(self.hub)
        self.hub.controllers.grants["a"] = {"remote::worker"}
        self.hub.controllers.grants["b"] = set()
        updated = self.hub.plugin_call("update_agent", {"agent_id": "remote::worker", "alias": "Scoped", "request_key": "a-update"}, controller_id="a")
        self.assertEqual(updated["alias"], "Scoped")
        for tool, kwargs in (("update_agent", {"alias": "No"}), ("reconnect_agent", {})):
            with self.assertRaises(PermissionError):
                self.hub.plugin_call(tool, {"agent_id": "remote::worker", "request_key": "b-request", **kwargs}, controller_id="b")
        self.hub.store.set_active("remote", "worker", False)
        with self.assertRaises(PermissionError):
            self.hub.plugin_call("reconnect_agent", {"agent_id": "remote::worker", "request_key": "inactive"}, controller_id="a")
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


if __name__ == "__main__":
    unittest.main()
