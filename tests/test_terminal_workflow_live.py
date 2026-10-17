"""Opt-in real PTY checks for default-home creation and scoped terminal close."""
import json
import os
import shlex
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from agent_captain.server.app import create_server
from agent_captain.core.terminal_hub import Hub


@unittest.skipUnless(os.environ.get("CEO_HUB_LIVE_TESTS") == "1", "opt-in disposable local PTYs")
class TerminalWorkflowLiveTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("CEO_HUB_REMOTE_TESTS") == "1", "opt-in disposable 316 tmux terminal")
    def test_remote_home_and_close_remove_only_the_owned_tmux(self):
        with tempfile.TemporaryDirectory(prefix="ceo-remote-workflow-") as temporary:
            hub = Hub(temporary)
            terminal_id = None
            previous = {session["id"] for session in hub.terminals.sessions() if session.get("connected")}
            try:
                listing = hub.list_directories("316")
                self.assertTrue(listing["home"].startswith("/"))
                terminal = hub.create_terminal("316", "", "Disposable remote close verification",
                    request_key="remote-workflow-" + uuid.uuid4().hex)
                self.assertTrue(terminal["created"], terminal)
                terminal_id = terminal["terminal_id"]
                self.assertEqual(terminal["cwd"], listing["home"])
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    view = hub.read_agent("316", terminal["thread_id"], 20)
                    if view.get("state") == "shell" and view.get("screen", "").strip():
                        break
                    time.sleep(.2)
                else:
                    self.fail("Disposable remote shell did not become ready")
                closed = hub.terminal_close("316", terminal["thread_id"], confirm=True)
                self.assertTrue(closed["closed"], closed)
                target = hub.host("316")["target"]
                check = subprocess.run(["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", target,
                    "tmux has-session -t " + shlex.quote(terminal_id)], capture_output=True, timeout=15,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                self.assertEqual(check.returncode, 1, "Owned disposable tmux should no longer exist")
                self.assertTrue(previous.issubset({s["id"] for s in hub.terminals.sessions() if s.get("connected")}))
            finally:
                if terminal_id:
                    hub.terminals.write(terminal_id, "close", {})
                hub.close()

    def test_home_directory_picker_and_confirmed_close_preserve_other_terminals(self):
        with tempfile.TemporaryDirectory(prefix="ceo-workflow-live-") as temporary:
            hub = Hub(temporary)
            server = create_server(hub, port=0, token="isolated-workflow-capability")
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            opener = build_opener(ProxyHandler({}))
            owned = []
            previous = {session["id"] for session in hub.terminals.sessions() if session.get("connected")}

            def ui(path, body):
                request = Request(server.origin + path, data=json.dumps(body).encode(), headers={
                    "Cookie": "ceo_session=" + server.session_token,
                    "Origin": server.origin,
                    "Content-Type": "application/json",
                })
                with opener.open(request, timeout=25) as response:
                    return json.load(response)

            def create(cwd, title):
                reply = ui("/api/terminals/create", {"host": "local", "cwd": cwd, "title": title,
                    "active": True, "request_key": "workflow-" + uuid.uuid4().hex})
                self.assertTrue(reply["created"], reply)
                owned.append(reply["terminal_id"])
                return reply

            try:
                listing = ui("/api/directories/list", {"host": "local", "path": ""})
                self.assertEqual(Path(listing["home"]).resolve(), Path.home().resolve())
                self.assertEqual(listing["path"], listing["home"])
                self.assertIn("roots", listing)
                self.assertIn("directories", listing)
                fixture = Path(temporary) / "目录 with spaces"
                fixture.mkdir()
                children = ui("/api/directories/list", {"host": "local", "path": temporary})
                self.assertIn(str(fixture), {item["path"] for item in children["directories"]})
                victim = create("", "Disposable home terminal")
                self.assertEqual(Path(victim["cwd"]).resolve(), Path.home().resolve())
                keeper = create(str(fixture), "Disposable retained terminal")
                identity = {"host": "local", "thread_id": victim["thread_id"]}
                with self.assertRaises(HTTPError) as denied:
                    ui("/api/terminals/close", identity)
                self.assertEqual(denied.exception.code, 400)
                self.assertTrue(hub.get_agent("local", victim["thread_id"])["connected"])
                result = ui("/api/terminals/close", {**identity, "confirm": True})
                self.assertTrue(result.get("closed"), result)
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline and hub.get_agent("local", victim["thread_id"])["connected"]:
                    time.sleep(.1)
                ended = hub.get_agent("local", victim["thread_id"])
                self.assertFalse(ended["connected"])
                self.assertFalse(ended["active"])
                self.assertEqual(ended["state"], "closed")
                self.assertIsNotNone(hub.store.agent("local", victim["thread_id"]))
                self.assertTrue(hub.get_agent("local", keeper["thread_id"])["connected"])
                self.assertTrue(previous.issubset({s["id"] for s in hub.terminals.sessions() if s.get("connected")}))
            finally:
                for terminal_id in owned:
                    hub.terminals.write(terminal_id, "close", {})
                server.shutdown()
                server.server_close()
                worker.join(2)
                hub.close()


if __name__ == "__main__":
    unittest.main()
