"""Scoped MCP initialization probes the real auth channel; no model or worker."""
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch, Mock

import mcp_bridge
from terminal_client import TerminalClient


class ControllerBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "controller.json"
        self.calls = []
        self.reply = {"mode": "scoped", "controller_id": "controller-test", "tools_ready": True}
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                owner.calls.append({"body": json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                                    "auth": self.headers.get("Authorization")})
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(owner.reply).encode())
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.path.write_text(json.dumps({"url": f"http://127.0.0.1:{self.server.server_port}",
                                        "token": "private-controller-cap", "controller_id": "controller-test"}), encoding="utf-8")
        self.env = patch.dict(os.environ, {"PRON_HUB_CONNECTION_FILE": str(self.path), "PRON_HUB_CONTROLLER_ID": "controller-test", "CEO_HUB_CONNECTION_FILE": "not-this-descriptor"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.temp.cleanup()

    @staticmethod
    def initialize(bridge):
        return bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test"}}})

    def test_scoped_initialize_passive_context_only_with_descriptor_auth_and_ninth_tool(self):
        bridge = mcp_bridge.McpBridge()
        response = self.initialize(bridge)
        self.assertIn("result", response)
        self.assertEqual(self.calls, [{"body": {"tool": "get_controller_context", "arguments": {}}, "auth": "Bearer private-controller-cap"}])
        self.assertNotIn("private-controller-cap", json.dumps(response))
        bridge.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        response = bridge.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual(len(response["result"]["tools"]), 9)
        self.assertEqual(response["result"]["tools"][-1]["name"], "get_controller_context")
        self.assertEqual(len(self.calls), 1)

    def test_descriptor_identity_is_not_authentication_and_mismatch_fails_closed(self):
        self.reply = {"mode": "legacy", "controller_id": "controller-test"}
        bridge = mcp_bridge.McpBridge()
        self.assertIn("error", self.initialize(bridge))
        self.assertFalse(bridge.initialized)
        self.reply = {"mode": "scoped", "controller_id": "different"}
        self.assertIn("error", self.initialize(bridge))
        self.assertFalse(bridge.initialized)

    def test_missing_controller_descriptor_cannot_initialize_as_legacy(self):
        self.path.unlink()
        bridge = mcp_bridge.McpBridge()
        self.assertIn("error", self.initialize(bridge))
        self.assertEqual(self.calls, [])

    def test_legacy_initialization_still_requires_neither_descriptor_nor_hub(self):
        with patch.dict(os.environ, {"PRON_HUB_CONNECTION_FILE": "", "PRON_HUB_CONTROLLER_ID": ""}), patch.object(mcp_bridge, "call_service", side_effect=AssertionError("no network")):
            bridge = mcp_bridge.McpBridge()
            self.assertIn("result", self.initialize(bridge))
            bridge.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
            response = bridge.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            self.assertEqual(len(response["result"]["tools"]), 8)

    def test_custom_terminal_descriptor_and_logs_only_change_child_environment(self):
        runtime = Path(self.temp.name) / "broker-logs"
        custom = Path(self.temp.name) / "own-broker.json"
        with patch.dict(os.environ, {"CEO_HUB_TERMINAL_FILE": "unchanged-worker.json"}):
            client = TerminalClient(descriptor_file=custom, runtime=runtime)
            process = Mock()
            process.poll.return_value = None
            with patch.object(client, "running", side_effect=[False, True]), patch("terminal_client.shutil.which", return_value="fake-node"), \
                 patch("terminal_client.subprocess.Popen", return_value=process) as launch:
                client.ensure_started()
            self.assertEqual(os.environ["CEO_HUB_TERMINAL_FILE"], "unchanged-worker.json")
            self.assertEqual(launch.call_args.kwargs["env"]["CEO_HUB_TERMINAL_FILE"], str(custom.resolve()))
            self.assertTrue((runtime / "terminal.out.log").is_file())
            self.assertTrue((runtime / "terminal.err.log").is_file())


if __name__ == "__main__":
    unittest.main()
