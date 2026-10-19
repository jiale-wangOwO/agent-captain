"""Explicit terminal replacement cleanup, with fake tmux and identity sources."""
from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from agent_captain.transport import remote_terminal_close as close


SESSION = "ceohub-0123456789abcdef01234567"
IDENTITY = {"name": SESSION, "session": "$17", "created": 1792000000,
    "pane": "%21", "pane_pid": 1234, "pane_start_ticks": 9876,
    "boot_id": "12345678-1234-1234-1234-123456789abc", "server_pid": 4321}


def result(code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


class RemoteTerminalCloseTests(unittest.TestCase):
    def test_identity_schema_requires_boot_server_and_exact_owned_name(self):
        self.assertTrue(close.valid_identity(IDENTITY, SESSION))
        for change in ({"boot_id": None}, {"pane_start_ticks": True}, {"session": "prefix"},
                       {"server_pid": 0}, {"name": SESSION + "a"}, {"extra": 1}):
            with self.subTest(change=change):
                self.assertFalse(close.valid_identity({**IDENTITY, **change}, SESSION))

    def test_exact_tmux_identity_is_checked_atomically_and_killed_by_immutable_id(self):
        before = {"ok": True, "terminal_id": SESSION, "exists": True, "identity": IDENTITY}
        with patch.object(close, "probe", return_value=before), \
                patch.object(close, "boot_id", return_value=IDENTITY["boot_id"]), \
                patch.object(close, "pane_start_ticks", return_value=IDENTITY["pane_start_ticks"]), \
                patch.object(close, "run_tmux", return_value=result()) as tmux, \
                patch.object(close, "session_rows", return_value=[]):
            reply = close.close(SESSION, expected_identity=IDENTITY)
        self.assertTrue(reply["closed_verified"])
        arguments = tmux.call_args.args
        self.assertEqual(arguments[:4], ("if-shell", "-F", "-t", "$17:"))
        self.assertEqual(arguments[-2:], ("kill-session -t '$17'", ""))
        for field in ("session_name", "session_id", "session_created", "pane_id", "pane_pid", "pid"):
            self.assertIn("#{" + field + "}", arguments[4])
        self.assertNotIn("kill-session -t " + SESSION, arguments)

    def test_absence_expectation_cannot_kill_a_now_existing_replacement(self):
        with patch.object(close, "probe", return_value={"ok": True, "exists": True, "identity": IDENTITY}), \
                patch.object(close, "run_tmux", side_effect=AssertionError("no deletion")):
            with self.assertRaisesRegex(close.CloseError, "identity_changed"):
                close.close(SESSION, expected_absent=True)

    def test_changed_boot_pane_or_server_is_never_killed(self):
        for change in ({"boot_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"}, {"pane": "%22"},
                       {"server_pid": 9999}, {"pane_start_ticks": 9877}, {"created": 1792000001}):
            with self.subTest(change=change), patch.object(close, "probe", return_value={
                    "ok": True, "exists": True, "identity": {**IDENTITY, **change}}), \
                    patch.object(close, "run_tmux", side_effect=AssertionError("no deletion")):
                with self.assertRaisesRegex(close.CloseError, "identity_changed"):
                    close.close(SESSION, expected_identity=IDENTITY)

    def test_no_longer_existing_expected_identity_is_not_silently_treated_as_expected_absence(self):
        with patch.object(close, "probe", return_value={"ok": True, "exists": False}), \
                patch.object(close, "run_tmux", side_effect=AssertionError("no deletion")):
            with self.assertRaisesRegex(close.CloseError, "identity_changed"):
                close.close(SESSION, expected_identity=IDENTITY)

    def test_absent_receipt_requires_two_affirmative_queries_and_never_deletes(self):
        with patch.object(close, "probe", return_value={"ok": True, "exists": False}), \
                patch.object(close, "session_rows", return_value=[]) as after, \
                patch.object(close, "run_tmux", side_effect=AssertionError("no deletion")):
            self.assertTrue(close.close(SESSION, expected_absent=True)["closed_verified"])
            after.assert_called_once_with()

    def test_post_close_same_name_or_id_blocks_receipt(self):
        for rows in ([[SESSION, "$18"]], [["unrelated-name", "$17"]]):
            with self.subTest(rows=rows), patch.object(close, "probe", return_value={
                    "ok": True, "exists": True, "identity": IDENTITY}), \
                    patch.object(close, "boot_id", return_value=IDENTITY["boot_id"]), \
                    patch.object(close, "pane_start_ticks", return_value=IDENTITY["pane_start_ticks"]), \
                    patch.object(close, "run_tmux", return_value=result()), \
                    patch.object(close, "session_rows", return_value=rows):
                with self.assertRaisesRegex(close.CloseError, "close_unconfirmed"):
                    close.close(SESSION, expected_identity=IDENTITY)

    def test_only_success_or_explicit_no_server_counts_as_absence(self):
        for reply in (result(stdout="unrelated|$12\n"),
                      result(1, stderr="no server running on /tmp/tmux-1000/default\n")):
            with patch.object(close, "run_tmux", return_value=reply):
                self.assertFalse(any(name == SESSION for name, _ in close.session_rows()))
        for reply in (result(1), result(1, stderr="permission denied\n"),
                      result(127, stderr="tmux: not found\n"), result(1, stderr="Connection reset\n"),
                      result(stdout="malformed row\n")):
            with self.subTest(reply=reply), patch.object(close, "run_tmux", return_value=reply):
                with self.assertRaises(close.CloseError):
                    close.session_rows()

    def test_no_binary_or_timeout_is_unknown_not_absent(self):
        for error in (FileNotFoundError(), subprocess.TimeoutExpired("tmux", 5)):
            with self.subTest(error=error), patch.object(close.subprocess, "run", side_effect=error):
                with self.assertRaisesRegex(close.CloseError, "tmux_unavailable"):
                    close.session_rows()

    def test_missing_socket_reply_also_requires_filesystem_absence(self):
        reply = result(1, stderr="error connecting to /tmp/inert-tmux-test/default (No such file or directory)\n")
        with patch.object(close, "run_tmux", return_value=reply), \
                patch.object(close.Path, "lstat", side_effect=FileNotFoundError()):
            self.assertEqual(close.session_rows(), [])
        for observation in (None, PermissionError()):
            with self.subTest(observation=observation), patch.object(close, "run_tmux", return_value=reply), \
                    patch.object(close.Path, "lstat", side_effect=observation):
                with self.assertRaisesRegex(close.CloseError, "tmux_query_failed"):
                    close.session_rows()

    def test_probe_returns_only_exact_session_identity_with_boot_and_server(self):
        rows = result(stdout=SESSION + "|$17\n")
        metadata = result(stdout=SESSION + "|$17|1792000000|%21|1234|4321\n")
        with patch.object(close, "run_tmux", side_effect=[rows, metadata]) as tmux, \
                patch.object(close, "boot_id", return_value=IDENTITY["boot_id"]), \
                patch.object(close, "pane_start_ticks", return_value=9876):
            self.assertEqual(close.probe(SESSION), {"ok": True, "terminal_id": SESSION,
                "exists": True, "identity": IDENTITY})
        self.assertEqual(tmux.call_args_list[1].args[:4], ("display-message", "-p", "-t", "$17:"))

    def test_ascii_format_survives_real_run_tmux_c_locale_sanitizing_tabs(self):
        # Model the real tmux boundary instead of mocking run_tmux's already
        # parsed output: LC_ALL=C replaces literal tabs in format output with _.
        # The old tab-delimited formats therefore fail this regression fixture.
        def tmux_process(argv, **kwargs):
            self.assertEqual(kwargs["env"]["LC_ALL"], "C")
            template = argv[-1]
            values = {"session_name": SESSION, "session_id": "$17", "session_created": "1792000000",
                "pane_id": "%21", "pane_pid": "1234", "pid": "4321"}
            for field, value in values.items():
                template = template.replace("#{" + field + "}", value)
            self.assertNotIn("\t", template)
            return result(stdout=template.replace("\t", "_") + "\n")
        with patch.object(close.subprocess, "run", side_effect=tmux_process) as process, \
                patch.object(close, "boot_id", return_value=IDENTITY["boot_id"]), \
                patch.object(close, "pane_start_ticks", return_value=9876):
            self.assertEqual(close.probe(SESSION)["identity"], IDENTITY)
        self.assertEqual(process.call_args_list[0].args[0], ["tmux", "list-sessions", "-F", "#{session_name}|#{session_id}"])
        self.assertEqual(process.call_args_list[1].args[0][-1],
            "#{session_name}|#{session_id}|#{session_created}|#{pane_id}|#{pane_pid}|#{pid}")

    def test_external_name_with_separator_does_not_corrupt_registry_or_match_owned_name(self):
        with patch.object(close, "run_tmux", return_value=result(stdout="external|name|$0\n" + SESSION + "|$17\n")):
            self.assertEqual(close.session_rows(), [["external|name", "$0"], [SESSION, "$17"]])

    def test_probe_rejects_extra_metadata_fields_instead_of_ignoring_them(self):
        rows = result(stdout=SESSION + "|$17\n")
        metadata = result(stdout=SESSION + "|$17|1792000000|%21|1234|4321|unexpected\n")
        with patch.object(close, "run_tmux", side_effect=[rows, metadata]):
            with self.assertRaisesRegex(close.CloseError, "identity_changed"):
                close.probe(SESSION)

    def test_plain_session_name_prefix_is_not_a_match_for_the_registered_terminal(self):
        with patch.object(close, "session_rows", return_value=[[SESSION + "a", "$17"]]), \
                patch.object(close, "run_tmux", side_effect=AssertionError("exact names only")):
            self.assertEqual(close.probe(SESSION), {"ok": True, "terminal_id": SESSION, "exists": False})


if __name__ == "__main__":
    unittest.main()
