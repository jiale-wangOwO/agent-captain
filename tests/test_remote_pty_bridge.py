"""Pure helper validation/rollback tests; no SSH, PTY, model or user process."""
import importlib.util
import base64
import io
import json
import os
import signal
import struct
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch, Mock


if os.name == "nt":
    fake_fcntl = types.SimpleNamespace(ioctl=None)
    fake_termios = types.SimpleNamespace(TIOCGWINSZ=1, TIOCSWINSZ=2)
    fake_signal = types.SimpleNamespace(SIGWINCH=28)
    modules = {"fcntl": fake_fcntl, "termios": fake_termios, "signal": fake_signal}
else:
    modules = {}
spec = importlib.util.spec_from_file_location("test_owned_pty_bridge", Path(__file__).resolve().parents[1] / "remote_pty_bridge.py")
bridge = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, modules):
    spec.loader.exec_module(bridge)


class BridgeTests(unittest.TestCase):
    def attachment(self):
        value = bridge.Attachment.__new__(bridge.Attachment)
        value.session_id = "ceohub-0123456789abcdef01234567"
        value.bridge_id = "0123456789abcdef01234567"
        value.tty = "/dev/pts/owned-test"
        value.child = types.SimpleNamespace(pid=999999, poll=lambda: None)
        value.lock = threading.RLock()
        value.stop = threading.Event()
        value.cols, value.rows = 100, 30
        return value

    def scenario(self, fail_first_redraw=False, fail_rollback=False):
        attachment = self.attachment()
        state = {"kernel": (100, 30), "window": (100, 30), "client": (100, 30), "redraws": 0, "calls": []}
        def ioctl(fd, operation, value):
            self.assertEqual(fd, 0, "Resize must use the inherited original descriptor")
            if operation == bridge.termios.TIOCGWINSZ:
                cols, rows = state["kernel"]
                return struct.pack("HHHH", rows, cols, 0, 0)
            rows, cols, _, _ = struct.unpack("HHHH", value)
            if fail_rollback and (cols, rows) == (100, 30):
                raise OSError("rollback unavailable")
            state["kernel"] = (cols, rows)
            state["calls"].append(("kernel", cols, rows))
        def kill(pid, signum):
            self.assertEqual(pid, attachment.child.pid)
            self.assertEqual(signum, bridge.signal.SIGWINCH)
            state["client"] = state["kernel"]
            state["calls"].append(("client", *state["client"]))
        def tmux(*args):
            if args[0] == "resize-window":
                self.assertEqual(state["client"], state["kernel"], "Cached client must acknowledge before window resize")
                state["window"] = (int(args[args.index("-x") + 1]), int(args[args.index("-y") + 1]))
                state["calls"].append(("window", *state["window"]))
            if args[0] == "refresh-client":
                state["redraws"] += 1
                if fail_first_redraw and state["redraws"] == 1:
                    raise bridge.BridgeError("test redraw failed")
            return ""
        attachment.client_geometry = lambda: state["client"]
        attachment.window_geometry = lambda: (*state["window"], 888888, *state["window"])
        return attachment, state, ioctl, kill, tmux

    def test_validation_rejects_foreign_ids_controls_and_boolean_sizes(self):
        for cols, rows in ((True, 30), (19, 30), (100, 201), (100, False)):
            with self.subTest(cols=cols, rows=rows), self.assertRaises(bridge.BridgeError):
                bridge.size(cols, rows)
        with self.assertRaises(bridge.BridgeError):
            bridge.identity("user-existing-tmux", "0123456789abcdef01234567")
        with self.assertRaises(bridge.BridgeError):
            bridge.identity("ceohub-0123456789abcdef01234567", "../foreign")

    def test_success_orders_original_fd_client_ack_and_window_resize(self):
        value, state, ioctl, kill, tmux = self.scenario()
        with patch.object(bridge.fcntl, "ioctl", ioctl), patch.object(bridge.os, "kill", kill), patch.object(bridge, "tmux", tmux):
            result = value.resize(73, 33)
        self.assertEqual(state["kernel"], (73, 33))
        self.assertEqual(state["client"], (73, 33))
        self.assertEqual(state["window"], (73, 33))
        self.assertEqual(state["calls"][:3], [("kernel", 73, 33), ("client", 73, 33), ("window", 73, 33)])
        self.assertEqual(result["pane_pid"], 888888)

    def test_partial_failure_rolls_back_and_verifies_all_three_geometries(self):
        value, state, ioctl, kill, tmux = self.scenario(fail_first_redraw=True)
        with patch.object(bridge.fcntl, "ioctl", ioctl), patch.object(bridge.os, "kill", kill), patch.object(bridge, "tmux", tmux), self.assertRaises(bridge.ResizeFailure) as error:
            value.resize(73, 33)
        self.assertTrue(error.exception.result["geometry_confirmed"])
        self.assertTrue(error.exception.result["rolled_back"])
        self.assertEqual((error.exception.result["cols"], error.exception.result["rows"]), (100, 30))
        self.assertEqual(state["kernel"], (100, 30))
        self.assertEqual(state["client"], (100, 30))
        self.assertEqual(state["window"], (100, 30))

    def test_failed_rollback_reports_unknown_instead_of_old_success(self):
        value, _, ioctl, kill, tmux = self.scenario(fail_first_redraw=True, fail_rollback=True)
        with patch.object(bridge.fcntl, "ioctl", ioctl), patch.object(bridge.os, "kill", kill), patch.object(bridge, "tmux", tmux), self.assertRaises(bridge.ResizeFailure) as error:
            value.resize(73, 33)
        self.assertFalse(error.exception.result["geometry_confirmed"])
        self.assertNotIn("cols", error.exception.result)
        self.assertNotIn("rows", error.exception.result)

    def test_invalid_request_never_performs_ioctl_or_signals(self):
        value = self.attachment()
        with patch.object(bridge.fcntl, "ioctl", side_effect=AssertionError("no mutation")), patch.object(bridge.os, "kill", side_effect=AssertionError("no signal")), self.assertRaises(bridge.BridgeError):
            value.resize(0, 30)

    def proof(self):
        return {"name": "ceohub-0123456789abcdef01234567", "session": "$11", "created": 1234,
                "pane": "%20", "pane_pid": 777777, "pane_start_ticks": 888888}

    def strict_attachment(self):
        value = self.attachment()
        value.require_existing = True
        value.expected_identity = self.proof()
        value.cwd = "/work/project"
        value.child, value.listener, value.worker, value.socket_inode = None, None, None, None
        value.path = Mock()
        value.path.lstat.return_value = types.SimpleNamespace(st_ino=123)
        return value

    def test_existing_marker_binds_exact_session_and_bounded_fingerprint(self):
        proof = self.proof()
        marker = bridge.EXISTING_MARKER + proof["name"] + "/" + base64.urlsafe_b64encode(json.dumps(proof).encode()).decode().rstrip("=")
        cwd, strict, decoded = bridge.attach_directory(marker, proof["name"])
        self.assertEqual((cwd, strict, decoded), ("", True, proof))
        self.assertEqual(bridge.attach_directory("/work/project", proof["name"]), ("/work/project", False, None))
        for invalid in (bridge.EXISTING_MARKER + "foreign", marker + "/extra", "/.__pronhub_existing_bad__/" + proof["name"]):
            with self.assertRaises(bridge.BridgeError):
                bridge.attach_directory(invalid, proof["name"])

    def test_missing_strict_original_never_starts_shell_or_display(self):
        value = self.strict_attachment()
        with patch.object(bridge, "apply_winsize"), patch.object(bridge, "probe_existing_session", return_value={"ok": True, "exists": False}), \
                patch.object(bridge, "tmux") as tmux, patch.object(bridge.subprocess, "Popen") as spawn:
            with self.assertRaisesRegex(bridge.BridgeError, "replacement was not created"):
                value.run()
        tmux.assert_not_called()
        spawn.assert_not_called()

    def test_same_name_replacement_before_attach_is_rejected(self):
        value = self.strict_attachment()
        replacement = self.proof() | {"session": "$12", "pane_pid": 777778}
        with patch.object(bridge, "apply_winsize"), patch.object(bridge, "probe_existing_session", return_value={"ok": True, "exists": True, "identity": replacement}), \
                patch.object(bridge, "tmux") as tmux, patch.object(bridge.subprocess, "Popen") as spawn:
            with self.assertRaisesRegex(bridge.BridgeError, "identity changed"):
                value.run()
        tmux.assert_not_called()
        spawn.assert_not_called()

    def test_close_recreate_race_attaches_only_immutable_id_and_ends_only_own_display(self):
        value = self.strict_attachment()
        replacement = self.proof() | {"session": "$12", "pane_pid": 777778}
        child = Mock()
        child.pid = 999999
        child.poll.return_value = None
        socket_mock = Mock()
        with patch.object(bridge, "apply_winsize"), patch.object(bridge, "probe_existing_session", side_effect=[
            {"ok": True, "exists": True, "identity": self.proof()}, {"ok": True, "exists": True, "identity": replacement}]), \
                patch.object(bridge.subprocess, "run", return_value=types.SimpleNamespace(returncode=0)), \
                patch.object(bridge.subprocess, "Popen", return_value=child) as spawn, patch.object(bridge, "tmux") as tmux, \
                patch.object(bridge.socket, "socket", return_value=socket_mock), patch.object(bridge.socket, "AF_UNIX", 1, create=True), \
                patch.object(bridge.os, "umask", return_value=0o077):
            with self.assertRaisesRegex(bridge.BridgeError, "identity changed"):
                value.run()
            value.cleanup()
        self.assertEqual(spawn.call_args.args[0], ["tmux", "-u", "attach-session", "-t", "$11"])
        self.assertNotIn("new-session", [call.args[0] for call in tmux.call_args_list])
        self.assertTrue(all(call.args[2] == "$11" for call in tmux.call_args_list))
        child.terminate.assert_called_once_with()
        child.kill.assert_not_called()

    def test_probe_reads_exact_session_and_selected_kernel_identity_only(self):
        proof = self.proof()
        fields = ["S"] + ["1"] * 19
        fields[19] = str(proof["pane_start_ticks"])
        raw = (str(proof["pane_pid"]) + " (bash) " + " ".join(fields)).encode()
        with patch.object(bridge.subprocess, "run", return_value=types.SimpleNamespace(returncode=0)) as run, \
                patch.object(bridge, "tmux", return_value="\t".join(str(proof[k]) for k in ("name", "session", "created", "pane", "pane_pid"))) as tmux, \
                patch.object(bridge.Path, "open", return_value=io.BytesIO(raw)), patch.object(bridge.Path, "stat", return_value=types.SimpleNamespace(st_uid=1000)), \
                patch.object(bridge.os, "geteuid", return_value=1000, create=True):
            result = bridge.probe_existing_session(proof["name"])
        self.assertEqual(result["identity"], proof)
        self.assertEqual(run.call_args.args[0], ["tmux", "has-session", "-t", "=" + proof["name"]])
        self.assertEqual(tmux.call_args.args[3], "=" + proof["name"])


if __name__ == "__main__":
    unittest.main()
