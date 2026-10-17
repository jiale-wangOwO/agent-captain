"""Opt-in local PTY + real stdio MCP integration, using an isolated Hub registry."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from urllib.request import Request, ProxyHandler, build_opener

from agent_captain.server.app import create_server
from agent_captain.core.hub import Hub, ROOT


@unittest.skipUnless(os.environ.get("CEO_HUB_LIVE_TESTS") == "1", "opt-in real local PTY; no model inference")
class LiveSharedTerminalTests(unittest.TestCase):
    def test_ui_and_stdio_mcp_share_exact_terminal_and_hub_restart_preserves_it(self):
        with tempfile.TemporaryDirectory(prefix="ceo-hub-live-") as temporary:
            hub = Hub(temporary)
            web = create_server(hub, port=0, token="isolated-live-terminal-capability")
            thread = threading.Thread(target=web.serve_forever, daemon=True)
            thread.start()
            descriptor = Path(temporary) / "connection.json"
            descriptor.write_text(json.dumps({"url": web.origin, "token": web.session_token}), encoding="utf-8")
            env = {**os.environ, "CEO_HUB_CONNECTION_FILE": str(descriptor)}
            bridge = subprocess.Popen([sys.executable, "-u", str(ROOT / "scripts/captain.py"), "mcp"], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            opener = build_opener(ProxyHandler({}))
            terminal_id = None
            counter = 0

            def ui(path, body=None):
                payload = None if body is None else json.dumps(body).encode()
                headers = {"Cookie": "ceo_session=" + web.session_token}
                if body is not None:
                    headers.update({"Origin": web.origin, "Content-Type": "application/json"})
                with opener.open(Request(web.origin + path, data=payload, headers=headers), timeout=20) as response:
                    return json.load(response)

            def rpc(method, params):
                nonlocal counter
                counter += 1
                bridge.stdin.write(json.dumps({"jsonrpc": "2.0", "id": counter, "method": method, "params": params}) + "\n")
                bridge.stdin.flush()
                reply = json.loads(bridge.stdout.readline())
                self.assertNotIn("error", reply)
                return reply["result"]

            def tool(name, args):
                reply = rpc("tools/call", {"name": name, "arguments": args})
                self.assertFalse(reply.get("isError"), reply)
                return json.loads(reply["content"][0]["text"])

            def screen_has(marker):
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    view = tool("read_agent", {"agent_id": agent_id, "limit": 30})
                    if marker in view["screen"]:
                        return view
                    time.sleep(.2)
                self.fail("Expected marker missing from isolated terminal")

            try:
                rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "ceo-hub-live-test", "version": "1"}})
                bridge.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
                bridge.stdin.flush()
                tools = rpc("tools/list", {})["tools"]
                self.assertIn("send_terminal_input", {t["name"] for t in tools})
                created = ui("/api/terminals/create", {"host": "local", "cwd": temporary, "title": "Isolated terminal integration", "active": True, "request_key": "live-shell-" + uuid.uuid4().hex})
                self.assertTrue(created["created"], created)
                terminal_id, thread_id = created["terminal_id"], created["thread_id"]
                agent_id = "local::" + thread_id
                screen_has("PS ")
                identity = {"host": "local", "thread_id": thread_id}
                ui("/api/terminals/input", {**identity, "data": "Write-Output ('UI_' + 'SHARED_OK')\r"})
                screen_has("UI_SHARED_OK")
                deadline = time.monotonic() + 8
                while True:
                    delivery = tool("send_terminal_input", {"agent_id": agent_id, "data": "Write-Output ('PLUGIN_' + 'SHARED_OK')\r", "request_key": "plugin-live-input"})
                    if delivery["state"] != "human_busy" or time.monotonic() >= deadline:
                        break
                    time.sleep(.25)
                self.assertEqual(delivery["state"], "submitted")
                view = screen_has("PLUGIN_SHARED_OK")
                replay = tool("send_terminal_input", {"agent_id": agent_id, "data": "Write-Output ('PLUGIN_' + 'SHARED_OK')\r", "request_key": "plugin-live-input"})
                self.assertEqual(replay["request_id"], delivery["request_id"])
                self.assertEqual(sum(line.strip() == "PLUGIN_SHARED_OK" for line in view["screen"].splitlines()), 1)
                output = ui("/api/terminals/output?host=local&thread_id=" + thread_id + "&after=0")
                self.assertIn("PLUGIN_SHARED_OK", output["ansi"])
                waiting = tool("send_agent_message", {"agent_id": agent_id, "text": "This task must never execute at a shell prompt", "request_key": "shell-task-guard"})
                self.assertEqual(waiting["state"], "waiting_for_agent")
                web.shutdown()
                hub.close()
                restored = Hub(temporary)
                try:
                    restored_view = restored.read_agent("local", thread_id)
                    self.assertEqual(restored_view["terminal_id"], terminal_id)
                    self.assertTrue(restored_view["connected"])
                    self.assertIn("PLUGIN_SHARED_OK", restored_view["screen"])
                finally:
                    restored.close()
            finally:
                if terminal_id:
                    hub.terminals.write(terminal_id, "close", {})
                bridge.terminate()
                bridge.communicate(timeout=5)
                web.shutdown()
                web.server_close()
                thread.join(2)
                hub.close()


if __name__ == "__main__":
    unittest.main()
