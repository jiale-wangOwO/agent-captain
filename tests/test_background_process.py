"""Private launch plans and fail-closed background startup (no real services)."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import background_process as background
import service


class BackgroundProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()

    def tearDown(self):
        self.temp.cleanup()

    def arguments(self):
        return {"argv": [sys.executable, "-c", "pass"], "cwd": self.root,
                "stdout_path": self.root / "out.log", "stderr_path": self.root / "err.log"}

    def test_environment_contains_no_debug_injection_or_api_credentials(self):
        source = {"PATH": "expected", "CEO_HUB_TERMINAL_FILE": str(self.root / "terminal.json"),
                  "CEO_HUB_RECOVERY_GUARD": "private-one-shot-guard", "NODE_OPTIONS": "--require unexpected",
                  "NODE_INSPECT_RESUME_ON_START": "1", "OPENAI_API_KEY": "synthetic-secret",
                  "GH_TOKEN": "synthetic-token", "CODEX_HOME": "unexpected"}
        result = background.background_environment(source)
        self.assertEqual(set(result), {"PATH", "CEO_HUB_TERMINAL_FILE", "CEO_HUB_RECOVERY_GUARD"})
        self.assertEqual(source["GH_TOKEN"], "synthetic-token")

    def test_windows_native_ssh_platform_environment_is_preserved(self):
        source = {"ProgramData": "C:/ProgramData", "SystemDrive": "C:", "ProgramFiles": "C:/Program Files",
                  "ALLUSERSPROFILE": "C:/ProgramData", "ComSpec": "C:/Windows/System32/cmd.exe",
                  "SystemRoot": "C:/Windows", "OPENAI_API_KEY": "synthetic-secret", "NODE_OPTIONS": "--inspect"}
        result = background.background_environment(source)
        self.assertEqual(result["ProgramData"], source["ProgramData"])
        self.assertEqual(result["SystemDrive"], source["SystemDrive"])
        self.assertEqual(result["ProgramFiles"], source["ProgramFiles"])
        self.assertEqual(result["SystemRoot"], source["SystemRoot"])
        self.assertNotIn("OPENAI_API_KEY", result)
        self.assertNotIn("NODE_OPTIONS", result)

    def test_explicit_unapproved_environment_is_rejected_before_launch(self):
        with patch.object(background.subprocess, "Popen", side_effect=AssertionError("must not spawn")):
            with self.assertRaises(ValueError):
                background.launch_background(**self.arguments(), env_overrides={"NODE_OPTIONS": "debug"})

    def test_relative_or_remote_executable_is_rejected(self):
        for executable in ("python", "//server/share/python.exe"):
            with self.subTest(executable=executable), self.assertRaises(ValueError):
                background._validated([executable], self.root, self.root / "out", self.root / "err", {})

    def test_namespace_and_identity_python_overrides_require_absolute_local_paths(self):
        for key in ("CEO_HUB_TERMINAL_FILE", "AGENTCAPTAIN_CONNECTION_FILE", "PRON_HUB_CONNECTION_FILE", "CEO_HUB_CONNECTION_FILE", "CEO_HUB_IDENTITY_PYTHON"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                background._validated([sys.executable], self.root, self.root / "out", self.root / "err", {key: "relative"})

    def test_validation_does_not_modify_parent_environment(self):
        before = dict(os.environ)
        _, _, _, _, environment = background._validated([sys.executable], self.root, self.root / "out", self.root / "err",
                                                        {"CEO_HUB_RECOVERY_GUARD": "synthetic"})
        self.assertEqual(environment["CEO_HUB_RECOVERY_GUARD"], "synthetic")
        self.assertEqual(dict(os.environ), before)

    def test_new_and_legacy_connection_aliases_survive_external_launch(self):
        for key in ("AGENTCAPTAIN_CONNECTION_FILE", "PRON_HUB_CONNECTION_FILE", "CEO_HUB_CONNECTION_FILE"):
            with self.subTest(key=key):
                source = {key: str(self.root / "service.json"), "GH_TOKEN": "synthetic-token"}
                self.assertEqual(background.background_environment(source), {key: source[key]})
                result = background._validated([sys.executable], self.root, self.root / "out", self.root / "err", {key: source[key]})
                self.assertEqual(result[-1][key], source[key])

    def test_record_is_atomic_exclusive_and_never_replaces_receipt(self):
        path = self.root / "receipt.json"
        background._write_json(path, {"state": "first"})
        with self.assertRaises(FileExistsError):
            background._write_json(path, {"state": "replacement"})
        self.assertEqual(background._read_json(path), {"state": "first"})
        self.assertEqual([x.name for x in self.root.iterdir()], ["receipt.json"])

    def test_oversized_record_is_rejected(self):
        path = self.root / "plan.json"
        path.write_bytes(b" " * (background.MAX_PLAN + 1))
        with self.assertRaises(ValueError):
            background._read_json(path)

    def plan(self, content):
        directory = self.root / ("launch-" + "1" * 32)
        directory.mkdir()
        path = directory / "plan.json"
        path.write_text(content, encoding="utf-8")
        return directory, path

    def test_duplicate_wrapper_cannot_delete_plan_or_publish_failure(self):
        directory, path = self.plan("not consumed by loser")
        (directory / "claimed").write_text("winner", encoding="ascii")
        self.assertEqual(background._run_plan(path), 1)
        self.assertTrue(path.exists())
        self.assertFalse((directory / "receipt.json").exists())

    def test_bad_json_is_cleaned_and_failure_is_recorded_without_secret_contents(self):
        directory, path = self.plan("synthetic-secret-not-json")
        self.assertEqual(background._run_plan(path), 1)
        self.assertFalse(path.exists())
        self.assertEqual(background._read_json(directory / "receipt.json")["state"], "failed")
        self.assertNotIn("synthetic-secret", (directory / "lifecycle.jsonl").read_text())

    def test_expired_plan_never_spawns_and_is_consumed(self):
        directory, path = self.plan(json.dumps({"schema": 1, "launch_id": "1" * 32, "expires_at": time.time() - 1}))
        with patch.object(background.subprocess, "Popen", side_effect=AssertionError("expired plan must not spawn")):
            self.assertEqual(background._run_plan(path), 1)
        self.assertFalse(path.exists())
        self.assertEqual(background._read_json(directory / "receipt.json")["error"], "expired_or_invalid_plan")

    @unittest.skipUnless(os.name == "nt", "Windows external monitor only")
    def test_post_spawn_identity_error_is_unknown_and_monitor_keeps_observing_child(self):
        plan = {"schema": 1, "launch_id": "1" * 32, "expires_at": time.time() + 20,
                "argv": [sys.executable, "-c", "pass"], "cwd": str(self.root),
                "stdout": str(self.root / "out"), "stderr": str(self.root / "err"), "environment": {}}
        directory, path = self.plan(json.dumps(plan))
        probe, child = Mock(), Mock(pid=123)
        probe.wait.return_value = child.wait.return_value = 0
        original_read = background._read_json
        def read(record):
            return {"job_flags": 0, "in_job": False} if record.name == "route-probe.json" else original_read(record)
        with patch.object(background, "process_job_state", return_value={"in_job": False, "job_flags": 0}), \
                patch.object(background, "_read_json", side_effect=read), \
                patch.object(background.subprocess, "Popen", side_effect=[probe, child]), \
                patch.object(background, "_creation_time", return_value=123456), \
                patch.object(background, "observe_process", return_value={"state": "unknown"}):
            self.assertEqual(background._run_plan(path), 1)
        receipt = original_read(directory / "receipt.json")
        self.assertEqual((receipt["state"], receipt["pid"], receipt["birth"]), ("unknown", 123, "win-filetime:123456"))
        child.terminate.assert_not_called()
        child.wait.assert_called_once_with()
        self.assertIn("child_launch_unconfirmed", (directory / "lifecycle.jsonl").read_text())

    @unittest.skipUnless(os.name == "nt", "Windows WMI route only")
    def test_wmi_failure_removes_one_shot_plan_without_popen_fallback(self):
        with patch.object(background, "_wmi_create", side_effect=RuntimeError("synthetic WMI failure")), \
                patch.object(background.subprocess, "Popen", side_effect=AssertionError("no fallback")):
            with self.assertRaises(background.BackgroundLaunchUnknown) as unknown:
                background.launch_background(**self.arguments())
        self.assertNotIn("synthetic WMI failure", str(unknown.exception))
        self.assertTrue(unknown.exception.launch_id)
        self.assertEqual(unknown.exception.receipt_path.name, "receipt.json")
        directories = list((self.root / ".runtime/background-launches").iterdir())
        self.assertEqual(len(directories), 1)
        self.assertFalse((directories[0] / "plan.json").exists())
        self.assertEqual(json.loads((directories[0] / "lifecycle.jsonl").read_text())["event"], "wmi_launch_unconfirmed")

    @unittest.skipUnless(os.name == "nt", "Windows WMI route only")
    def test_unknown_launch_does_not_retry_or_start_fallback(self):
        with patch.object(background, "_wmi_create", return_value=123) as create, \
                patch.object(background.subprocess, "Popen", side_effect=AssertionError("no fallback")):
            with self.assertRaisesRegex(RuntimeError, "outcome unknown"):
                background.launch_background(**self.arguments(), startup_timeout=0)
        create.assert_called_once()
        self.assertFalse(any((self.root / ".runtime/background-launches").glob("*/plan.json")))

    def test_posix_route_keeps_new_session_and_filtered_environment(self):
        process = Mock()
        with patch.object(background.os, "name", "posix"), patch.object(background.subprocess, "Popen", return_value=process) as spawn:
            # All pathlib objects are prepared before patching os.name.
            args = self.arguments()
            with patch.object(background, "_validated", return_value=(args["argv"], self.root, args["stdout_path"], args["stderr_path"], {"PATH": "safe"})):
                result = background.launch_background(**args)
        self.assertIs(result, process)
        self.assertTrue(spawn.call_args.kwargs["start_new_session"])
        self.assertEqual(spawn.call_args.kwargs["env"], {"PATH": "safe"})


class ServiceLauncherTests(unittest.TestCase):
    def test_healthy_service_never_spawns(self):
        current = {"running": True, "pid": 123, "version": service.VERSION}
        with patch.object(service, "status", return_value=current), patch.object(service, "launch_background", side_effect=AssertionError("already running")):
            self.assertEqual(service.start(), current)

    def test_new_service_uses_background_api_and_matches_actual_child_pid(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            process = Mock(pid=123)
            process.poll.return_value = None
            wrong = {"running": True, "pid": 456, "version": service.VERSION}
            expected = {"running": True, "pid": 123, "version": service.VERSION}
            with patch.object(service, "ROOT", root), patch.object(service, "status", side_effect=[{"running": False}, wrong, expected]), \
                    patch.object(service, "launch_background", return_value=process) as spawn, patch.object(service.time, "sleep"):
                self.assertEqual(service.start(), expected)
            self.assertEqual(spawn.call_args.kwargs["cwd"], root)
            self.assertEqual(spawn.call_args.kwargs["stderr_path"], root / ".runtime/server.err.log")


if __name__ == "__main__":
    unittest.main()
