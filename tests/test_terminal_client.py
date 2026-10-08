"""The local terminal capability cannot be redirected or used to start scans."""
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
from urllib.error import HTTPError

from terminal_client import BrokerError, TerminalClient


class TerminalClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "terminal.json"
        self.env = patch.dict(os.environ, {"CEO_HUB_TERMINAL_FILE": str(self.path)})
        self.env.start()
        self.client = TerminalClient()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def descriptor(self, url="http://127.0.0.1:39001", token="test-capability-0123456789", pid=123):
        self.path.write_text(json.dumps({"url": url, "token": token, "pid": pid, "version": "0.3.0"}), encoding="utf-8")

    def test_only_literal_loopback_without_credentials_paths_or_queries_is_accepted(self):
        for url in ("https://127.0.0.1:3", "http://localhost:3", "http://example.com:3", "http://user@127.0.0.1:3",
                    "http://127.0.0.1:3/private", "http://127.0.0.1:3?token=bad", "http://127.0.0.1:3#bad"):
            with self.subTest(url=url):
                self.descriptor(url)
                with patch.object(self.client.opener, "open", side_effect=AssertionError("no network")), self.assertRaises(ValueError):
                    self.client.request("/sessions")

    def test_descriptor_reloads_for_every_request_after_restart(self):
        self.descriptor()
        self.assertEqual(self.client.descriptor()["pid"], 123)
        self.descriptor(pid=456)
        self.assertEqual(self.client.descriptor()["pid"], 456)

    def test_absent_broker_registry_read_never_starts_a_process(self):
        with patch.object(self.client, "ensure_started", side_effect=AssertionError("no launch")):
            self.assertEqual(self.client.sessions(), [])

    def test_pid_and_version_must_match_to_reuse_a_broker(self):
        self.descriptor()
        with patch.object(self.client, "request", return_value={"pid": 456, "version": "0.3.0"}):
            self.assertFalse(self.client.running())
        with patch.object(self.client, "request", return_value={"pid": 123, "version": "0.1.0"}):
            self.assertFalse(self.client.running())
        with patch.object(self.client, "request", return_value={"pid": 123, "version": "0.3.0"}):
            self.assertTrue(self.client.running())

    def test_error_codes_and_messages_preserve_known_non_delivery(self):
        self.descriptor()
        error = HTTPError("http://127.0.0.1:39001", 409, "Conflict", {}, io.BytesIO(b'{"error":"human_busy","message":"User has a draft"}'))
        with patch.object(self.client.opener, "open", side_effect=error):
            with self.assertRaises(BrokerError) as result:
                self.client.request("/sessions/x/prompt", {"text": "hello"})
        self.assertEqual(result.exception.code, "human_busy")
        self.assertEqual(str(result.exception), "User has a draft")

    def test_browser_initial_output_restores_atomic_screen_dimensions_and_sequence(self):
        view = {"ansi": "\x1b[32mShared screen", "current_seq": 42, "connected": True, "state": "shell", "cols": 93, "rows": 27}
        with patch.object(self.client, "snapshot", return_value=view) as snapshot:
            result = self.client.output("ceohub-0123456789abcdef", after=0)
        self.assertTrue(result["reset"])
        self.assertEqual((result["seq"], result["cols"], result["rows"]), (42, 93, 27))
        self.assertEqual(result["ansi"], view["ansi"])
        snapshot.assert_called_once_with("ceohub-0123456789abcdef", 5000)


if __name__ == "__main__":
    unittest.main()
