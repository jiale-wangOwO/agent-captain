"""Controller capability, scope and delivery fences; fake terminals only."""
import json
import tempfile
import threading
import unittest
from pathlib import PurePosixPath, PureWindowsPath
from urllib.error import HTTPError
from urllib.request import Request, ProxyHandler, build_opener
from unittest.mock import patch

from agent_captain.server.app import create_server
from agent_captain.transport.terminal_client import BrokerError
from agent_captain.core.terminal_hub import Hub


class Terminals:
    def __init__(self):
        self.records, self.writes, self.creates = {}, [], []
        self.error = None

    def sessions(self):
        return list(self.records.values())

    def add(self, terminal_id, host="local"):
        view = {"id": terminal_id, "host": host, "connected": True, "state": "agent", "command": "codex",
                "agent_ready": True, "current_seq": 1, "screen": "Codex", "ansi": "Codex", "cols": 80, "rows": 20}
        self.records[terminal_id] = view
        return view

    def create(self, body):
        self.creates.append(body)
        return self.add(body["session_id"], body["host"])

    def write(self, terminal_id, action, body):
        if self.error:
            raise self.error
        self.writes.append((terminal_id, action, body))
        return {"state": "submitted"}

    def snapshot(self, terminal_id, lines=60):
        return dict(self.records[terminal_id])


class Controllers:
    def __init__(self, hub):
        self.hub = hub
        self.grants = {"a": {"local::one"}, "b": {"local::two"}}
        self.roots = {"a": {"local": ["C:/scope"]}, "b": {}}
        self.active = {"a", "b"}
        self.manager_refs = set()
        self.seen = []
        self.state = "ready"

    def shutdown(self):
        pass

    def set_gateway_origin(self, url):
        self.origin = url

    def authenticate(self, token):
        return {"cap-a": "a", "cap-b": "b"}.get(token)

    def view_context(self, cid):
        if cid not in self.active:
            raise PermissionError("revoked")
        return {"mode": "scoped", "controller_id": cid, "worker_access": "automatic", "controller": {"state": self.state,
                "session_id": "11111111-1111-4111-8111-111111111111", "tools_ready": True}}

    def mark_tool_seen(self, cid):
        self.seen.append(cid)

    def allowed_agents(self, cid):
        return sorted(self.hub.agent_id(row["host"], row["thread_id"]) for row in self.hub.managed_worker_rows())

    def allowed_hosts(self, cid):
        return self.roots[cid]

    def authorize(self, cid, host, thread_id):
        return cid in self.active and self.state == "ready" and self.hub.is_managed_worker(self.hub.store.agent(host, thread_id))

    def authorize_creation(self, cid, host, cwd):
        if cid not in self.active:
            return False
        path_cls = PureWindowsPath if ":" in cwd else PurePosixPath
        return any(path_cls(cwd).is_relative_to(path_cls(root)) for root in self.roots[cid].get(host, []))

    def is_controller_reference(self, host, thread_id, cwd=None):
        return host == "local" and (thread_id in self.manager_refs or cwd == "C:/manager")

    def add_created_agent(self, cid, host, thread_id):
        self.grants[cid].add(host + "::" + thread_id)


class ControllerScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.terminals = Terminals()
        self.hub = Hub(self.temp.name, terminals=self.terminals)
        self.hub.controllers.shutdown()
        self.hub.controllers = Controllers(self.hub)
        self.hub.hosts = {"local": {"transport": "local", "label": "Local"},
                          "hidden": {"transport": "ssh", "target": "fake-hidden"}}
        self.hub.connections["hidden"] = {"connected": True}
        for name in ("one", "two", "inactive"):
            terminal_id = "ceohub-" + {"one": "1", "two": "2", "inactive": "3"}[name] * 24
            self.terminals.add(terminal_id)
            self.hub.store.remember("local", name, name, active=name != "inactive", terminal_id=terminal_id, mode="terminal", cwd="C:/scope")

    def tearDown(self):
        self.hub.close()
        self.temp.cleanup()

    def call(self, tool, cid="a", **arguments):
        return self.hub.plugin_call(tool, arguments, controller_id=cid)

    def test_automatic_inventory_includes_all_open_workers_without_legacy_activation(self):
        result = self.call("list_active_agents")
        self.assertEqual(set(r["agent_id"] for r in result["agents"]), {"local::one", "local::two", "local::inactive"})
        self.assertEqual(result["connected_hosts"], ["local"])
        self.assertFalse(result["legacy_scope_shared"])
        for name in ("two", "inactive"):
            self.assertEqual(self.call("get_agent", agent_id="local::" + name)["agent_id"], "local::" + name)
        legacy = self.hub.plugin_call("list_active_agents", {})
        self.assertTrue(legacy["legacy_scope_shared"])
        self.assertEqual(len(legacy["agents"]), 3)

    def test_self_context_marks_real_tool_use_not_model_claim(self):
        self.assertEqual(self.call("get_controller_context")["controller_id"], "a")
        self.assertEqual(self.hub.controllers.seen, ["a"])
        self.assertEqual(self.hub.plugin_call("get_controller_context", {})["mode"], "legacy")

    def test_future_and_disconnected_original_workers_join_without_creation_roots(self):
        tid = "ceohub-" + "4" * 24
        self.terminals.add(tid, "hidden").update(connected=False, state="disconnected", agent_ready=False)
        self.hub.store.remember("hidden", "later", "Later", active=False, terminal_id=tid, mode="terminal", cwd="/work")
        result = self.call("list_active_agents")
        self.assertIn("hidden::later", [r["agent_id"] for r in result["agents"]])
        self.assertFalse(self.call("get_agent", agent_id="hidden::later")["connected"])
        self.assertEqual(self.hub.controllers.allowed_hosts("b"), {})
        self.assertTrue(self.hub.controllers.authorize("b", "hidden", "later"))
        self.assertEqual(self.terminals.creates, [])
        self.assertEqual(self.terminals.writes, [])

    def test_bootstrap_can_only_read_self_context_not_worker_inventory_or_dispatch(self):
        self.hub.controllers.state = "initializing"
        self.assertEqual(self.call("get_controller_context")["mode"], "scoped")
        for tool, arguments in (("list_active_agents", {}), ("poll_events", {}),
                                 ("send_agent_message", {"agent_id": "local::one", "text": "not authorized during init", "request_key": "bootstrap"})):
            with self.assertRaises(PermissionError):
                self.call(tool, **arguments)
        self.assertEqual(self.terminals.writes, [])

    def test_delivery_namespace_isolates_identical_keys_and_record_ownership(self):
        self.hub.controllers.grants["b"].add("local::one")
        first = self.call("send_agent_message", agent_id="local::one", text="task", request_key="same")
        replay = self.call("send_agent_message", agent_id="local::one", text="task", request_key="same")
        other = self.call("send_agent_message", cid="b", agent_id="local::one", text="task", request_key="same")
        self.assertEqual(first["request_id"], replay["request_id"])
        self.assertNotEqual(first["request_id"], other["request_id"])
        self.assertEqual(len(self.terminals.writes), 2)
        with self.assertRaises(PermissionError):
            self.call("get_delivery", request_id=other["request_id"])
        self.assertEqual(self.call("get_delivery", request_id=first["request_id"])["state"], "submitted")

    def test_prior_revoked_delivery_never_replays_after_automatic_management(self):
        self.hub.controllers.grants["b"].add("local::one")
        self.terminals.error = BrokerError("human is typing", "human_busy", 409)
        one = self.call("send_agent_message", agent_id="local::one", text="queued", request_key="pending")
        two = self.call("send_agent_message", cid="b", agent_id="local::one", text="queued", request_key="pending")
        self.assertEqual(one["state"], "human_busy")
        with self.hub.registry_lock:
            self.hub.scope_revoking("a", ["local::one"])
        self.assertEqual(self.hub.store.delivery(one["request_id"])["state"], "scope_revoked")
        self.assertEqual(self.hub.store.delivery(two["request_id"])["state"], "human_busy")
        self.terminals.error = None
        retry = self.call("send_agent_message", agent_id="local::one", text="queued", request_key="pending")
        self.assertEqual(retry["state"], "scope_revoked")
        self.assertEqual(self.terminals.writes, [])

    def test_last_moment_scope_recheck_prevents_write_after_enqueue(self):
        enqueue = self.hub.store.enqueue
        def revoke(*args, **kwargs):
            result = enqueue(*args, **kwargs)
            self.hub.controllers.active.remove("a")
            return result
        with patch.object(self.hub.store, "enqueue", side_effect=revoke):
            result = self.call("send_agent_message", agent_id="local::one", text="must not write", request_key="race")
        self.assertEqual(result["state"], "scope_revoked")
        self.assertEqual(self.terminals.writes, [])

    def test_events_include_all_workers_but_not_history_or_manager_references(self):
        self.hub.record_event("terminal_output", host="local", thread_id="two")
        self.hub.record_event("terminal_output", host="local", thread_id="one")
        self.hub.store.remember("local", "history", "History", active=True, mode="history")
        self.hub.record_event("terminal_output", host="local", thread_id="history")
        self.hub.controllers.manager_refs.add("manager")
        self.hub.store.remember("local", "manager", "Manager", active=True, mode="terminal", terminal_id="ceohub-" + "f" * 24)
        self.hub.record_event("terminal_output", host="local", thread_id="manager")
        result = self.call("poll_events")
        self.assertEqual([e["data"]["thread_id"] for e in result["events"]], ["two", "one"])

    def test_manager_reference_cannot_be_registered_bound_or_read(self):
        self.hub.controllers.manager_refs.add("manager-session")
        for fn in (lambda: self.hub.get_agent("local", "manager-session"),
                   lambda: self.hub.create_terminal("local", "C:/scope", "manager", "manager-session"),
                   lambda: self.hub.create_terminal("local", "C:/manager", "manager")):
            with self.assertRaises(PermissionError):
                fn()
        sid = "11111111-1111-4111-8111-111111111111"
        self.hub.controllers.manager_refs.add(sid)
        with self.assertRaises(PermissionError):
            self.hub.bind_output("local", "one", sid)
        self.assertEqual(self.terminals.creates, [])

    def test_create_requires_explicit_root_checks_canonical_path_and_never_shell_command(self):
        args = {"host": "local", "cwd": "C:/scope/project", "title": "work", "request_key": "create"}
        with self.assertRaises(PermissionError):
            self.call("create_agent", **{**args, "command": "codex --yolo"})
        with patch.object(self.hub.directories, "list", return_value={"path": "C:/outside"}):
            with self.assertRaises(PermissionError):
                self.call("create_agent", **args)
        with patch.object(self.hub.directories, "list", return_value={"path": args["cwd"]}):
            result = self.call("create_agent", **args)
        self.assertTrue(result["created"])
        self.assertIn(result["agent_id"], self.hub.controllers.allowed_agents("a"))
        self.assertIn(result["agent_id"], self.hub.controllers.allowed_agents("b"))
        self.assertFalse(self.hub.controllers.authorize_creation("b", "local", "C:/scope/other"))
        self.assertEqual(self.terminals.writes, [])

    def test_http_auth_identity_not_body_identity_and_browser_cannot_use_cap(self):
        server = create_server(self.hub, port=0, token="admin-root")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        opener = build_opener(ProxyHandler({}))
        def request(token, arguments, origin=None, tool="get_agent"):
            body = {"tool": tool, "controller_id": "b", "arguments": arguments}
            headers = {"Authorization": "Bearer " + token}
            if origin:
                headers["Origin"] = origin
            with opener.open(Request(server.origin + "/api/plugin", data=json.dumps(body).encode(), headers=headers), timeout=3) as reply:
                return json.load(reply)
        try:
            self.assertEqual(request("cap-a", {"agent_id": "local::one"})["agent_id"], "local::one")
            self.assertEqual(request("cap-a", {"agent_id": "local::two"})["agent_id"], "local::two")
            self.assertEqual(request("cap-a", {}, tool="get_controller_context")["controller_id"], "a")
            for token, args, origin in (("cap-a", {"agent_id": "local::missing"}, None),
                                        ("cap-a", {"agent_id": "local::one"}, server.origin),
                                        ("invalid", {"agent_id": "local::one"}, None)):
                with self.assertRaises(HTTPError) as error:
                    request(token, args, origin)
                self.assertEqual(error.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


if __name__ == "__main__":
    unittest.main()
