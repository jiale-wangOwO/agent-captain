"""Offline broker identity contracts across the source-layout migration.

Only temporary descriptors and mocked kernel observations are used. No live
process, terminal, SSH route, personal configuration, or service is inspected.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_captain.transport import terminal_client


class BrokerLayoutIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="agent-captain-owner-layout-")
        self.root = Path(self.temporary.name).resolve()
        self.root_patch = patch.object(terminal_client, "ROOT", self.root)
        self.root_patch.start()
        self.executable = str(Path(sys.executable).with_name("node.exe" if os.name == "nt" else "node"))
        self.canonical = self.root / "terminal" / "terminal_broker.cjs"
        self.legacy = self.root / "terminal_broker.cjs"
        self.client = terminal_client.TerminalClient(runtime=self.root / "unused-runtime")

    def tearDown(self):
        self.root_patch.stop()
        self.temporary.cleanup()

    def identity(self, script, *, birth="win-filetime:133700000000000001"):
        return {"birth": birth, "executable": self.executable, "script": str(script)}

    def descriptor(self, identity):
        filename = self.root / "fixture-descriptor.json"
        filename.write_text(json.dumps({
            "url": "http://127.0.0.1:39001", "token": "inert-layout-fixture-capability",
            "pid": 123, "version": "0.3.0", "owner_identity": identity,
        }), encoding="utf-8")
        return filename

    def test_default_launch_uses_canonical_source_and_both_exact_entries_remain_valid(self):
        self.assertEqual(terminal_client.broker_script(), self.canonical)
        for script in (self.canonical, self.legacy):
            with self.subTest(script=script):
                identity = self.identity(script)
                self.assertEqual(terminal_client.broker_script(identity), script)
                descriptor = self.client._read_descriptor_file(self.descriptor(identity))
                self.assertEqual(descriptor["owner_identity"], identity)
                owner = {"pid": 123, "owner_identity": identity}
                self.assertEqual(self.client._lock_owner(json.dumps(owner).encode()), owner)

    def test_same_basename_outside_two_exact_entries_is_not_a_broker_identity(self):
        for script in (self.root / "unrelated" / "terminal_broker.cjs",
                       self.root.parent / "terminal_broker.cjs", Path("terminal_broker.cjs"),
                       self.root / "terminal" / "other.cjs"):
            with self.subTest(script=script):
                identity = self.identity(script)
                with self.assertRaises(ValueError):
                    terminal_client.broker_script(identity)
                with self.assertRaises(ValueError):
                    self.client._read_descriptor_file(self.descriptor(identity))
                with self.assertRaises(ValueError):
                    self.client._lock_owner(json.dumps({"pid": 123, "owner_identity": identity}).encode())

    def test_explicit_null_or_malformed_present_owner_identity_is_not_legacy_absence(self):
        for identity in (None, {}, self.identity(self.canonical, birth="not-a-kernel-birth"),
                         {**self.identity(self.canonical), "extra": "not-allowed"},
                         {**self.identity(self.canonical), "executable": "node"}):
            with self.subTest(identity=identity):
                with self.assertRaises(ValueError):
                    self.client._read_descriptor_file(self.descriptor(identity))

    def test_both_layouts_preserve_birth_and_executable_checks(self):
        for script in (self.canonical, self.legacy):
            identity = self.identity(script)
            owner = {"pid": 123, "owner_identity": identity}
            for observation, expected in (
                ({"state": "alive", "birth": identity["birth"], "executable": self.executable}, "alive"),
                ({"state": "alive", "birth": "win-filetime:133700000000000002", "executable": self.executable}, "dead"),
                ({"state": "alive", "birth": identity["birth"], "executable": str(self.root / "other" / Path(self.executable).name)}, "unknown"),
                ({"state": "unknown"}, "unknown"),
            ):
                with self.subTest(script=script, observation=observation), \
                        patch.object(self.client, "_pid_state", return_value="alive"), \
                        patch.object(terminal_client, "observe_process", return_value=observation) as observe:
                    self.assertEqual(self.client._owner_state(owner), expected)
                    observe.assert_called_once_with(123)

    def test_dead_or_unknown_os_result_never_triggers_second_process_probe(self):
        for state in ("dead", "unknown"):
            with self.subTest(state=state), patch.object(self.client, "_pid_state", return_value=state), \
                    patch.object(terminal_client, "observe_process", side_effect=AssertionError("no second probe")):
                self.assertEqual(self.client._owner_state({"pid": 123, "owner_identity": self.identity(self.legacy)}), state)


if __name__ == "__main__":
    unittest.main()
