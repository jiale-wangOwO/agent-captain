"""Scoped read-only history fixtures: no SSH, tmux, PTY or worker input."""
from __future__ import annotations

import copy
import json
import subprocess
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from agent_captain.transport import remote_terminal_history as history
from agent_captain.server.app import create_server
from agent_captain.core.terminal_hub import Hub
from tests.test_terminal_hub import FakeTerminals

TERMINAL = "ceohub-0123456789abcdef01234567"
OTHER_TERMINAL = "ceohub-fedcba9876543210fedcba98"
IDENTITY = {"name": TERMINAL, "session": "$11", "created": 1234,
            "pane": "%20", "pane_pid": 777777, "pane_start_ticks": 888888}


def probe_result(**extra):
    return {"identity": dict(IDENTITY), "cols": 100, "rows": 30,
            "history_size": 60, "alternate_on": False, **extra}


def captured(lines=5000, text="older row\nrecent row"):
    return history.capture_history(TERMINAL, lines, probe=lambda _: probe_result(),
                                   capture=lambda pane, count: (text.encode(), False))


def ssh_reply(result=None, **extra):
    return types.SimpleNamespace(returncode=0, stdout=history.RESULT_MARKER + json.dumps(
        {"ok": True, "result": result or captured()}, ensure_ascii=False) + "\n", stderr="", **extra)


class HelperHistoryTests(unittest.TestCase):
    def test_exact_immutable_pane_is_captured_with_before_after_witness(self):
        probe = Mock(side_effect=[probe_result(), probe_result()])
        capture = Mock(return_value=(b"older\nnewer\n", False))
        result = history.capture_history(TERMINAL, 50, probe=probe, capture=capture)
        self.assertEqual(probe.call_args_list[0].args, (TERMINAL,))
        self.assertEqual(probe.call_count, 2)
        capture.assert_called_once_with("%20", 50)
        self.assertEqual(result["text"], "older\nnewer")
        self.assertEqual(result["source"], "tmux_pane_history")
        self.assertEqual(result["identity"], IDENTITY)
        self.assertFalse(result["history_complete"])

    def test_invalid_scope_and_line_bounds_fail_before_probe(self):
        probe = Mock()
        for terminal in ("", None, "ordinary-session", TERMINAL + ";send-keys", "-t%1"):
            with self.assertRaises(history.HistoryError):
                history.capture_history(terminal, probe=probe)
        for lines in (True, 0, 5001, "50", 1.2):
            with self.assertRaises(history.HistoryError):
                history.capture_history(TERMINAL, lines, probe=probe)
        probe.assert_not_called()

    def test_every_identity_component_must_stay_identical(self):
        for key, value in (("name", OTHER_TERMINAL), ("session", "$12"), ("created", 1235),
                           ("pane", "%21"), ("pane_pid", 777778), ("pane_start_ticks", 888889)):
            with self.subTest(key=key):
                after = probe_result()
                after["identity"][key] = value
                with self.assertRaisesRegex(history.HistoryError, "identity_changed"):
                    history.capture_history(TERMINAL, probe=Mock(side_effect=[probe_result(), after]),
                                            capture=lambda *_: (b"DO NOT RETURN", False))

    def test_geometry_or_alternate_transition_rejects_capture_but_output_growth_does_not(self):
        for field, value in (("cols", 101), ("rows", 31), ("alternate_on", True)):
            with self.subTest(field=field), self.assertRaisesRegex(history.HistoryError, "identity_changed"):
                history.capture_history(TERMINAL, probe=Mock(side_effect=[probe_result(), probe_result(**{field: value})]),
                                        capture=lambda *_: (b"DO NOT RETURN", False))
        result = history.capture_history(TERMINAL, probe=Mock(side_effect=[probe_result(), probe_result(history_size=61)]),
                                        capture=lambda *_: (b"same retained pane", False))
        self.assertEqual(result["history_size"], 60)

    def test_plain_text_controls_are_removed_never_replayed(self):
        raw = "中文\x1b[31m\x00\x7f\x85\r\nnext\ttab".encode()
        result = history.capture_history(TERMINAL, probe=lambda _: probe_result(), capture=lambda *_: (raw, False))
        self.assertEqual(result["text"], "中文[31m\nnext\ttab")
        self.assertFalse(any(ord(c) < 32 and c not in "\n\t" or 127 <= ord(c) <= 159 for c in result["text"]))

    def test_line_and_byte_limits_keep_only_complete_newest_rows_and_flag_truncation(self):
        result = history.capture_history(TERMINAL, 2, probe=lambda _: probe_result(),
                                        capture=lambda *_: (b"first\nsecond\nthird\n", False))
        self.assertEqual(result["text"], "second\nthird")
        self.assertEqual(result["line_count"], 2)
        self.assertTrue(result["truncated"])
        result = history.capture_history(TERMINAL, probe=lambda _: probe_result(),
                                        capture=lambda *_: (b"partial row\nwhole row\n", True))
        self.assertEqual(result["text"], "whole row")
        self.assertTrue(result["truncated"])
        with self.assertRaises(history.HistoryError):
            history.capture_history(TERMINAL, probe=lambda _: probe_result(),
                                    capture=lambda *_: (b"x" * (history.MAX_TEXT_BYTES + 1), False))

    def test_alternate_mode_is_labelled_not_claimed_as_complete_conversation(self):
        result = history.capture_history(TERMINAL, probe=lambda _: probe_result(alternate_on=True, history_size=0),
                                        capture=lambda *_: (b"current alternate frame", False))
        self.assertTrue(result["alternate_on"])
        self.assertFalse(result["history_complete"])
        self.assertIn("备用屏幕", result["note"])

    def test_all_blank_capture_has_empty_text_and_zero_lines(self):
        for raw in (b"", b"\n", b"\n\n\n", b"\r\n\r\n", b"\x00\n\x1b\n"):
            with self.subTest(raw=raw):
                result = history.capture_history(TERMINAL, probe=lambda _: probe_result(),
                                                capture=lambda *_: (raw, False))
                self.assertEqual(result["text"], "")
                self.assertEqual(result["line_count"], 0)

    def test_native_probe_reads_only_exact_target_and_kernel_start_witness(self):
        fields = f"{TERMINAL}\t$11\t1234\t%20\t777777\t100\t30\t60\t0\n".encode()
        with patch.object(history.subprocess, "run", return_value=types.SimpleNamespace(returncode=0, stdout=fields)) as run, \
             patch.object(history, "_pane_start_ticks", return_value=888888) as ticks:
            result = history.probe_pane(TERMINAL)
        self.assertEqual(result["identity"], IDENTITY)
        ticks.assert_called_once_with(777777)
        args, kwargs = run.call_args
        self.assertEqual(args[0][:5], ["tmux", "display-message", "-p", "-t", "=" + TERMINAL + ":"])
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertFalse(any(word in args[0] for word in ("list-sessions", "send-keys", "attach-session", "copy-mode")))

    def test_native_probe_invalid_metadata_and_provider_details_are_not_exposed(self):
        for raw in (b"PRIVATE_SECRET", (f"{OTHER_TERMINAL}\t$11\t1234\t%20\t777777\t100\t30\t60\t0").encode(),
                    (f"{TERMINAL}\t$11\t1234\t%20\t777777\t501\t30\t60\t0").encode()):
            with patch.object(history.subprocess, "run", return_value=types.SimpleNamespace(returncode=0, stdout=raw)), \
                 self.assertRaisesRegex(history.HistoryError, "^identity_unavailable$"):
                history.probe_pane(TERMINAL)

    def test_capture_pipe_is_memory_bounded_and_uses_only_stdout_capture(self):
        class FakeSelector:
            def __enter__(self): return self
            def __exit__(self, *_): return None
            def register(self, fileobj, _): self.mapping = {7: types.SimpleNamespace(fd=7, fileobj=fileobj)}
            def get_map(self): return self.mapping
            def select(self, _): return [(self.mapping[7], 1)]
            def unregister(self, _): self.mapping.clear()
        child = Mock()
        child.stdout = Mock()
        child.wait.return_value = 0
        child.poll.return_value = 0
        with patch.object(history.subprocess, "Popen", return_value=child) as popen, \
             patch.object(history.selectors, "DefaultSelector", FakeSelector), \
             patch.object(history.os, "read", side_effect=[b"a" * history.MAX_TEXT_BYTES, b"newest\n", b""]):
            data, truncated = history.capture_text("%20", 50)
        self.assertEqual(len(data), history.MAX_TEXT_BYTES)
        self.assertTrue(data.endswith(b"newest\n"))
        self.assertTrue(truncated)
        self.assertEqual(popen.call_args.args[0], ["tmux", "capture-pane", "-p", "-S", "-50", "-E", "-", "-t", "%20"])
        self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(popen.call_args.kwargs["stderr"], subprocess.DEVNULL)
        child.kill.assert_not_called()
        child.stdout.close.assert_called_once()


class HubTerminalHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.terminals = FakeTerminals()
        self.hub = Hub(self.temp.name, terminals=self.terminals)
        self.hub.hosts = {"local": {"transport": "local", "label": "Local"},
                          "remote": {"transport": "ssh", "target": "fake316", "label": "Remote"}}
        self.terminals.add(TERMINAL, "remote")
        self.hub.store.remember("remote", "worker", "Worker", active=False, mode="terminal", terminal_id=TERMINAL, cwd="/work")

    def tearDown(self):
        self.hub.close()
        self.temp.cleanup()

    def test_selected_inactive_worker_history_is_one_readonly_ssh_not_global_scan(self):
        with patch("agent_captain.core.terminal_hub.subprocess.run", return_value=ssh_reply()) as run:
            result = self.hub.terminal_history("remote", "worker")
        self.assertEqual(result["text"], "older row\nrecent row")
        self.assertEqual(result["terminal_id"], TERMINAL)
        self.assertNotIn("identity", result)
        self.assertFalse(result["history_complete"])
        run.assert_called_once()
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ControlMaster=no", "-o", "ControlPersist=no", "-o", "ConnectTimeout=30", "fake316",
                                       "python3 - --session " + TERMINAL + " --lines 5000"])
        self.assertEqual(kwargs["timeout"], 45)
        self.assertIn("def capture_history", kwargs["input"])
        self.assertFalse(self.terminals.writes)
        self.assertFalse(any(call[0] == "create" for call in self.terminals.calls))
        self.assertFalse(self.hub.store.agent("remote", "worker")["active"])

    def test_local_unknown_history_only_closed_or_invalid_scope_never_runs_ssh(self):
        self.terminals.add(OTHER_TERMINAL, "local")
        self.hub.store.remember("local", "local-worker", "Local", mode="terminal", terminal_id=OTHER_TERMINAL)
        with patch("agent_captain.core.terminal_hub.subprocess.run") as run:
            for host, thread in (("remote", "missing"), ("local", "local-worker"), ("missing", "worker")):
                with self.assertRaises((ValueError, PermissionError)):
                    self.hub.terminal_history(host, thread)
            for mode in ("closed", "closing", "close_unknown"):
                self.hub.store.remember("remote", "worker", "Worker", mode=mode)
                with self.assertRaises(ValueError):
                    self.hub.terminal_history("remote", "worker")
            run.assert_not_called()
        self.assertFalse(self.terminals.writes)

    def test_bounds_and_ssh_alias_validation_happen_before_remote_io(self):
        with patch("agent_captain.core.terminal_hub.subprocess.run") as run:
            for lines in (True, 0, 5001, "100"):
                with self.assertRaises(ValueError):
                    self.hub.terminal_history("remote", "worker", lines)
            for target in ("-evil", "fake host", "fake\ncommand", None):
                self.hub.hosts["remote"]["target"] = target
                with self.assertRaises(ValueError):
                    self.hub.terminal_history("remote", "worker")
            run.assert_not_called()

    def test_bad_or_oversized_or_control_response_is_rejected_without_raw_details(self):
        base = captured()
        variants = []
        for key, value in (("terminal_id", OTHER_TERMINAL), ("source", "codex_rollout"), ("lines", 4999),
                           ("text", "PRIVATE_SECRET\x1b[31m"), ("line_count", 5001), ("history_complete", True),
                           ("cols", 501), ("alternate_on", 1), ("captured_at", "x" * 129)):
            result = copy.deepcopy(base)
            result[key] = value
            variants.append(ssh_reply(result))
        changed_identity = copy.deepcopy(base)
        changed_identity["identity"]["name"] = OTHER_TERMINAL
        variants.append(ssh_reply(changed_identity))
        variants += [types.SimpleNamespace(returncode=1, stdout="PRIVATE_SECRET", stderr="PRIVATE_AUTH"),
                     types.SimpleNamespace(returncode=0, stdout="x" * (history.MAX_RESPONSE_BYTES + 1), stderr=""),
                     types.SimpleNamespace(returncode=0, stdout=ssh_reply().stdout * 2, stderr=""),
                     types.SimpleNamespace(returncode=0, stdout=history.RESULT_MARKER + "{bad PRIVATE_SECRET", stderr="")]
        for reply in variants:
            with self.subTest(reply=str(reply)[:50]), patch("agent_captain.core.terminal_hub.subprocess.run", return_value=reply), \
                 self.assertRaises(ValueError) as error:
                self.hub.terminal_history("remote", "worker")
            self.assertNotIn("PRIVATE_", str(error.exception))
        self.assertFalse(self.terminals.writes)

    def test_timeout_and_identity_change_are_explicit_safe_failures(self):
        with patch("agent_captain.core.terminal_hub.subprocess.run", side_effect=subprocess.TimeoutExpired("PRIVATE_COMMAND", 25)), \
             self.assertRaisesRegex(ValueError, "只读终端历史暂不可用"):
            self.hub.terminal_history("remote", "worker")
        reply = types.SimpleNamespace(returncode=1, stderr="", stdout=history.RESULT_MARKER + json.dumps(
            {"ok": False, "reason": "identity_changed"}))
        with patch("agent_captain.core.terminal_hub.subprocess.run", return_value=reply), self.assertRaisesRegex(ValueError, "身份或显示状态已变化"):
            self.hub.terminal_history("remote", "worker")

    def test_registration_transport_or_closed_state_changed_during_io_rejects_text(self):
        for change in ("terminal", "target", "closed"):
            self.hub.store.remember("remote", "worker", "Worker", mode="terminal", terminal_id=TERMINAL)
            self.hub.hosts["remote"]["target"] = "fake316"
            def reply(*_, **__):
                if change == "terminal":
                    self.hub.store.remember("remote", "worker", "Worker", terminal_id=OTHER_TERMINAL)
                elif change == "target":
                    self.hub.hosts["remote"]["target"] = "changed-alias"
                else:
                    self.hub.store.remember("remote", "worker", "Worker", mode="closed")
                return ssh_reply()
            with self.subTest(change=change), patch("agent_captain.core.terminal_hub.subprocess.run", side_effect=reply), self.assertRaises(ValueError):
                self.hub.terminal_history("remote", "worker")
        self.assertFalse(self.terminals.writes)

    def test_controller_reference_is_not_worker_history_even_with_valid_registration(self):
        with patch.object(self.hub.controllers, "is_controller_reference", return_value=True), \
             patch("agent_captain.core.terminal_hub.subprocess.run") as run, self.assertRaises(PermissionError):
            self.hub.terminal_history("remote", "worker")
        run.assert_not_called()

    def test_disconnected_display_history_read_never_revives_attachment(self):
        self.terminals.records[TERMINAL].update(connected=False, state="exited")
        with patch("agent_captain.core.terminal_hub.subprocess.run", return_value=ssh_reply()):
            result = self.hub.terminal_history("remote", "worker")
        self.assertEqual(result["source"], "tmux_pane_history")
        self.assertFalse(self.terminals.records[TERMINAL]["connected"])
        self.assertFalse(self.terminals.writes)
        self.assertFalse(any(call[0] == "create" for call in self.terminals.calls))

    def test_http_get_uses_human_cookie_not_bearer_and_only_selected_scope(self):
        server = create_server(self.hub, port=0, token="fixture-user-cookie-token")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        opener = build_opener(ProxyHandler({}))
        url = f"http://127.0.0.1:{server.server_port}/api/terminals/history?host=remote&thread_id=worker&lines=50"
        try:
            with patch("agent_captain.core.terminal_hub.subprocess.run", return_value=ssh_reply(captured(lines=50))) as run:
                for headers in ({}, {"Authorization": "Bearer fixture-user-cookie-token"}):
                    with self.assertRaises(HTTPError) as error:
                        opener.open(Request(url, headers=headers))
                    self.assertEqual(error.exception.code, 404)
                self.assertEqual(run.call_count, 0)
                with opener.open(Request(url, headers={"Cookie": "ceo_session=fixture-user-cookie-token"})) as response:
                    result = json.load(response)
                self.assertEqual(result["lines"], 50)
                self.assertEqual(result["terminal_id"], TERMINAL)
                self.assertEqual(run.call_count, 1)
                with self.assertRaises(HTTPError) as error:
                    opener.open(Request(url + "01", headers={"Cookie": "ceo_session=fixture-user-cookie-token"}))
                self.assertEqual(error.exception.code, 400)
                self.assertEqual(run.call_count, 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
