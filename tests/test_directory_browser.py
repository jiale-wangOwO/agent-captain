"""Directory navigation reads one selected directory, not project/history trees."""
import base64
import errno
import io
import json
import os
import tempfile
import unittest
import subprocess
import sys
import threading
import time
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
            def __init__(self):
                self.stdin = io.BytesIO()
                self.stdout = io.BytesIO(b'')
                self.stderr = io.BytesIO(b'sensitive-model-provider-output')
                self.waits = []
            def wait(self, timeout=None):
                self.waits.append(timeout)
                return self.returncode
        process = Process()
        with patch("directory_browser.subprocess.Popen", return_value=process), self.assertRaises(RuntimeError) as error:
            _bounded_ssh("unit-test-host", {"op": "home"})
        self.assertNotIn("sensitive", str(error.exception))
        self.assertIn("SSH", str(error.exception))
        self.assertEqual(process.waits, [20])
        self.assertTrue(all(stream.closed for stream in (process.stdin, process.stdout, process.stderr)))

    def test_ssh_spawn_error_is_safe_and_does_not_retry(self):
        with patch("directory_browser.subprocess.Popen", side_effect=OSError("PRIVATE_STARTUP_CONTENT")) as spawn, self.assertRaises(RuntimeError) as error:
            _bounded_ssh("unit-test-host", {"op": "home"})
        self.assertIn("SSH", str(error.exception))
        self.assertNotIn("PRIVATE", str(error.exception))
        spawn.assert_called_once()

    def test_early_write_and_close_pipe_errors_are_safe_and_reap_child(self):
        pipe_errors = [BrokenPipeError(errno.EPIPE, "PRIVATE_WRITE_CONTENT")]
        for winerror in (109, 232):
            pipe_error = OSError(errno.EINVAL, "PRIVATE_WINDOWS_PIPE_CONTENT")
            pipe_error.winerror = winerror
            pipe_errors.append(pipe_error)
        for phase in ("write", "close"):
            for pipe_error in pipe_errors:
                with self.subTest(phase=phase, exception=type(pipe_error).__name__, winerror=getattr(pipe_error, "winerror", None)):
                    class Sink(io.BytesIO):
                        def write(self, value):
                            if phase == "write":
                                raise pipe_error
                            return super().write(value)
                        def close(self):
                            super().close()
                            if phase == "close":
                                raise pipe_error
                    class Process:
                        returncode = 255
                        def __init__(self):
                            self.stdin = Sink()
                            self.stdout = io.BytesIO(b'')
                            self.stderr = io.BytesIO(b'PRIVATE_PROVIDER_TOKEN_CONTENT')
                            self.killed = False
                            self.waits = []
                        def wait(self, timeout=None):
                            self.waits.append(timeout)
                            return self.returncode
                        def kill(self): self.killed = True
                    process = Process()
                    with patch("directory_browser.subprocess.Popen", return_value=process) as spawn, self.assertRaises(RuntimeError) as error:
                        _bounded_ssh("unit-test-host", {"op": "home"})
                    self.assertIn("远端目录", str(error.exception))
                    self.assertIn("SSH", str(error.exception))
                    self.assertNotIn("PRIVATE", str(error.exception))
                    self.assertNotIn("Broken pipe", str(error.exception))
                    self.assertTrue(process.killed)
                    self.assertEqual(process.waits, [5])
                    self.assertTrue(all(stream.closed for stream in (process.stdin, process.stdout, process.stderr)))
                    spawn.assert_called_once()

    def test_pipe_error_cleanup_failures_do_not_replace_safe_error(self):
        for cleanup_error in (subprocess.TimeoutExpired("PRIVATE_COMMAND", 5), OSError("PRIVATE_WAIT_CONTENT")):
            with self.subTest(exception=type(cleanup_error).__name__):
                class Sink(io.BytesIO):
                    def write(self, value): raise BrokenPipeError("PRIVATE_PIPE_CONTENT")
                class Process:
                    returncode = None
                    def __init__(self):
                        self.stdin = Sink()
                        self.stdout = io.BytesIO(b'')
                        self.stderr = io.BytesIO(b'PRIVATE_PROVIDER_CONTENT')
                        self.waits = []
                    def wait(self, timeout=None):
                        self.waits.append(timeout)
                        raise cleanup_error
                    def kill(self): raise OSError("PRIVATE_KILL_CONTENT")
                process = Process()
                with patch("directory_browser.subprocess.Popen", return_value=process), self.assertRaises(RuntimeError) as error:
                    _bounded_ssh("unit-test-host", {"op": "home"})
                self.assertIn("SSH", str(error.exception))
                self.assertNotIn("PRIVATE", str(error.exception))
                self.assertEqual(process.waits, [5])
                self.assertTrue(all(stream.closed for stream in (process.stdin, process.stdout, process.stderr)))

    def test_failed_buffered_input_cleanup_does_not_flush_remaining_bytes(self):
        class RawSink(io.RawIOBase):
            def writable(self): return True
            def write(self, value):
                # Retained pending bytes must not be retried after a failed
                # stdin write: on a full live SSH pipe this call could block.
                raise AssertionError("cleanup retried the failed stdin flush")
        class Sink(io.BufferedWriter):
            def write(self, value):
                super().write(b'pending bytes')
                raise BrokenPipeError("PRIVATE_PIPE_CONTENT")
        class Process:
            returncode = None
            def __init__(self):
                self.stdin = Sink(RawSink())
                self.stdout = io.BytesIO(b'')
                self.stderr = io.BytesIO(b'PRIVATE_PROVIDER_CONTENT')
            def wait(self, timeout=None): raise subprocess.TimeoutExpired("PRIVATE_COMMAND", timeout)
            def kill(self): raise OSError("PRIVATE_KILL_CONTENT")
        process = Process()
        with patch("directory_browser.subprocess.Popen", return_value=process), self.assertRaisesRegex(RuntimeError, "SSH") as error:
            _bounded_ssh("unit-test-host", {"op": "home"})
        self.assertNotIn("PRIVATE", str(error.exception))
        self.assertTrue(all(stream.closed for stream in (process.stdin, process.stdout, process.stderr)))

    def test_retained_os_pipe_writers_do_not_block_request_cleanup(self):
        # A surviving writer keeps BufferedReader.read blocked even though the
        # query cannot kill/reap its child. The main query must return after
        # bounded joins, and the readers own closing their streams after EOF.
        real_thread = threading.Thread
        writers, readers, errors = [], [], []
        def retained_pipe():
            read_fd, write_fd = os.pipe()
            writers.append(os.fdopen(write_fd, "wb", buffering=0))
            return io.BufferedReader(os.fdopen(read_fd, "rb", buffering=0))
        class Sink(io.BytesIO):
            def write(self, value): raise BrokenPipeError("PRIVATE_PIPE_CONTENT")
        class Process:
            returncode = None
            def __init__(self):
                self.stdin = Sink()
                self.stdout = retained_pipe()
                self.stderr = retained_pipe()
                self.waits = []
            def wait(self, timeout=None):
                self.waits.append(timeout)
                raise subprocess.TimeoutExpired("PRIVATE_COMMAND", timeout)
            def kill(self): raise OSError("PRIVATE_KILL_CONTENT")
        process = Process()
        def reader(*args, **kwargs):
            thread = real_thread(*args, **kwargs)
            readers.append(thread)
            return thread
        completed = threading.Event()
        def query():
            try:
                _bounded_ssh("unit-test-host", {"op": "home"})
            except Exception as error:
                errors.append(error)
            finally:
                completed.set()
        query_thread = real_thread(target=query, daemon=True)
        started = time.monotonic()
        try:
            with patch("directory_browser.subprocess.Popen", return_value=process), patch("directory_browser.threading.Thread", side_effect=reader):
                query_thread.start()
                finished_before_eof = completed.wait(timeout=4)
            elapsed = time.monotonic() - started
            self.assertTrue(finished_before_eof, "query blocked closing a reader instead of returning after bounded joins")
            self.assertLess(elapsed, 4)
            self.assertEqual(process.waits, [5])
            self.assertEqual(len(readers), 2)
            self.assertTrue(all(thread.is_alive() for thread in readers))
            self.assertFalse(process.stdout.closed)
            self.assertFalse(process.stderr.closed)
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], RuntimeError)
            self.assertIn("SSH", str(errors[0]))
            self.assertNotIn("PRIVATE", str(errors[0]))
        finally:
            # Release the real OS writers even on a failed assertion; this
            # unblocks both readers and lets their own finally close the pipe.
            for writer in writers:
                writer.close()
            query_thread.join(timeout=3)
            for thread in readers:
                thread.join(timeout=3)
        self.assertFalse(query_thread.is_alive())
        self.assertTrue(all(not thread.is_alive() for thread in readers))
        self.assertTrue(all(stream.closed for stream in (process.stdin, process.stdout, process.stderr)))

    def test_reader_pipe_error_is_safe_and_stops_this_child(self):
        class Source(io.BytesIO):
            def read(self, size=-1): raise OSError("PRIVATE_READER_CONTENT")
        class Process:
            returncode = 255
            def __init__(self):
                self.stdin = io.BytesIO()
                self.stdout = Source()
                self.stderr = io.BytesIO(b'PRIVATE_PROVIDER_CONTENT')
                self.killed = False
            def wait(self, timeout=None): return self.returncode
            def kill(self): self.killed = True
        process = Process()
        with patch("directory_browser.subprocess.Popen", return_value=process), self.assertRaises(RuntimeError) as error:
            _bounded_ssh("unit-test-host", {"op": "home"})
        self.assertNotIn("PRIVATE", str(error.exception))
        self.assertTrue(process.killed)
        self.assertTrue(all(stream.closed for stream in (process.stdin, process.stdout, process.stderr)))

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

    def test_output_limit_takes_precedence_over_pipe_error_after_child_exit(self):
        class Sink(io.BytesIO):
            def write(self, value): raise BrokenPipeError("PRIVATE_PIPE_CONTENT")
        class Process:
            returncode = 255
            def __init__(self):
                self.stdin = Sink()
                self.stdout = io.BytesIO(b'x' * 600000)
                self.stderr = io.BytesIO(b'PRIVATE_PROVIDER_CONTENT')
                self.killed = False
                self.waits = []
            def wait(self, timeout=None):
                self.waits.append(timeout)
                return self.returncode
            def kill(self): self.killed = True
        process = Process()
        with patch("directory_browser.subprocess.Popen", return_value=process), self.assertRaisesRegex(RuntimeError, "超过限制") as error:
            _bounded_ssh("unit-test-host", {"path": "/tmp"})
        self.assertTrue(process.killed)
        self.assertEqual(process.waits, [5])
        self.assertNotIn("PRIVATE", str(error.exception))
        self.assertTrue(all(stream.closed for stream in (process.stdin, process.stdout, process.stderr)))

    def test_repeated_early_exited_children_leave_no_readers_or_open_streams(self):
        # Exercise real OS pipes without SSH/network. Each substituted child is
        # already gone before the parent writes the directory helper script.
        real_spawn, real_thread = subprocess.Popen, threading.Thread
        processes, readers = [], []
        def spawn(*args, **kwargs):
            process = real_spawn([sys.executable, "-c", "import sys; sys.stderr.write('PRIVATE_PROVIDER_CONTENT'); sys.exit(1)"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            process.wait(timeout=5)
            processes.append(process)
            return process
        def reader(*args, **kwargs):
            thread = real_thread(*args, **kwargs)
            readers.append(thread)
            return thread
        with patch("directory_browser.subprocess.Popen", side_effect=spawn) as substituted, patch("directory_browser.threading.Thread", side_effect=reader):
            for _ in range(4):
                with self.assertRaisesRegex(RuntimeError, "SSH") as error:
                    _bounded_ssh("unit-test-host", {"op": "home"})
                self.assertNotIn("PRIVATE", str(error.exception))
        self.assertEqual(substituted.call_count, 4)
        self.assertEqual(len(readers), 8)
        self.assertTrue(all(not thread.is_alive() for thread in readers))
        self.assertTrue(all(process.returncode is not None for process in processes))
        self.assertTrue(all(stream.closed for process in processes for stream in (process.stdin, process.stdout, process.stderr)))

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
        self.assertTrue(all(stream.closed for stream in (process.stdin, process.stdout, process.stderr)))

    def test_remote_timeout_cleanup_timeout_does_not_leak_command(self):
        class Process:
            returncode = None
            def __init__(self):
                self.stdin = io.BytesIO()
                self.stdout = io.BytesIO(b'')
                self.stderr = io.BytesIO(b'PRIVATE_STARTUP_CONTENT')
                self.killed = False
                self.waits = []
            def wait(self, timeout=None):
                self.waits.append(timeout)
                raise subprocess.TimeoutExpired("PRIVATE_COMMAND", timeout)
            def kill(self): self.killed = True
        process = Process()
        with patch("directory_browser.subprocess.Popen", return_value=process), self.assertRaisesRegex(RuntimeError, "超时") as error:
            _bounded_ssh("unit-test-host", {"path": "/tmp"})
        self.assertTrue(process.killed)
        self.assertEqual(process.waits, [20, 5])
        self.assertNotIn("PRIVATE", str(error.exception))
        self.assertTrue(all(stream.closed for stream in (process.stdin, process.stdout, process.stderr)))


if __name__ == "__main__":
    unittest.main()
