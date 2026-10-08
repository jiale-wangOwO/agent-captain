"""Pure fake CLI/socket maintenance tests: no SSH, services, or real processes."""
from __future__ import annotations

import base64
import errno
import hashlib
import importlib.util
import io
import json
import socket
import stat
import struct
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts/codex_sync_daemon.py"
SPEC = importlib.util.spec_from_file_location("codex_sync_daemon", MODULE_PATH)
sync = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sync
SPEC.loader.exec_module(sync)

HOME = Path("C:/fake-home" if sys.platform == "win32" else "/fake-home").absolute()
IDENTITY = sync.Identity(HOME, 1003)
CLI_PATH = HOME / ".local/bin/codex"
MANAGED_PATH = HOME / ".codex/packages/app-server-daemon/current/bin/codex"
SOCKET_PATH = HOME / ".codex/app-server-control/app-server-control.sock"
OLD_FEATURES = "api_key_model_discovery\tunder development\tfalse\nauth_elicitation\tstable\ttrue\n"
NEW_FEATURES = "api_key_model_discovery\tstable\ttrue\nauth_elicitation\tstable\ttrue\n"
UPDATE = ["app-server", "daemon", "update", "--from-cli", "--yes"]


def status(*, version="0.160.0", state="running"):
    return {"status": state, "backend": "pid", "managedCodexPath": str(MANAGED_PATH),
            "managedCodexVersion": version, "socketPath": str(SOCKET_PATH),
            "cliVersion": "0.161.0", "appServerVersion": version if state == "running" else None}


class FakeCli:
    def __init__(self, statuses=None):
        self.identity, self.resolved = IDENTITY, CLI_PATH
        self.statuses = list(statuses or [status(), status(), status(version="0.161.0")])
        self.calls, self.updated = [], False
        self.update_error = None
        self.features = NEW_FEATURES

    def verify(self):
        pass

    def run(self, args, deadline):
        deadline.remaining()
        self.calls.append(list(args))
        if args == ["app-server", "daemon", "version"]:
            return json.dumps(self.statuses.pop(0))
        if args == ["--version"]:
            return "codex-cli 0.161.0\n"
        if args == ["features", "list"]:
            return self.features
        if args == UPDATE:
            self.updated = True
            if self.update_error:
                raise self.update_error
            return "Updater output is never included in the report.\n"
        raise AssertionError(f"Unexpected command: {args!r}")


class FakeManaged:
    def __init__(self, path, identity, main):
        self.main = main

    def verify(self):
        pass

    def run(self, args, deadline):
        assert args == ["features", "list"], "Managed binary only lists feature rows"
        return NEW_FEATURES if self.main.updated else self.main.managed_features


class FakeRpc:
    def __init__(self, path, identity, deadline, *, loaded=None, version="0.160.0"):
        self.loaded = list(loaded or [{"data": []}, {"data": []}])
        self.version, self.calls, self.closed = version, [], False

    def call(self, request_id, method, params):
        self.calls.append((request_id, method, params))
        if method == "initialize":
            return {"userAgent": f"codex-tui/{self.version} (Linux)"}
        assert method == "thread/loaded/list", "Never read threads, turns, auth, or config"
        result = self.loaded.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def notify(self, method, params):
        assert (method, params) == ("initialized", {})
        self.calls.append((None, method, params))

    def close(self):
        self.closed = True

    def verify(self):
        pass


class FeatureRowsTests(unittest.TestCase):
    def test_installed_cli_dotted_feature_name(self):
        self.assertEqual(sync.feature_rows(
            "guardianv2.thread_context                removed            false\n"
            "api_key_model_discovery                  stable             true\n"
        ), {"guardianv2.thread_context": False, "api_key_model_discovery": True})

    def test_invalid_dotted_names_and_overlong_names_still_fail_closed(self):
        for name in ["guardian..context", "guardian.context.", ".guardian", "g" * 81]:
            with self.subTest(name=name), self.assertRaises(sync.GuardError):
                sync.feature_rows(name + " stable true\n")


class MaintenanceTests(unittest.TestCase):
    def run_maintenance(self, cli=None, *, loaded=None, sync_mode=False, rpc_version="0.160.0", factory_error=None):
        cli = cli or FakeCli()
        cli.managed_features = getattr(cli, "managed_features", OLD_FEATURES)
        self.cli = cli
        self.rpc = FakeRpc(SOCKET_PATH, IDENTITY, None, loaded=loaded, version=rpc_version)

        def factory(*args):
            if factory_error:
                raise factory_error
            return self.rpc

        with patch.object(sync, "checked_path", side_effect=lambda path, *_args, **_kwargs: path), \
                patch.object(sync, "Cli", side_effect=lambda path, identity: FakeManaged(path, identity, cli)):
            return sync.maintain(cli, sync=sync_mode, rpc_factory=factory)

    def assert_no_update(self):
        self.assertNotIn(UPDATE, self.cli.calls)

    def test_default_check_is_read_only_and_only_lists_loaded_ids(self):
        report = self.run_maintenance()
        self.assertTrue(report["ok"])
        self.assertTrue(report["syncNeeded"])
        self.assertEqual(report["runningFeatureMode"], "not_reported_by_daemon_version")
        self.assertEqual([call[1] for call in self.rpc.calls], ["initialize", "initialized", "thread/loaded/list"])
        self.assert_no_update()
        self.assertTrue(self.rpc.closed)

    def test_even_idle_loaded_thread_blocks_sync_and_is_not_output(self):
        report = self.run_maintenance(loaded=[{"data": ["idle-private-session"]}], sync_mode=True)
        self.assertFalse(report["ok"])
        self.assertEqual(report["action"], "blocked_loaded_threads")
        self.assertEqual(report["loadedThreadCount"], 1)
        self.assertNotIn("idle-private-session", json.dumps(report))
        self.assert_no_update()

    def test_same_versions_and_feature_rows_noop_still_probes_loaded_threads(self):
        cli = FakeCli([status(version="0.161.0")])
        cli.managed_features = NEW_FEATURES
        report = self.run_maintenance(cli, sync_mode=True, rpc_version="0.161.0")
        self.assertEqual(report["action"], "already_synchronized")
        self.assert_no_update()
        self.assertEqual(self.rpc.calls[-1][1], "thread/loaded/list")

    def test_matching_cli_managed_running_versions_with_different_features_need_sync(self):
        cli = FakeCli([status(version="0.161.0")])
        report = self.run_maintenance(cli, rpc_version="0.161.0")
        self.assertTrue(report["versionsMatch"])
        self.assertFalse(report["cliAndManagedDefaultsMatch"])
        self.assertTrue(report["syncNeeded"])
        self.assert_no_update()

    def test_same_versions_still_refuse_sync_when_an_idle_thread_is_loaded(self):
        cli = FakeCli([status(version="0.161.0")])
        cli.managed_features = NEW_FEATURES
        report = self.run_maintenance(cli, loaded=[{"data": ["idle-thread"]}],
                                      sync_mode=True, rpc_version="0.161.0")
        self.assertFalse(report["ok"])
        self.assertEqual(report["action"], "blocked_loaded_threads")
        self.assert_no_update()

    def test_matching_managed_but_older_running_version_is_not_noop(self):
        before = status(version="0.161.0")
        before["appServerVersion"] = "0.160.0"
        cli = FakeCli([before, before, status(version="0.161.0")])
        cli.managed_features = NEW_FEATURES
        report = self.run_maintenance(cli, sync_mode=True)
        self.assertEqual(report["action"], "synchronized")
        self.assertEqual(self.cli.calls.count(UPDATE), 1)

    def test_sync_updates_once_with_exact_approved_command_and_verifies(self):
        report = self.run_maintenance(sync_mode=True)
        self.assertEqual(report["action"], "synchronized")
        self.assertEqual(self.cli.calls.count(UPDATE), 1)
        self.assertEqual([call[1] for call in self.rpc.calls],
                         ["initialize", "initialized", "thread/loaded/list", "thread/loaded/list"])
        self.assertEqual(report["after"]["appServerVersion"], "0.161.0")

    def test_failed_unknown_or_paginated_loaded_probe_never_updates(self):
        for value in [{}, {"data": None}, {"data": [1]}, {"data": [], "nextCursor": "more"},
                      sync.GuardError("rpc_failed")]:
            with self.subTest(value=value), self.assertRaises(sync.GuardError):
                self.run_maintenance(loaded=[value], sync_mode=True)
            self.assert_no_update()

    def test_installed_cli_complete_null_cursor_is_read_only(self):
        report = self.run_maintenance(loaded=[{"data": [], "nextCursor": None}])
        self.assertTrue(report["ok"])
        self.assertEqual(report["loadedThreadCount"], 0)
        self.assert_no_update()

    def test_socket_probe_failure_and_rpc_version_mismatch_never_update(self):
        for kwargs in [{"factory_error": OSError("do not display this text")}, {"rpc_version": "0.159.0"}]:
            with self.subTest(kwargs=kwargs), self.assertRaises((sync.GuardError, OSError)):
                self.run_maintenance(sync_mode=True, **kwargs)
            self.assert_no_update()

    def test_loaded_thread_appearing_on_final_probe_blocks_update(self):
        with self.assertRaisesRegex(sync.GuardError, "loaded_threads_appeared"):
            self.run_maintenance(loaded=[{"data": []}, {"data": ["just-loaded"]}], sync_mode=True)
        self.assert_no_update()

    def test_same_version_socket_replacement_blocks_update_even_when_old_rpc_is_empty(self):
        def changed_socket(_rpc):
            raise sync.GuardError("control_socket_changed_during_maintenance")
        with patch.object(FakeRpc, "verify", changed_socket), self.assertRaisesRegex(sync.GuardError, "socket_changed"):
            self.run_maintenance(sync_mode=True)
        self.assert_no_update()

    def test_daemon_status_changing_on_final_probe_blocks_update(self):
        cli = FakeCli([status(), status(version="0.159.0")])
        with self.assertRaisesRegex(sync.GuardError, "status_changed"):
            self.run_maintenance(cli, sync_mode=True)
        self.assert_no_update()

    def test_private_group_membership_change_before_update_blocks_mutation(self):
        cli = FakeCli()
        cli.identity = sync.Identity(HOME, 1003, frozenset({1003}), lambda: frozenset())
        with self.assertRaisesRegex(sync.GuardError, "private_group_membership_changed"):
            self.run_maintenance(cli, sync_mode=True)
        self.assert_no_update()

    def test_update_timeout_or_postcheck_failure_is_never_retried(self):
        for failure in [sync.GuardError("cli_command_timeout"), None]:
            cli = FakeCli([status(), status(), status()])
            cli.update_error = failure
            report = self.run_maintenance(cli, sync_mode=True)
            self.assertFalse(report["ok"])
            self.assertTrue(report["updateAttempted"])
            self.assertEqual(report["action"], "update_unverified_do_not_retry")
            self.assertEqual(cli.calls.count(UPDATE), 1)

    def test_post_update_daemon_status_change_is_not_claimed_success_or_retried(self):
        cli = FakeCli([status(), status(), status(version="0.161.0", state="stopped")])
        report = self.run_maintenance(cli, sync_mode=True)
        self.assertFalse(report["ok"])
        self.assertEqual(report["action"], "update_unverified_do_not_retry")
        self.assertEqual(cli.calls.count(UPDATE), 1)

    def test_unknown_backend_status_or_missing_versions_fail_closed(self):
        for change in [{"status": "unknown"}, {"backend": "systemd"}, {"cliVersion": None},
                       {"managedCodexVersion": "secret-not-a-version"}, {"appServerVersion": None}]:
            before = status()
            before.update(change)
            with self.subTest(change=change), self.assertRaises(sync.GuardError):
                self.run_maintenance(FakeCli([before]), sync_mode=True)
            self.assert_no_update()

    def test_stopped_daemon_sync_does_not_connect_or_start_a_thread(self):
        cli = FakeCli([status(state="stopped"), status(state="stopped"),
                       status(version="0.161.0", state="stopped")])
        report = self.run_maintenance(cli, sync_mode=True)
        self.assertEqual(report["action"], "synchronized")
        self.assertEqual(self.rpc.calls, [])

    def test_feature_output_error_blocks_sync(self):
        for text in ["", "API_KEY=private", "feature stable true\nfeature stable false\n"]:
            cli = FakeCli()
            cli.features = text
            with self.subTest(text=text), self.assertRaises(sync.GuardError):
                self.run_maintenance(cli, sync_mode=True)
            self.assert_no_update()


class FakePopen:
    def __init__(self, data=b"codex-cli 0.161.0\n", code=0):
        self.stdout = io.BytesIO(data)
        self.code, self.killed = code, False

    def wait(self, timeout):
        return self.code

    def poll(self):
        return self.code

    def kill(self):
        self.killed = True


class CliRunnerTests(unittest.TestCase):
    def cli(self):
        cli = object.__new__(sync.Cli)
        cli.resolved = CLI_PATH
        cli.verify = lambda: None
        return cli

    def test_fake_popen_argv_has_no_shell_auth_provider_or_feature_write(self):
        fake = FakePopen()
        with patch.object(sync.subprocess, "Popen", return_value=fake) as popen:
            self.assertEqual(self.cli().run(["--version"], sync.Deadline(5)), "codex-cli 0.161.0\n")
        args, kwargs = popen.call_args
        self.assertEqual(args[0], [str(CLI_PATH), "--version"])
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["stderr"], sync.subprocess.DEVNULL)

    def test_oversized_or_nonzero_cli_output_is_not_in_error(self):
        for fake in [FakePopen(b"x" * (sync.MAX_OUTPUT + 1)), FakePopen(b"private-token", 1)]:
            with patch.object(sync.subprocess, "Popen", return_value=fake), self.assertRaises(sync.GuardError) as caught:
                self.cli().run(["--version"], sync.Deadline(5))
            self.assertNotIn("private-token", str(caught.exception))

    def test_timeout_validation_and_one_shared_deadline(self):
        for seconds in [0, -1, 61, float("inf"), float("nan")]:
            with self.subTest(seconds=seconds), self.assertRaises(sync.GuardError):
                sync.Deadline(seconds)
        with patch.object(sync.time, "monotonic", side_effect=[100, 120, 161]):
            deadline = sync.Deadline(60)
            self.assertEqual(deadline.remaining(), 40)
            with self.assertRaises(sync.GuardError):
                deadline.remaining()


def frame(payload, opcode=1, *, final=True, masked=False):
    payload = payload.encode() if isinstance(payload, str) else payload
    size = len(payload)
    length = bytes([size]) if size < 126 else b"\x7e" + struct.pack("!H", size)
    return bytes([(0x80 if final else 0) | opcode]) + length + payload


class FakeSocket:
    KEY_BYTES = b"k" * 16

    def __init__(self, frames=b"", *, valid_accept=True, uid=IDENTITY.uid):
        key = base64.b64encode(self.KEY_BYTES).decode()
        accept = base64.b64encode(hashlib.sha1((key + sync.WS_GUID).encode()).digest()).decode()
        if not valid_accept:
            accept = accept.lower()  # Acceptance is case-sensitive, not substring matching.
        self.pending = bytearray(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                                  "Connection: keep-alive, Upgrade\r\n"
                                  f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode() + frames)
        self.uid, self.sent, self.closed = uid, [], False

    def settimeout(self, seconds):
        assert 0 < seconds <= 60

    def connect(self, path):
        self.path = path

    def getsockopt(self, level, option, size):
        return struct.pack("3i", 201, self.uid, self.uid)

    def sendall(self, data):
        self.sent.append(data)

    def recv(self, count):
        result = bytes(self.pending[:count])
        del self.pending[:count]
        return result

    def close(self):
        self.closed = True


class WebSocketTests(unittest.TestCase):
    def rpc(self, fake):
        info = SimpleNamespace(st_dev=1, st_ino=2, st_uid=1003, st_gid=1003, st_mode=stat.S_IFSOCK | 0o600,
                               st_size=0, st_mtime_ns=100)
        with patch.object(sync, "checked_path", return_value=SOCKET_PATH), \
                patch.object(Path, "stat", return_value=info), \
                patch.object(sync.socket, "socket", return_value=fake), \
                patch.object(sync.socket, "AF_UNIX", 1, create=True), \
                patch.object(sync.socket, "SO_PEERCRED", 17, create=True), \
                patch.object(sync.os, "urandom", side_effect=lambda size: b"k" * size):
            return sync.UnixRpc(SOCKET_PATH, IDENTITY, sync.Deadline(5))

    def test_valid_upgrade_leaves_following_frame_intact_and_masks_client_json(self):
        fake = FakeSocket(frame('{"id":1,"result":{"userAgent":"codex-tui/0.160.0"}}'))
        rpc = self.rpc(fake)
        result = rpc.call(1, "initialize", {"clientInfo": {"name": "test"}})
        self.assertEqual(result["userAgent"], "codex-tui/0.160.0")
        sent = fake.sent[-1]
        self.assertEqual(sent[0], 0x81)
        self.assertTrue(sent[1] & 0x80, "Client frames must be masked")
        length = sent[1] & 0x7f
        mask, payload = sent[2:6], sent[6:]
        decoded = bytes(value ^ mask[i % 4] for i, value in enumerate(payload))
        self.assertEqual(len(payload), length)
        self.assertEqual(json.loads(decoded)["method"], "initialize")
        rpc.close()

    def test_bad_accept_or_wrong_peer_uid_closes_and_refuses(self):
        for fake in [FakeSocket(valid_accept=False), FakeSocket(uid=999)]:
            with self.subTest(uid=fake.uid), self.assertRaises(sync.GuardError):
                self.rpc(fake)
            self.assertTrue(fake.closed)

    def test_ping_pong_during_rpc_is_bounded_and_not_a_json_reply(self):
        fake = FakeSocket(frame(b"probe", opcode=9) + frame('{"id":2,"result":{"data":[]}}'))
        rpc = self.rpc(fake)
        self.assertEqual(rpc.call(2, "thread/loaded/list", {}), {"data": []})
        self.assertEqual(fake.sent[-1][0], 0x8a)
        self.assertTrue(fake.sent[-1][1] & 0x80)

    def test_exact_message_limit_uses_standard_127_length_and_larger_fails(self):
        fake = FakeSocket()
        rpc = self.rpc(fake)
        rpc.frame(b"x" * sync.MAX_MESSAGE)
        sent = fake.sent[-1]
        self.assertEqual(sent[:2], b"\x81\xff")
        self.assertEqual(struct.unpack("!Q", sent[2:10])[0], sync.MAX_MESSAGE)
        self.assertEqual(len(sent), sync.MAX_MESSAGE + 14)
        with self.assertRaises(sync.GuardError):
            rpc.frame(b"x" * (sync.MAX_MESSAGE + 1))

    def test_socket_inode_change_and_peer_change_fail_final_verification(self):
        for change in ["inode", "uid"]:
            fake = FakeSocket()
            rpc = self.rpc(fake)
            info = SimpleNamespace(st_dev=1, st_ino=999 if change == "inode" else 2, st_uid=1003, st_gid=1003,
                                   st_mode=stat.S_IFSOCK | 0o600, st_size=0, st_mtime_ns=100)
            if change == "uid":
                fake.uid = 999
            with patch.object(sync, "checked_path", return_value=SOCKET_PATH), \
                    patch.object(Path, "stat", return_value=info), \
                    patch.object(sync.socket, "SO_PEERCRED", 17, create=True), \
                    self.subTest(change=change), self.assertRaises(sync.GuardError):
                rpc.verify()

    def test_close_fragmentation_binary_masking_and_large_frames_fail_closed(self):
        invalid = [frame(b"", opcode=8), frame(b"{}", final=False), frame(b"{}", opcode=2),
                   b"\x81\x82kkkkxx", b"\x81\x7f" + struct.pack("!Q", sync.MAX_MESSAGE + 1)]
        for data in invalid:
            rpc = self.rpc(FakeSocket(data))
            with self.subTest(data=data), self.assertRaises(sync.GuardError):
                rpc.call(2, "thread/loaded/list", {})

    def test_rpc_error_wrong_id_notifications_or_nonobject_fail_closed(self):
        for message in [{"id": 2, "error": {"message": "private"}}, {"id": 3, "result": {}},
                        {"method": "turn/started", "params": {}}, [], {"id": 2, "result": []}]:
            rpc = self.rpc(FakeSocket(frame(json.dumps(message))))
            with self.subTest(message=message), self.assertRaises(sync.GuardError):
                rpc.call(2, "thread/loaded/list", {})

    def test_installed_cli_remote_control_status_is_ignored_without_reply(self):
        notification = {"method": "remoteControl/status/changed", "params": {}, "emittedAtMs": 1}
        result = {"data": [], "nextCursor": None}
        rpc = self.rpc(FakeSocket(frame(json.dumps(notification))
                                 + frame(json.dumps({"id": 2, "result": result}))))
        self.assertEqual(rpc.call(2, "thread/loaded/list", {}), result)

    def test_remote_control_request_and_notification_flood_are_refused(self):
        request = {"id": 99, "method": "remoteControl/status/changed", "params": {}}
        notification = {"method": "remoteControl/status/changed", "params": {}}
        for data in [frame(json.dumps(request)), frame(json.dumps(notification)) * sync.MAX_FRAMES]:
            rpc = self.rpc(FakeSocket(data))
            with self.assertRaises(sync.GuardError):
                rpc.call(2, "thread/loaded/list", {})


class PathGuardTests(unittest.TestCase):
    def setUp(self):
        self.acl = patch.object(sync.os, "getxattr", create=True,
                                side_effect=OSError(errno.ENODATA, "No ACL"))
        self.acl_mock = self.acl.start()
        self.addCleanup(self.acl.stop)

    def info(self, *, uid=1003, gid=1003, mode=stat.S_IFREG | 0o700):
        return SimpleNamespace(st_uid=uid, st_gid=gid, st_mode=mode)

    def test_cli_cannot_escape_home_through_symlink(self):
        outside = Path("C:/elsewhere/codex" if sys.platform == "win32" else "/elsewhere/codex")
        with patch.object(Path, "resolve", return_value=outside), self.assertRaises(sync.GuardError):
            sync.checked_path(CLI_PATH, IDENTITY, executable=True)

    def test_wrong_owner_writable_cli_or_public_socket_is_rejected(self):
        for info, control in [(self.info(uid=999), False), (self.info(mode=stat.S_IFREG | 0o722), False),
                              (self.info(mode=stat.S_IFSOCK | 0o666), True)]:
            path = SOCKET_PATH if control else CLI_PATH
            with patch.object(Path, "resolve", return_value=path), patch.object(Path, "stat", return_value=info), \
                    self.assertRaises(sync.GuardError):
                sync.checked_path(path, IDENTITY, executable=not control, control=control)

    def test_owner_private_group_allows_775_parent_and_group_writable_cli(self):
        identity = sync.Identity(HOME, 1003, frozenset({1003}))
        def metadata(path, *_args, **_kwargs):
            return self.info(mode=(stat.S_IFREG | 0o775) if path == CLI_PATH else (stat.S_IFDIR | 0o775))
        with patch.object(Path, "resolve", return_value=CLI_PATH), patch.object(Path, "stat", metadata):
            self.assertEqual(sync.checked_path(CLI_PATH, identity, executable=True), CLI_PATH)

    def test_shared_or_unverified_group_rejects_775_parent(self):
        def metadata(path, *_args, **_kwargs):
            return self.info(mode=(stat.S_IFREG | 0o755) if path == CLI_PATH else (stat.S_IFDIR | 0o775))
        for identity in [IDENTITY, sync.Identity(HOME, 1003, frozenset({999}))]:
            with patch.object(Path, "resolve", return_value=CLI_PATH), patch.object(Path, "stat", metadata), \
                    self.subTest(private_gids=identity.private_gids), \
                    self.assertRaisesRegex(sync.GuardError, "path_parent_group_write_not_private_or_unverified"):
                sync.checked_path(CLI_PATH, identity, executable=True)

    def test_private_group_never_allows_world_write_or_foreign_owner(self):
        identity = sync.Identity(HOME, 1003, frozenset({1003}))
        for info in [self.info(mode=stat.S_IFREG | 0o777), self.info(uid=999, mode=stat.S_IFREG | 0o775)]:
            with patch.object(Path, "resolve", return_value=CLI_PATH), patch.object(Path, "stat", return_value=info), \
                    self.assertRaises(sync.GuardError):
                sync.checked_path(CLI_PATH, identity, executable=True)

    def test_control_socket_remains_private_even_with_verified_private_group(self):
        identity = sync.Identity(HOME, 1003, frozenset({1003}))
        for mode in [0o660, 0o640, 0o620]:
            def metadata(path, *_args, **_kwargs):
                return self.info(mode=(stat.S_IFSOCK | mode) if path == SOCKET_PATH else (stat.S_IFDIR | 0o775))
            with patch.object(Path, "resolve", return_value=SOCKET_PATH), patch.object(Path, "stat", metadata), \
                    patch.object(Path, "is_symlink", return_value=False), self.subTest(mode=mode), \
                    self.assertRaisesRegex(sync.GuardError, "control_socket_type_or_mode_unsafe"):
                sync.checked_path(SOCKET_PATH, identity, control=True)

    def test_private_group_with_acl_or_unreadable_acl_is_not_allowed(self):
        identity = sync.Identity(HOME, 1003, frozenset({1003}))
        for result in [b"named-user-acl", OSError(errno.EACCES, "Unknown ACL")]:
            self.acl_mock.side_effect = result if isinstance(result, Exception) else None
            self.acl_mock.return_value = result
            with patch.object(Path, "resolve", return_value=CLI_PATH), \
                    patch.object(Path, "stat", return_value=self.info(mode=stat.S_IFREG | 0o775)), \
                    self.subTest(result=result), self.assertRaises(sync.GuardError):
                sync.checked_path(CLI_PATH, identity, executable=True)

    def test_private_group_directory_default_acl_is_rejected(self):
        identity = sync.Identity(HOME, 1003, frozenset({1003}))
        def acl(_path, attribute):
            if attribute == "system.posix_acl_default":
                return b"default-acl"
            raise OSError(errno.ENODATA, "No access ACL")
        def metadata(path, *_args, **_kwargs):
            return self.info(mode=(stat.S_IFREG | 0o755) if path == CLI_PATH else (stat.S_IFDIR | 0o775))
        self.acl_mock.side_effect = acl
        with patch.object(Path, "resolve", return_value=CLI_PATH), patch.object(Path, "stat", metadata), \
                self.assertRaisesRegex(sync.GuardError, "has_posix_acl"):
            sync.checked_path(CLI_PATH, identity, executable=True)


class PrivateGroupTests(unittest.TestCase):
    def user(self, name="owner", uid=1003, gid=1003):
        return SimpleNamespace(pw_name=name, pw_uid=uid, pw_gid=gid)

    def group(self, *, gid=1003, names=()):
        return SimpleNamespace(gr_name="owner", gr_gid=gid, gr_mem=list(names))

    def verify(self, users, groups, *, lookup=None):
        by_name = {user.pw_name: user for user in users}
        passwd_db = SimpleNamespace(getpwall=lambda: users, getpwnam=lookup or by_name.__getitem__)
        group_db = SimpleNamespace(getgrall=lambda: groups,
                                  getgrgid=lambda gid: next(group for group in groups if group.gr_gid == gid))
        return sync.verified_private_gids(1003, passwd_db, group_db)

    def test_personal_primary_group_without_explicit_members_is_private(self):
        self.assertEqual(self.verify([self.user()], [self.group()]), frozenset({1003}))

    def test_personal_explicit_group_is_private_when_owner_has_other_primary_group(self):
        self.assertEqual(self.verify([self.user(gid=2000)], [self.group(names=["owner"])]), frozenset({1003}))

    def test_other_users_primary_group_or_explicit_members_make_group_shared(self):
        cases = [([self.user(), self.user("other", uid=1004)], self.group()),
                 ([self.user(), self.user("other", uid=1004, gid=2000)], self.group(names=["other"]))]
        for users, group in cases:
            with self.subTest(users=users):
                self.assertEqual(self.verify(users, [group]), frozenset())

    def test_missing_nss_member_inconsistent_lookup_or_empty_group_is_unverified(self):
        cases = [([self.user()], [self.group(names=["unknown"])], None),
                 ([self.user()], [self.group(names=["owner"])], lambda _: self.user(uid=999)),
                 ([self.user(gid=2000)], [self.group()], None),
                 ([self.user()], [self.group(), self.group()], None)]
        for users, groups, lookup in cases:
            with self.subTest(groups=groups):
                self.assertEqual(self.verify(users, groups, lookup=lookup), frozenset())

    def test_nss_enumeration_failure_or_malformed_metadata_never_authorizes_group_write(self):
        def fail():
            raise OSError("NSS details must not be printed")
        self.assertEqual(sync.verified_private_gids(1003, SimpleNamespace(getpwall=fail), None), frozenset())
        self.assertEqual(self.verify([self.user(uid="1003")], [self.group()]), frozenset())


class DefaultRuntimeSocketTests(unittest.TestCase):
    def setUp(self):
        self.temp_root = Path("/tmp").absolute()
        self.runtime = self.temp_root / "codex-daemon-1003"
        self.target = self.runtime / ("a" * 64)
        self.identity = sync.Identity(HOME, 1003, frozenset({1003}))
        self.overrides = {}
        self.acl = patch.object(sync.os, "getxattr", create=True,
                                side_effect=OSError(errno.ENODATA, "No ACL"))
        self.acl.start()
        self.addCleanup(self.acl.stop)

    def metadata(self, path, *_args, **_kwargs):
        if path in self.overrides:
            info = self.overrides[path]
        elif path == SOCKET_PATH:
            info = SimpleNamespace(st_uid=1003, st_gid=1003, st_mode=stat.S_IFLNK | 0o777)
        elif path == self.temp_root:
            info = SimpleNamespace(st_uid=0, st_gid=0, st_mode=stat.S_IFDIR | 0o1777)
        elif path == self.runtime:
            info = SimpleNamespace(st_uid=1003, st_gid=1003, st_mode=stat.S_IFDIR | 0o700)
        elif path == self.target:
            info = SimpleNamespace(st_uid=1003, st_gid=1003, st_mode=stat.S_IFSOCK | 0o600)
        else:
            info = SimpleNamespace(st_uid=1003, st_gid=1003, st_mode=stat.S_IFDIR | 0o775)
        for key, value in {"st_dev": 1, "st_ino": 2, "st_size": 0, "st_mtime_ns": 100}.items():
            if not hasattr(info, key):
                setattr(info, key, value)
        return info

    def checked(self, *, entry=SOCKET_PATH, resolved=None, executable=False, control=True,
                link_target=None, parent_escape=False):
        def resolve(path, *_args, **_kwargs):
            if parent_escape and path == SOCKET_PATH.parent:
                return self.runtime
            return (resolved or self.target) if path == entry else path
        metadata = lambda path, *args, **kwargs: self.metadata(path, *args, **kwargs)
        with patch.object(Path, "resolve", resolve), patch.object(Path, "stat", metadata), \
                patch.object(Path, "lstat", metadata), \
                patch.object(sync.os, "readlink", return_value=str(link_target if link_target is not None else resolved or self.target)):
            return sync.checked_path(entry, self.identity, executable=executable, control=control)

    def test_official_owned_home_symlink_and_canonical_runtime_path_are_allowed(self):
        self.assertEqual(self.checked(), self.target)
        self.assertEqual(self.checked(entry=self.target), self.target)

    def test_arbitrary_external_target_wrong_uid_runtime_or_invalid_hash_is_rejected(self):
        for target in [self.temp_root / "custom" / ("a" * 64),
                       self.temp_root / "codex-daemon-1004" / ("a" * 64),
                       self.runtime / ("a" * 63), self.runtime / ("A" * 64),
                       self.runtime / "ordinary.sock"]:
            with self.subTest(target=target), self.assertRaises(sync.GuardError):
                self.checked(resolved=target)

    def test_wrong_link_owner_and_group_writable_lexical_parent_are_rejected(self):
        for path, info in [(SOCKET_PATH, SimpleNamespace(st_uid=999, st_gid=999, st_mode=stat.S_IFLNK | 0o777)),
                           (SOCKET_PATH.parent, SimpleNamespace(st_uid=1003, st_gid=999, st_mode=stat.S_IFDIR | 0o775))]:
            self.overrides = {path: info}
            with self.subTest(path=path), self.assertRaises(sync.GuardError):
                self.checked()

    def test_runtime_symlink_foreign_owner_or_non700_directory_are_rejected(self):
        for info in [SimpleNamespace(st_uid=1003, st_gid=1003, st_mode=stat.S_IFLNK | 0o700),
                     SimpleNamespace(st_uid=999, st_gid=999, st_mode=stat.S_IFDIR | 0o700),
                     SimpleNamespace(st_uid=1003, st_gid=1003, st_mode=stat.S_IFDIR | 0o775),
                     SimpleNamespace(st_uid=1003, st_gid=1003, st_mode=stat.S_IFDIR | 0o777)]:
            self.overrides = {self.runtime: info}
            with self.subTest(info=info), self.assertRaises(sync.GuardError):
                self.checked()

    def test_tmp_requires_real_root_owned_1777_directory(self):
        for info in [SimpleNamespace(st_uid=1003, st_gid=1003, st_mode=stat.S_IFDIR | 0o1777),
                     SimpleNamespace(st_uid=0, st_gid=0, st_mode=stat.S_IFDIR | 0o777),
                     SimpleNamespace(st_uid=0, st_gid=0, st_mode=stat.S_IFDIR | 0o700),
                     SimpleNamespace(st_uid=0, st_gid=0, st_mode=stat.S_IFLNK | 0o1777)]:
            self.overrides = {self.temp_root: info}
            with self.subTest(info=info), self.assertRaises(sync.GuardError):
                self.checked()

    def test_runtime_socket_wrong_owner_world_write_wrong_type_or_alias_is_rejected(self):
        for info in [SimpleNamespace(st_uid=999, st_gid=999, st_mode=stat.S_IFSOCK | 0o600),
                     SimpleNamespace(st_uid=1003, st_gid=1003, st_mode=stat.S_IFSOCK | 0o602),
                     SimpleNamespace(st_uid=1003, st_gid=1003, st_mode=stat.S_IFSOCK | 0o660),
                     SimpleNamespace(st_uid=1003, st_gid=1003, st_mode=stat.S_IFREG | 0o600),
                     SimpleNamespace(st_uid=1003, st_gid=1003, st_mode=stat.S_IFLNK | 0o600)]:
            self.overrides = {self.target: info}
            with self.subTest(info=info), self.assertRaises(sync.GuardError):
                self.checked()

    def test_default_runtime_exception_never_applies_to_cli_or_managed_binary(self):
        for entry in [CLI_PATH, MANAGED_PATH, self.target]:
            with self.subTest(entry=entry), self.assertRaises(sync.GuardError):
                self.checked(entry=entry, executable=True, control=False)

    def test_external_alias_to_valid_runtime_is_not_an_allowed_home_link(self):
        with self.assertRaisesRegex(sync.GuardError, "external_control_socket_alias"):
            self.checked(entry=self.temp_root / "arbitrary-alias.sock")

    def test_relative_target_escaping_parent_or_nondefault_home_link_are_rejected(self):
        for kwargs in [{"link_target": "relative-runtime-socket"},
                       {"link_target": str(self.runtime / ".." / self.runtime.name / ("a" * 64))},
                       {"parent_escape": True}, {"entry": HOME / "custom-control.sock"}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(sync.GuardError):
                self.checked(**kwargs)

    def test_home_symlink_replacement_during_validation_is_rejected(self):
        original = self.metadata
        counter = 0
        def changed(path, *args, **kwargs):
            nonlocal counter
            info = original(path, *args, **kwargs)
            if path == SOCKET_PATH:
                counter += 1
                info.st_ino = counter
            return info
        with patch.object(self, "metadata", side_effect=changed), \
                self.assertRaisesRegex(sync.GuardError, "link_changed_during_validation"):
            self.checked()


if __name__ == "__main__":
    unittest.main()
