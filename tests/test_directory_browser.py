"""Directory navigation reads one selected directory, not project/history trees."""
import base64
import io
import json
import tempfile
import unittest
import subprocess
from pathlib import Path
from unittest.mock import patch

from directory_browser import DirectoryBrowser, list_local, _bounded_ssh


class DirectoryBrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        # TEMP may contain an 8.3 alias on Windows; APIs return canonical paths.
        self.root = Path(self.temp.name).resolve()
        (self.root / "B-project").mkdir()
        (self.root / "a-project").mkdir()
        (self.root / "a-project" / "nested-secret-name").mkdir()
        (self.root / "ordinary-file.txt").touch()

    def tearDown(self):
        self.temp.cleanup()

    def test_empty_path_defaults_to_actual_home_not_codex_home(self):
        with patch("directory_browser.Path.home", return_value=self.root):
            result = list_local("")
        self.assertEqual((result["home"], result["path"]), (str(self.root), str(self.root)))
        self.assertEqual([entry["name"] for entry in result["directories"]], ["a-project", "B-project"])
        self.assertNotIn("nested-secret-name", json.dumps(result))
        self.assertNotIn("ordinary-file", json.dumps(result))
        self.assertEqual(result["parent"], str(self.root.parent))
        self.assertTrue(any(entry["path"] == str(self.root) for entry in result["roots"]))

    def test_path_with_shell_metacharacters_is_plain_filesystem_data(self):
        directory = self.root / "project; touch unsafe"
        directory.mkdir()
        result = list_local(str(directory))
        self.assertEqual(result["path"], str(directory))
        with self.assertRaises(ValueError):
            list_local("relative")
        with self.assertRaises(ValueError):
            list_local(str(directory) + "\n")

    def test_listing_is_bounded_and_home_lookup_never_scans(self):
        with patch("directory_browser.MAX_DIRECTORIES", 1):
            result = list_local(str(self.root))
        self.assertEqual(len(result["directories"]), 1)
        self.assertTrue(result["truncated"])
        with patch("directory_browser.os.scandir", side_effect=AssertionError("home only does not scan")):
            self.assertEqual(DirectoryBrowser().home({"transport": "local"}), str(Path.home().resolve()))

    def test_remote_payload_is_base64_argv_not_interpolated_path_or_shell_command(self):
        class Sink(io.BytesIO):
            def close(self):
                self.saved = self.getvalue()
                super().close()
        class Process:
            returncode = 0
            def __init__(self):
                self.stdin = Sink()
                self.stdout = io.BytesIO(b'CEO_HUB_DIRECTORY_RESULT={"ok":true,"result":{"home":"/home/test"}}\n')
                self.stderr = io.BytesIO(b'')
            def wait(self, timeout=None):
                return 0
            def kill(self):
                self.killed = True
        process = Process()
        path = "/tmp/$(touch unsafe);'quoted'"
        with patch("directory_browser.subprocess.Popen", return_value=process) as spawn:
            result = _bounded_ssh("unit-test-host", {"path": path})
        self.assertEqual(result["home"], "/home/test")
        argv = spawn.call_args.args[0]
        self.assertNotIn(path, " ".join(argv))
        payload = argv[-1].split(" ")[-1]
        self.assertEqual(json.loads(base64.urlsafe_b64decode(payload))["path"], path)
        self.assertIn("BatchMode=yes", argv)
        self.assertIn("ConnectTimeout=8", argv)

    def test_ssh_error_output_is_not_exposed(self):
        class Process:
            returncode = 255
            stdin = io.BytesIO()
            stdout = io.BytesIO(b'')
            stderr = io.BytesIO(b'sensitive-model-provider-output')
            def wait(self, timeout=None): return self.returncode
        with patch("directory_browser.subprocess.Popen", return_value=Process()), self.assertRaises(RuntimeError) as error:
            _bounded_ssh("unit-test-host", {"op": "home"})
        self.assertNotIn("sensitive", str(error.exception))

    def test_remote_output_limits_kill_only_the_spawned_ssh_process(self):
        class Process:
            returncode = 0
            def __init__(self):
                self.stdin = io.BytesIO()
                self.stdout = io.BytesIO(b'x' * 600000)
                self.stderr = io.BytesIO(b'')
                self.killed = False
            def wait(self, timeout=None): return 0
            def kill(self): self.killed = True
        process = Process()
        with patch("directory_browser.subprocess.Popen", return_value=process), self.assertRaisesRegex(RuntimeError, "超过限制"):
            _bounded_ssh("unit-test-host", {"path": "/tmp"})
        self.assertTrue(process.killed)

    def test_remote_timeout_is_bounded_and_does_not_include_private_startup_output(self):
        class Process:
            returncode = 255
            def __init__(self):
                self.stdin = io.BytesIO()
                self.stdout = io.BytesIO(b'')
                self.stderr = io.BytesIO(b'PRIVATE_STARTUP_CONTENT')
                self.killed = False
                self.waits = []
            def wait(self, timeout=None):
                self.waits.append(timeout)
                if len(self.waits) == 1: raise subprocess.TimeoutExpired("ssh", timeout)
                return self.returncode
            def kill(self): self.killed = True
        process = Process()
        with patch("directory_browser.subprocess.Popen", return_value=process), self.assertRaisesRegex(RuntimeError, "超时") as error:
            _bounded_ssh("unit-test-host", {"path": "/tmp"})
        self.assertTrue(process.killed)
        self.assertEqual(process.waits, [20, 5])
        self.assertNotIn("PRIVATE", str(error.exception))


if __name__ == "__main__":
    unittest.main()
