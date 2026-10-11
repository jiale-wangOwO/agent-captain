"""Shared-terminal management contracts; no PTY, SSH, model, or App Server."""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from terminal_client import BrokerError
from terminal_hub import Hub


class FakeTerminals:
    """A broker-shaped shared screen and input sink with explicit failure modes."""

    def __init__(self):
        self.lock = threading.RLock()
        self.records = {}
        self.calls = []
        self.writes = []
        self.closed = False
        self.write_error = None

    def add(self, terminal_id="ceohub-0123456789abcdef01234567", host="local", state="agent", screen="Codex\n› "):
        row = {"id": terminal_id, "host": host, "transport": "local" if host == "local" else "ssh",
               "target": "" if host == "local" else "fake316", "cwd": "/workspace/project", "title": "Work",
               "connected": True, "pid": 4242, "cols": 100, "rows": 30, "state": state,
               "command": "codex" if state == "agent" else "bash", "current_seq": 3,
               "agent_ready": state == "agent", "blocked_reason": None, "screen": screen,
               "ansi": "\x1b[32m" + screen + "\x1b[0m"}
        with self.lock:
            self.records[terminal_id] = row
        return dict(row)

    def sessions(self):
        with self.lock:
            self.calls.append(("sessions",))
            return [dict(row) for row in self.records.values()]

    def create(self, body):
        with self.lock:
            self.calls.append(("create", dict(body)))
            terminal_id = body.get("session_id") or f"ceohub-{len(self.records) + 1:024x}"
            if terminal_id in self.records:
                self.records[terminal_id]["connected"] = True
                return dict(self.records[terminal_id])
            row = self.add(terminal_id, body.get("host", "local"), state="shell", screen="$ ")
            row.update({key: body[key] for key in ("cwd", "title", "cols", "rows", "transport", "target") if key in body})
            self.records[terminal_id] = row
            return dict(row)

    def snapshot(self, terminal_id, lines=60):
        with self.lock:
            self.calls.append(("snapshot", terminal_id, lines))
            return dict(self.records[terminal_id])

    def output(self, terminal_id, after=0):
        with self.lock:
            self.calls.append(("output", terminal_id, after))
            row = self.records[terminal_id]
            return {"data": row["screen"] if after < row["current_seq"] else "", "seq": row["current_seq"],
                    "reset": after == 0, "ansi": row["ansi"], "connected": row["connected"],
                    "cols": row["cols"], "rows": row["rows"], "state": row["state"]}

    def write(self, terminal_id, action, body):
        with self.lock:
            self.calls.append(("write", terminal_id, action, dict(body)))
            row = self.records[terminal_id]
            if action == "prompt" and not row["agent_ready"]:
                raise BrokerError("User must start Codex first", "waiting_for_agent", 409)
            self.writes.append((terminal_id, action, dict(body)))
            if self.write_error is not None:
                raise self.write_error
            if action == "resize":
                row.update({"cols": body["cols"], "rows": body["rows"]})
            elif action == "close":
                row.update({"connected": False, "state": "exited"})
                return {"id": terminal_id, "closed": True}
            return {"id": terminal_id, "state": "submitted", "submitted": True, "current_seq": row["current_seq"]}

    def close(self):
        self.closed = True


class TerminalHubTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.terminals = FakeTerminals()
        self.hub = Hub(self.temp.name, terminals=self.terminals)
        self.hub.hosts = {"local": {"transport": "local", "label": "Local"},
                          "316": {"transport": "ssh", "target": "fake316", "label": "316"}}
        self.hub.connections["316"] = {"connected": True}

    def tearDown(self):
        self.hub.close()
        self.temp.cleanup()

    def test_bound_owner_unavailable_reports_known_blocked_creation(self):
        with patch.object(self.hub, 'history', side_effect=AssertionError('No history/SSH for this fake terminal')), \
             patch.object(self.terminals, 'create', side_effect=BrokerError('Original transport unavailable', 'bound_transport_unavailable')):
            result = self.hub.create_terminal('316', '/workspace/project', 'Pending worker', thread_id='ceohub-112233445566778899aabbccddeeff00')
        self.assertFalse(result['created'])
        self.assertEqual(result['delivery']['state'], 'blocked')
        self.assertEqual(result['delivery']['code'], 'bound_transport_unavailable')
        self.assertEqual(self.terminals.records, {})

    def register(self, thread_id="work-1", active=True, terminal_id="ceohub-0123456789abcdef01234567", state="agent", host="local"):
        self.terminals.add(terminal_id, host, state)
        self.hub.store.remember(host, thread_id, "Work", active=active, access="control", mode="terminal", terminal_id=terminal_id, cwd="/workspace/project")
        return f"{host}::{thread_id}"

    def send(self, text="请检查这个 bug。", key="task-1", agent="local::work-1"):
        return self.hub.plugin_call("send_agent_message", {"agent_id": agent, "text": text, "request_key": key})

    def restart(self):
        hosts = self.hub.hosts
        self.hub.close()
        self.hub = Hub(self.temp.name, terminals=self.terminals)
        self.hub.hosts = hosts

    def test_geometry_fence_is_durable_blocked_and_same_key_retries_once_after_repair(self):
        self.register()
        for tool, field, value in (("send_agent_message", "text", "请继续检查这个问题。"),
                                   ("send_terminal_input", "data", "\x03")):
            with self.subTest(tool=tool):
                arguments = {"agent_id": "local::work-1", field: value, "request_key": "geometry-" + tool}
                rejection = BrokerError("Display geometry is not synchronized", "geometry_unknown", 409)
                writes_before = len(self.terminals.writes)
                with patch.object(self.terminals, "write", side_effect=rejection) as rejected:
                    first = self.hub.plugin_call(tool, arguments)
                    self.assertEqual(first["state"], "blocked")
                    self.assertEqual(first["delivery"], "blocked")
                    self.assertEqual(len(self.terminals.writes), writes_before)
                    self.restart()
                    replay = self.hub.plugin_call(tool, arguments)
                    self.assertEqual(replay["id"], first["id"])
                    self.assertEqual(replay["state"], "blocked")
                    self.assertEqual(rejected.call_count, 2)
                # A verified display repair removes the broker's fence; no new
                # logical request or idempotency key is needed to submit once.
                repaired = self.hub.plugin_call(tool, arguments)
                self.assertEqual(repaired["id"], first["id"])
                self.assertEqual(repaired["state"], "submitted")
                self.assertIsNone(repaired["error"])
                self.assertEqual(len(self.terminals.writes), writes_before + 1)
                self.hub.plugin_call(tool, arguments)
                self.assertEqual(len(self.terminals.writes), writes_before + 1)

    def test_legacy_activation_noop_preserves_geometry_fence_and_same_key_retry(self):
        self.register()
        with patch.object(self.terminals, "write", side_effect=BrokerError("Display needs resize", "geometry_unknown", 409)):
            delivery = self.send(key="geometry-revoked")
        self.assertEqual(delivery["state"], "blocked")
        compatibility = self.hub.activate_agent("local", "work-1", False)
        self.assertTrue(compatibility["active"])
        self.assertTrue(compatibility["activation_compatibility"]["ignored"])
        self.assertEqual(self.hub.store.delivery(delivery["id"])["state"], "blocked")
        self.hub.activate_agent("local", "work-1", True)
        self.assertEqual(self.send(key="geometry-revoked")["state"], "submitted")
        self.assertEqual(len(self.terminals.writes), 1)

    def test_startup_and_registry_never_launch_or_scan_agents(self):
        self.hub.close()
        with patch("terminal_hub.subprocess.run", side_effect=AssertionError("no SSH at startup")), \
             patch("terminal_hub.catalog.main", side_effect=AssertionError("no history scan")):
            self.hub = Hub(self.temp.name, terminals=self.terminals)
            self.hub.plugin_call("list_active_agents", {})
            self.hub.attached_agents()
        self.assertFalse(any(call[0] in ("create", "write") for call in self.terminals.calls))
        self.assertFalse(hasattr(self.hub, "servers"))
        self.assertFalse(hasattr(self.hub, "live_servers"))

    def test_legacy_registry_migration_stays_history_until_explicit_terminal_open(self):
        self.hub.store.remember("316", "legacy-id", "Old", active=True, access="observe")
        before = self.hub.get_agent("316", "legacy-id")
        self.assertEqual(before["mode"], "history")
        self.assertIsNone(before["terminal_id"])
        self.assertEqual(self.terminals.writes, [])
        opened = self.hub.create_terminal("316", "/workspace/project", "Old", thread_id="legacy-id")
        row = self.hub.store.agent("316", "legacy-id")
        self.assertEqual(row["mode"], "terminal")
        self.assertEqual(row["cwd"], "/workspace/project")
        self.assertEqual(row["terminal_id"], opened["terminal_id"])
        self.assertEqual(opened["thread_id"], "legacy-id")
        self.assertEqual(self.terminals.writes, [], "opening history must not type resume or switch model")

    def test_create_shell_is_idempotent_and_changed_fingerprint_is_rejected(self):
        args = {"host": "local", "cwd": "C:/workspace/project", "title": "Work", "request_key": "shell-1"}
        first = self.hub.create_terminal(**args)
        replay = self.hub.create_terminal(**args)
        self.assertEqual(first, replay)
        self.assertEqual(len(self.terminals.records), 1, "replay must never create a second terminal")
        self.assertEqual(self.terminals.writes, [])
        with self.assertRaises(ValueError):
            self.hub.create_terminal(**{**args, "title": "Different"})

    def test_create_explicit_command_is_raw_enter_and_part_of_fingerprint(self):
        args = {"host": "local", "cwd": "C:/workspace/project", "title": "Work", "command": "codex-switch", "request_key": "create-command", "origin": "plugin"}
        first = self.hub.create_agent(**args)
        replay = self.hub.create_agent(**args)
        self.assertEqual(first, replay)
        self.assertEqual(len(self.terminals.writes), 1)
        self.assertEqual(self.terminals.writes[0][1], "input")
        self.assertEqual(self.terminals.writes[0][2]["data"], "codex-switch\r")
        with self.assertRaises(ValueError):
            self.hub.create_agent(**{**args, "command": "codex"})

    def test_stored_inactive_worker_is_managed_and_direct_ui_input_stays_unpersisted(self):
        agent = self.register(active=False)
        self.assertEqual([row["agent_id"] for row in self.hub.plugin_call("list_active_agents", {})["agents"]], [agent])
        self.assertTrue(self.hub.plugin_call("get_agent", {"agent_id": agent})["active"])
        self.assertEqual(self.send(agent=agent)["state"], "submitted")
        deliveries = self.hub.store.outbox()
        self.hub.terminal_input("local", "work-1", "hello\r")
        self.assertEqual(self.terminals.writes[-1][2]["data"], "hello\r")
        self.assertEqual(self.hub.store.outbox(), deliveries, "human raw input must not persist credentials")

    def test_ordinary_task_is_sent_to_same_terminal_without_ceo_injection(self):
        self.register()
        delivery = self.send()
        self.assertEqual(delivery["state"], "submitted")
        terminal_id, action, body = self.terminals.writes[0]
        self.assertEqual(terminal_id, self.hub.store.agent("local", "work-1")["terminal_id"])
        self.assertEqual(action, "prompt")
        self.assertEqual(body["text"], "请检查这个 bug。")
        self.assertNotIn("CEO", body["text"])

    def test_task_waits_for_agent_instead_of_executing_in_shell(self):
        self.register(state="shell")
        delivery = self.send(text="请修复问题；不要运行这个文本作为 shell 命令。")
        self.assertEqual(delivery["state"], "waiting_for_agent")
        self.assertEqual(self.terminals.writes, [])
        self.assertEqual(self.hub.store.delivery(delivery["id"])["state"], "waiting_for_agent")

    def test_prompt_rejects_terminal_control_injection(self):
        self.register()
        for text in ("task\x1b[200~", "task\x03", "task\x00", "task\x7f"):
            with self.subTest(text=repr(text)), self.assertRaises(ValueError):
                self.send(text=text, key=repr(text))
        self.assertEqual(self.terminals.writes, [])

    def test_replay_and_key_collision_do_not_write_twice(self):
        self.register()
        first, replay = self.send(), self.send()
        self.assertEqual(first["id"], replay["id"])
        self.assertEqual(len(self.terminals.writes), 1)
        with self.assertRaises(ValueError):
            self.send(text="不同任务")
        self.assertEqual(len(self.terminals.writes), 1)

    def test_concurrent_replay_writes_once(self):
        self.register()
        gate = threading.Barrier(5)
        replies, errors = [], []
        def worker():
            try:
                gate.wait(timeout=3)
                replies.append(self.send())
            except Exception as error:
                errors.append(error)
        threads = [threading.Thread(target=worker, daemon=True) for _ in range(4)]
        for thread in threads:
            thread.start()
        gate.wait(timeout=3)
        for thread in threads:
            thread.join(4)
        self.assertTrue(all(not thread.is_alive() for thread in threads), "concurrent management deadlocked")
        self.assertEqual(errors, [])
        self.assertEqual(len({reply["id"] for reply in replies}), 1)
        self.assertEqual(len(self.terminals.writes), 1)

    def test_submitted_replay_after_restart_never_reopens_or_resends(self):
        self.register()
        first = self.send()
        self.restart()
        replay = self.send()
        self.assertEqual(replay["id"], first["id"])
        self.assertEqual(replay["state"], "submitted")
        self.assertEqual(len(self.terminals.writes), 1)
        self.assertFalse(any(call[0] == "create" for call in self.terminals.calls))

    def test_unknown_delivery_survives_restart_without_retry(self):
        self.register()
        self.terminals.write_error = TimeoutError("response lost after PTY write")
        try:
            self.send()
        except TimeoutError:
            pass
        row = self.hub.store.outbox()[0]
        self.assertEqual(row["state"], "unknown")
        self.restart()
        replay = self.send()
        self.assertEqual(replay["id"], row["id"])
        self.assertEqual(replay["state"], "unknown")
        self.assertEqual(len(self.terminals.writes), 1)

    def test_crash_during_submission_never_resends_on_restart(self):
        self.register()
        request_id = self.hub.store.enqueue("local", "work-1", "请检查这个 bug。", origin="plugin", request_key="task-1", kind="prompt")
        self.hub.store.update_outbox(request_id, "submitting")
        self.restart()
        replay = self.send()
        self.assertEqual(replay["id"], request_id)
        self.assertIn(replay["state"], ("submitting", "unknown"))
        self.assertEqual(self.terminals.writes, [])

    def test_raw_ui_input_is_exact_and_not_persisted_in_delivery_or_events(self):
        self.register(active=False)
        secret = "temporary-password-not-for-storage\x1b[A\r"
        self.hub.terminal_input("local", "work-1", secret, request_key="ui-secret")
        self.assertEqual(self.terminals.writes[-1][1], "input")
        self.assertEqual(self.terminals.writes[-1][2]["data"], secret)
        self.assertNotIn(secret, json.dumps(self.hub.store.outbox()))
        self.assertNotIn(secret, json.dumps(self.hub.store.events()))

    def test_raw_plugin_input_deduplicates_exact_keys_and_preserves_controls(self):
        self.register()
        args = {"agent_id": "local::work-1", "data": "\x1b[A\r", "request_key": "key-1"}
        first = self.hub.plugin_call("send_terminal_input", args)
        replay = self.hub.plugin_call("send_terminal_input", args)
        self.assertEqual(first["id"], replay["id"])
        self.assertEqual(len(self.terminals.writes), 1)
        self.assertEqual(self.terminals.writes[0][2]["data"], "\x1b[A\r")
        with self.assertRaises(ValueError):
            self.hub.plugin_call("send_terminal_input", {**args, "data": "\x1b[B\r"})
        with self.assertRaises(ValueError):
            self.hub.plugin_call("send_terminal_input", {"agent_id": "local::work-1", "data": "\r"})

    def test_legacy_activation_noop_does_not_revoke_pending_rows_even_beyond_ui_limit(self):
        self.register()
        ids = []
        for index in range(140):
            request_id = self.hub.store.enqueue("local", "work-1", f"task {index}", origin="plugin", request_key=f"pending-{index}")
            self.hub.store.update_outbox(request_id, "waiting_for_agent" if index % 2 else "queued")
            ids.append(request_id)
        human = self.hub.store.enqueue("local", "work-1", "human task", origin="ui", request_key="human-task")
        submitted = self.hub.store.enqueue("local", "work-1", "already sent", origin="plugin", request_key="already-sent")
        self.hub.store.update_outbox(submitted, "submitted")
        self.hub.activate_agent("local", "work-1", False)
        self.assertTrue(all(self.hub.store.delivery(request_id)["state"] in ("waiting_for_agent", "queued") for request_id in ids))
        self.assertEqual(self.hub.store.delivery(human)["state"], "queued")
        self.assertEqual(self.hub.store.delivery(submitted)["state"], "submitted")

    def test_plugin_and_ui_read_same_terminal_screen_not_a_new_agent(self):
        agent = self.register()
        ui = self.hub.read_agent("local", "work-1")
        plugin = self.hub.plugin_call("read_agent", {"agent_id": agent})
        self.assertEqual(ui["screen"], plugin["screen"])
        self.assertEqual(ui["pid"], plugin["pid"])
        self.assertEqual(ui["ansi"], plugin["ansi"])
        self.assertEqual(ui["current_seq"], plugin["current_seq"])
        self.assertFalse(any(call[0] == "create" for call in self.terminals.calls))

    def test_output_and_resize_use_registered_terminal_identity(self):
        self.register()
        output = self.hub.terminal_output("local", "work-1", after=2)
        self.hub.terminal_resize("local", "work-1", 132, 42)
        terminal_id = self.hub.store.agent("local", "work-1")["terminal_id"]
        self.assertEqual(output["seq"], 3)
        self.assertIn(("output", terminal_id, 2), self.terminals.calls)
        self.assertEqual(self.terminals.writes[-1], (terminal_id, "resize", {"cols": 132, "rows": 42}))

    def test_metadata_and_output_never_manufacture_task_completion(self):
        self.register()
        self.hub.attached_agents()
        self.hub.read_agent("local", "work-1")
        self.hub.terminal_output("local", "work-1", after=3)
        self.terminals.records[self.hub.store.agent("local", "work-1")["terminal_id"]]["state"] = "shell"
        self.hub.attached_agents()
        events = self.hub.store.events()
        self.assertFalse(any(event["kind"] in ("worker_completed", "task_completed", "agent_completed") for event in events))

    def test_hub_close_does_not_close_broker_or_running_terminal(self):
        self.register()
        self.hub.close()
        self.assertFalse(self.terminals.closed)
        self.assertFalse(any(call[0] == "write" and call[2] == "close" for call in self.terminals.calls))

    def test_blank_new_directory_uses_actual_host_home_but_reconnect_uses_recorded_cwd(self):
        with patch.object(self.hub.directories, "home", return_value="/home/selected-user") as home:
            created = self.hub.create_terminal("316", "", "Home shell")
            self.assertEqual(created["cwd"], "/home/selected-user")
            home.assert_called_once_with(self.hub.hosts["316"])
        with patch.object(self.hub.directories, "home", side_effect=AssertionError("no SSH home probe on reconnect")):
            restored = self.hub.connect_terminal("316", created["thread_id"])
        self.assertEqual(restored["cwd"], "/home/selected-user")

    def test_explicit_close_requires_confirmation_and_targets_only_registered_terminal(self):
        first = self.register()
        keeper_id = "ceohub-1123456789abcdef01234567"
        self.register(thread_id="keeper", terminal_id=keeper_id)
        for confirm in (False, None, "true", 1):
            with self.subTest(confirm=confirm), self.assertRaises(ValueError):
                self.hub.terminal_close("local", "work-1", confirm)
        self.assertEqual(self.terminals.writes, [])
        result = self.hub.terminal_close("local", "work-1", True)
        self.assertTrue(result["closed"])
        ended = self.hub.get_agent("local", "work-1")
        self.assertEqual((ended["state"], ended["status"], ended["mode"]), ("closed", "closed", "closed"))
        self.assertFalse(ended["active"])
        self.assertFalse(ended["connected"])
        self.assertEqual(self.terminals.writes, [(ended["terminal_id"], "close", {})])
        self.assertTrue(self.hub.get_agent("local", "keeper")["connected"])
        self.assertTrue(self.hub.get_agent("local", "keeper")["active"])
        self.assertTrue(self.hub.read_agent("local", "work-1")["screen"])
        self.assertEqual(self.hub.read_agent("local", "work-1")["state"], "closed")
        self.assertEqual(self.hub.terminal_output("local", "work-1")["state"], "closed")
        self.assertEqual(self.hub.terminal_close("local", "work-1", True), result)
        self.assertEqual(len(self.terminals.writes), 1)
        self.assertTrue(any(event["kind"] == "terminal_closed" for event in self.hub.store.events()))

    def test_closed_terminal_persists_after_hub_restart_and_refuses_stale_ui_inputs(self):
        self.register()
        result = self.hub.terminal_close("local", "work-1", True)
        self.restart()
        self.assertEqual(self.hub.get_agent("local", "work-1")["state"], "closed")
        for action in (lambda: self.hub.terminal_input("local", "work-1", "stale-key"),
                       lambda: self.hub.terminal_resize("local", "work-1", 80, 24),
                       lambda: self.hub.connect_terminal("local", "work-1"),
                       lambda: self.hub.activate_agent("local", "work-1", True)):
            with self.assertRaisesRegex(ValueError, "新建终端"):
                action()
        self.assertEqual(self.hub.terminal_close("local", "work-1", True), result)
        self.assertEqual(len(self.terminals.writes), 1)

    def test_uncertain_close_is_durable_and_never_replays_against_replacement(self):
        self.register()
        self.terminals.write_error = TimeoutError("close reply lost")
        result = self.hub.terminal_close("local", "work-1", True)
        self.assertFalse(result["closed"])
        self.assertEqual(result["close_state"], "unknown")
        self.assertFalse(any(event["kind"] == "terminal_closed" for event in self.hub.store.events()))
        self.assertFalse(self.hub.get_agent("local", "work-1")["active"])
        self.restart()
        self.terminals.write_error = None
        self.assertEqual(self.hub.terminal_close("local", "work-1", True), result)
        self.assertEqual(len(self.terminals.writes), 1)
        with self.assertRaisesRegex(ValueError, "新建终端"):
            self.hub.connect_terminal("local", "work-1")

    def test_closed_record_remains_readable_when_broker_cache_is_missing_after_restart(self):
        self.register()
        self.hub.terminal_close("local", "work-1", True)
        self.restart()
        self.terminals.records.clear()
        before_writes = len(self.terminals.writes)
        status = self.hub.get_agent("local", "work-1")
        self.assertEqual(status["state"], "closed")
        view = self.hub.read_agent("local", "work-1")
        self.assertEqual(view["state"], "closed")
        self.assertTrue(view["output_unavailable"])
        self.assertFalse(view["history_complete"])
        output = self.hub.terminal_output("local", "work-1", after=17)
        self.assertEqual(output["state"], "closed")
        self.assertEqual(output["seq"], 17)
        self.assertFalse(output["reset"], "Do not blank an existing browser's retained screen")
        self.assertEqual(len(self.terminals.writes), before_writes)

    def test_missing_broker_cannot_report_close_success_or_emit_closed_event(self):
        self.register()
        self.terminals.write_error = BrokerError("Terminal not found after broker restart", "terminal_not_found", 404)
        result = self.hub.terminal_close("local", "work-1", True)
        self.assertFalse(result["closed"])
        self.assertEqual(result["close_state"], "unknown")
        self.assertEqual(self.hub.get_agent("local", "work-1")["state"], "close_unknown")
        self.assertFalse(any(event["kind"] == "terminal_closed" for event in self.hub.store.events()))
        self.assertEqual(self.hub.terminal_close("local", "work-1", True), result)
        self.assertEqual(len(self.terminals.writes), 1)

    def test_stale_ui_keys_are_refused_while_close_waits_on_the_broker(self):
        self.register()
        closing, release = threading.Event(), threading.Event()
        original = self.terminals.write
        result = []
        def delayed_close(terminal_id, action, body):
            if action == "close":
                closing.set()
                if not release.wait(3):
                    raise TimeoutError("test close did not release")
            return original(terminal_id, action, body)
        self.terminals.write = delayed_close
        worker = threading.Thread(target=lambda: result.append(self.hub.terminal_close("local", "work-1", True)))
        worker.start()
        self.assertTrue(closing.wait(1))
        try:
            with self.assertRaisesRegex(ValueError, "新建终端"):
                self.hub.terminal_input("local", "work-1", "late paste must not reach terminal")
            self.assertEqual(self.terminals.writes, [])
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertTrue(result[0]["closed"])

    def test_user_input_can_preempt_a_ceo_prompt_while_foreground_probe_waits(self):
        self.register()
        probing, release, human_done = threading.Event(), threading.Event(), threading.Event()
        writes = self.terminals.write
        errors, replies = [], []
        def delayed_write(terminal_id, action, body):
            if action == "prompt":
                probing.set()
                if not release.wait(3):
                    raise TimeoutError("test probe not released")
                raise BrokerError("User started typing during probe", "human_busy", 409)
            return writes(terminal_id, action, body)
        self.terminals.write = delayed_write
        def ceo():
            try:
                replies.append(self.send())
            except Exception as error:
                errors.append(error)
        def human():
            try:
                self.hub.terminal_input("local", "work-1", "my draft")
                human_done.set()
            except Exception as error:
                errors.append(error)
        ceo_thread = threading.Thread(target=ceo, daemon=True)
        ceo_thread.start()
        self.assertTrue(probing.wait(2))
        human_thread = threading.Thread(target=human, daemon=True)
        human_thread.start()
        try:
            self.assertTrue(human_done.wait(1), "CEO registry lock blocked direct human keyboard input")
        finally:
            release.set()
            ceo_thread.join(4)
            human_thread.join(4)
        self.assertEqual(errors, [])
        self.assertEqual(replies[0]["state"], "human_busy")
        self.assertEqual(self.terminals.writes[0][2]["data"], "my draft")

    def test_invalid_terminal_creation_never_registers_or_spawns(self):
        for args in ({"cwd": "relative/path"}, {"title": ""}, {"cols": 0}, {"rows": True}, {"request_key": ""}):
            body = {"host": "local", "cwd": "C:/workspace/project", "title": "Work", **args}
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.hub.create_terminal(**body)
        self.assertEqual(self.hub.store.agents(), [])
        self.assertFalse(any(call[0] == "create" for call in self.terminals.calls))

    def test_uncertain_shell_creation_is_durable_without_spawn_replay(self):
        create = self.terminals.create
        def lost_reply(body):
            create(body)
            raise TimeoutError("create reply lost after shell opened")
        self.terminals.create = lost_reply
        args = {"host": "local", "cwd": "C:/workspace/project", "title": "Work", "request_key": "uncertain-shell"}
        first = self.hub.create_terminal(**args)
        self.assertFalse(first["created"])
        self.assertEqual(first["delivery"]["state"], "unknown")
        self.restart()
        replay = self.hub.create_terminal(**args)
        self.assertEqual(replay, first)
        self.assertEqual(sum(call[0] == "create" for call in self.terminals.calls), 1)
        self.assertEqual(len(self.terminals.records), 1)


if __name__ == "__main__":
    unittest.main()
