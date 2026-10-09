"""Stable checkout anchors across packaged/native contexts; temporary fakes only."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

import app
import mcp_bridge
import terminal_client
from terminal_client import BrokerError, TerminalClient


class ConnectionAnchorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runtime = self.root / ".runtime"
        self.runtime.mkdir()
        self.legacy_service = self.root / "legacy-service.json"
        self.legacy_terminal = self.root / "legacy-terminal.json"
        self.physical_terminal = self.root / "physical-worker.json"
        self.reference = self.runtime / "worker-terminal-reference.json"
        self.patches = [patch.object(app, "ROOT", self.root), patch.object(mcp_bridge, "ROOT", self.root),
                        patch.object(terminal_client, "ROOT", self.root),
                        patch.object(mcp_bridge, "legacy_connection_file", return_value=self.legacy_service),
                        patch.object(terminal_client, "legacy_descriptor_path", return_value=self.legacy_terminal),
                        patch.dict(os.environ, {"CEO_HUB_CONNECTION_FILE": "", "PRON_HUB_CONNECTION_FILE": "",
                                               "CEO_HUB_TERMINAL_FILE": "", "PRON_HUB_CONTROLLER_ID": ""})]
        for fixture in self.patches:
            fixture.start()

    def tearDown(self):
        for fixture in reversed(self.patches):
            fixture.stop()
        self.temp.cleanup()

    def descriptor(self, path=None, **changes):
        target = path or self.physical_terminal
        target.write_text(json.dumps({"url": "http://127.0.0.1:39001", "token": "fixture-capability-not-real", "pid": 123,
                                      "version": "0.3.0", **changes}), encoding="utf-8")
        return target

    def pin(self, value=None):
        self.reference.write_text(json.dumps(value or {"descriptor_file": str(self.physical_terminal)}), encoding="utf-8")

    def test_web_default_is_checkout_anchor_regardless_of_user_appdata(self):
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.root / "native-context")}):
            one = app.connection_file()
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.root / "packaged-context")}):
            two = app.connection_file()
        self.assertEqual(one, self.runtime / "service.json")
        self.assertEqual(one, two)
        with patch.dict(os.environ, {"CEO_HUB_CONNECTION_FILE": str(self.legacy_service)}):
            self.assertEqual(app.connection_file(), self.legacy_service)

    def test_bridge_prefers_existing_canonical_anchor_and_falls_back_only_when_absent(self):
        self.assertEqual(mcp_bridge.connection_file(), self.legacy_service)
        canonical = self.runtime / "service.json"
        canonical.write_text("malformed-not-a-valid-service", encoding="utf-8")
        self.assertEqual(mcp_bridge.connection_file(), canonical)
        with self.assertRaises(mcp_bridge.BridgeError):
            mcp_bridge.read_descriptor()
        # A bad authoritative anchor must not silently use a stale legacy token.
        self.descriptor(self.legacy_service)
        with self.assertRaises(mcp_bridge.BridgeError):
            mcp_bridge.read_descriptor()

    def test_explicit_scoped_bridge_descriptor_retains_priority_over_global_anchor(self):
        canonical = self.runtime / "service.json"
        self.descriptor(canonical)
        self.descriptor(self.legacy_service)
        scoped = self.root / "scoped.json"
        self.descriptor(scoped)
        with patch.dict(os.environ, {"CEO_HUB_CONNECTION_FILE": str(self.legacy_service), "PRON_HUB_CONNECTION_FILE": str(scoped)}):
            self.assertEqual(mcp_bridge.connection_file(), scoped)
        with patch.dict(os.environ, {"CEO_HUB_CONNECTION_FILE": str(self.legacy_service)}):
            self.assertEqual(mcp_bridge.connection_file(), self.legacy_service)

    def test_worker_pin_selects_existing_physical_owner_and_preserves_wire_03(self):
        self.descriptor()
        self.pin()
        self.assertEqual(terminal_client.descriptor_path(), self.physical_terminal)
        client = TerminalClient()
        self.assertEqual(client.descriptor()["pid"], 123)
        with patch.object(client, "request", return_value={"pid": 123, "version": "0.3.0"}), \
             patch("terminal_client.subprocess.Popen", side_effect=AssertionError("must not restart original broker")):
            client.ensure_started()
        self.assertEqual(set(json.loads(self.reference.read_text())), {"descriptor_file"})
        self.assertNotIn("fixture-capability-not-real", self.reference.read_text())

    def test_explicit_worker_override_and_custom_manager_descriptor_do_not_use_worker_reference(self):
        self.reference.write_text("malformed", encoding="utf-8")
        custom = self.root / "manager-owned.json"
        self.descriptor(custom)
        self.descriptor(self.legacy_terminal)
        with patch.dict(os.environ, {"CEO_HUB_TERMINAL_FILE": str(self.legacy_terminal)}):
            self.assertEqual(terminal_client.descriptor_path(), self.legacy_terminal)
        self.assertEqual(TerminalClient(descriptor_file=custom).descriptor()["pid"], 123)

    def test_bad_missing_relative_remote_or_oversized_pin_cannot_fallback_or_launch(self):
        self.descriptor(self.legacy_terminal)
        client = TerminalClient()
        values = [{}, {"descriptor_file": "relative.json"}, {"descriptor_file": str(self.root / "missing.json")},
                  {"descriptor_file": "https://127.0.0.1:3/descriptor"}, {"descriptor_file": "\\\\remote\\share\\descriptor.json"},
                  {"descriptor_file": str(self.physical_terminal) + "\0"}, {"descriptor_file": str(self.physical_terminal), "token": "not-a-capability"}]
        for value in values:
            self.reference.write_text(json.dumps(value), encoding="utf-8")
            with self.subTest(value=list(value)):
                with self.assertRaises(ValueError):
                    terminal_client.descriptor_path()
                with patch("terminal_client.subprocess.Popen", side_effect=AssertionError("no alternate owner")):
                    self.assertEqual(client.sessions(), [])
                    with self.assertRaises(BrokerError) as error:
                        client.create({"cwd": str(self.root)})
                    self.assertEqual(error.exception.code, "bound_transport_unavailable")
        self.reference.write_bytes(b" " * 8193)
        with self.assertRaises(ValueError):
            terminal_client.descriptor_path()

    def test_valid_reference_with_unhealthy_or_invalid_target_never_starts_second_owner(self):
        self.descriptor()
        self.pin()
        client = TerminalClient()
        for changes in ({"url": "http://other.example:3"}, {"token": "bad\ncredential"}, {}):
            self.descriptor(**changes)
            with patch.object(client, "request", side_effect=ConnectionError("bound owner unavailable")), \
                 patch("terminal_client.subprocess.Popen", side_effect=AssertionError("no second owner")):
                with self.assertRaises(BrokerError) as error:
                    client.create({"cwd": str(self.root)})
                self.assertEqual(error.exception.code, "bound_transport_unavailable")

    def test_without_pin_old_default_is_retained_and_launch_explicitly_pins_child_path(self):
        self.assertEqual(terminal_client.descriptor_path(), self.legacy_terminal)
        self.descriptor(self.legacy_terminal)
        client = TerminalClient(runtime=self.runtime)
        process = Mock()
        process.poll.return_value = None
        with patch.object(client, "running", side_effect=[False, True]), \
             patch("terminal_client.shutil.which", return_value="fake-node"), \
             patch.object(Path, "exists", return_value=True), \
             patch("terminal_client.subprocess.Popen", return_value=process) as launch:
            client.ensure_started()
        self.assertEqual(launch.call_args.kwargs["env"]["CEO_HUB_TERMINAL_FILE"], str(self.legacy_terminal.resolve()))
        self.assertEqual(os.environ["CEO_HUB_TERMINAL_FILE"], "")
        self.assertEqual(json.loads(self.reference.read_text())["descriptor_file"], str(self.legacy_terminal.resolve()))

    def test_verified_default_reuse_exclusively_pins_owner_but_metadata_does_not(self):
        self.descriptor(self.legacy_terminal)
        client = TerminalClient()
        with patch.object(client, "request", return_value={"pid": 123, "version": "0.3.0"}):
            self.assertTrue(client.running())
            self.assertFalse(self.reference.exists())
            client.ensure_started()
        self.assertEqual(json.loads(self.reference.read_text()), {"descriptor_file": str(self.legacy_terminal.resolve())})
        self.assertNotIn("fixture-capability-not-real", self.reference.read_text())

    def test_default_reuse_never_overwrites_competing_or_malformed_reference(self):
        self.descriptor(self.legacy_terminal)
        self.descriptor()
        client = TerminalClient()
        def competing(*args, **kwargs):
            self.pin()
            return True
        with patch.object(client, "running", side_effect=competing), \
             patch("terminal_client.subprocess.Popen", side_effect=AssertionError("no replacement")):
            with self.assertRaises(BrokerError) as error:
                client.ensure_started()
            self.assertEqual(error.exception.code, "bound_transport_unavailable")
        self.assertEqual(json.loads(self.reference.read_text())["descriptor_file"], str(self.physical_terminal))
        self.reference.write_text("malformed-owned-by-someone-else", encoding="utf-8")
        with self.assertRaises(BrokerError):
            client.ensure_started()
        self.assertEqual(self.reference.read_text(), "malformed-owned-by-someone-else")

    def test_custom_manager_start_never_writes_worker_reference(self):
        manager = self.root / "manager.json"
        self.descriptor(manager)
        client = TerminalClient(descriptor_file=manager, runtime=self.runtime / "manager-logs")
        with patch.object(client, "running", return_value=True):
            client.ensure_started()
        self.assertFalse(self.reference.exists())


if __name__ == "__main__":
    unittest.main()
