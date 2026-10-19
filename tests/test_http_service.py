import json
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler
from unittest.mock import Mock

from agent_captain.server.app import create_server
from agent_captain.core.terminal_hub import Hub, HistoryReadError


class HttpServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        terminals = Mock()
        terminals.sessions.return_value = []
        self.hub = Hub(self.temp.name, terminals=terminals)
        self.server = create_server(self.hub, port=0, token="test-private-token")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.opener = build_opener(ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.hub.close()
        self.temp.cleanup()

    def request(self, path, body=None, **headers):
        data = json.dumps(body).encode() if body is not None else None
        # Windows shared CI runners can briefly stall loopback scheduling. This
        # is an authorization contract, not a three-second latency benchmark.
        with self.opener.open(Request(self.server.origin + path, data=data, headers=headers), timeout=10) as response:
            return json.load(response)

    def test_plugin_read_is_authorized_without_browser_cookie_or_model(self):
        self.hub.store.remember("local", "active", "test", active=False, mode="terminal", terminal_id="ceohub-" + "a" * 32)
        result = self.request("/api/plugin", {"tool": "list_active_agents", "arguments": {}}, Authorization="Bearer test-private-token")
        self.assertEqual(result["agents"][0]["agent_id"], "local::active")
        self.assertFalse(hasattr(self.hub, "servers"))
        self.assertFalse(hasattr(self.hub, "live_servers"))

    def test_busy_read_is_explicit_conflict_for_ui_and_plugin_not_worker_offline(self):
        self.hub.read_agent = Mock(side_effect=HistoryReadError("ssh_read_in_progress"))
        self.hub.plugin_call = Mock(side_effect=HistoryReadError("ssh_read_in_progress"))
        cases = (
            ("/api/agents/read", {"host": "fixture", "thread_id": "original", "cursor": "original-page"},
             {"Cookie": "ceo_session=test-private-token", "Origin": self.server.origin}),
            ("/api/plugin", {"tool": "read_agent", "arguments": {"agent_id": "fixture::original", "cursor": "original-page"}},
             {"Authorization": "Bearer test-private-token"}),
        )
        for path, body, headers in cases:
            with self.subTest(path=path), self.assertRaises(HTTPError) as caught:
                self.request(path, body, **headers)
            self.assertEqual(caught.exception.code, 409)
            result = json.load(caught.exception)
            self.assertEqual(result["code"], "ssh_read_in_progress")
            self.assertIn("原游标", result["error"])
            self.assertNotIn("original-page", result["error"])
            self.assertNotIn("messages", result)
            self.assertNotIn("output_binding", result)
        self.assertEqual(self.hub.store.outbox(), [])

    def test_browser_origin_cannot_use_bearer_channel(self):
        with self.assertRaises(HTTPError) as error:
            self.request("/api/plugin", {"tool": "list_active_agents", "arguments": {}}, Authorization="Bearer test-private-token", Origin="https://evil.example")
        self.assertEqual(error.exception.code, 403)

    def test_missing_auth_and_non_loopback_host_are_rejected(self):
        with self.assertRaises(HTTPError):
            self.request("/api/plugin", {"tool": "list_active_agents", "arguments": {}})
        with self.assertRaises(HTTPError):
            self.request("/health", Host="evil.example")

    def test_brand_mark_is_an_explicit_safe_local_static_route(self):
        with self.opener.open(self.server.origin + "/agent-captain-mark.png", timeout=10) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Content-Type"], "image/png")
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
            self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])
            self.assertIsNone(response.headers.get("Set-Cookie"))
            payload = response.read()
        from pathlib import Path
        expected = Path(__file__).resolve().parents[1] / "assets" / "brand" / "agentcaptain-mark.png"
        self.assertTrue(payload.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(payload, expected.read_bytes())
        for path in ("/assets/agent-captain-logo.svg", "/assets/pron-hub-logo.png", "/agent-captain-mark.svg",
                     "/assets/brand/agentcaptain-mark.svg", "/assets/brand/agentcaptain-mark.png", "/assets/brand/generation.md",
                     "/../assets/agent-captain-logo.svg", "/%2e%2e/assets/agent-captain-logo.svg",
                     "/agent-captain-mark.png/../config.json"):
            with self.subTest(path=path), self.assertRaises(HTTPError) as error:
                self.opener.open(self.server.origin + path, timeout=10)
            self.assertEqual(error.exception.code, 404)
        with self.assertRaises(HTTPError) as error:
            self.opener.open(Request(self.server.origin + "/agent-captain-mark.png", headers={"Host": "evil.example"}), timeout=10)
        self.assertEqual(error.exception.code, 403)

    def test_web_cookie_requires_same_origin_for_mutation(self):
        cookie = "ceo_session=test-private-token"
        with self.assertRaises(HTTPError):
            self.request("/api/agents/activate", {"host": "local", "thread_id": "t", "active": True}, Cookie=cookie)
        self.hub.store.remember("local", "t", "test", active=False, mode="terminal", terminal_id="ceohub-" + "b" * 32)
        row = self.request("/api/agents/activate", {"host": "local", "thread_id": "t", "active": True}, Cookie=cookie, Origin=self.server.origin)
        self.assertTrue(row["active"])

    def test_terminal_paths_use_browser_cookie_and_origin_not_plugin_capability(self):
        with self.assertRaises(HTTPError) as error:
            self.request("/api/terminals/input", {"host": "local", "thread_id": "t", "data": "hello"}, Authorization="Bearer test-private-token")
        self.assertEqual(error.exception.code, 403)
        with self.assertRaises(HTTPError):
            self.request("/api/terminals/output?host=local&thread_id=t&after=0")

    def test_legacy_worker_scope_body_does_not_clear_creation_directory_permissions(self):
        self.hub.controllers.set_scope = Mock(return_value={"worker_access": "automatic"})
        cookie = "ceo_session=test-private-token"
        self.request("/api/controllers/scope", {"id": "fixture", "agent_ids": []}, Cookie=cookie, Origin=self.server.origin)
        self.hub.controllers.set_scope.assert_called_once_with("fixture", [], allowed_hosts=None, host_roots=None)
        self.hub.controllers.set_scope.reset_mock()
        roots = {"local": ["C:/synthetic-project"]}
        self.request("/api/controllers/scope", {"id": "fixture", "host_roots": roots}, Cookie=cookie, Origin=self.server.origin)
        self.hub.controllers.set_scope.assert_called_once_with("fixture", [], allowed_hosts=None, host_roots=roots)

    def test_terminal_routes_forward_same_identity_and_do_not_persist_ui_keys(self):
        calls = []
        self.hub.terminal_input = lambda host, thread, data, **kw: calls.append((host, thread, data, kw)) or {"accepted": True}
        self.hub.terminal_output = lambda host, thread, after: {"ansi": "Shared", "seq": 2, "reset": True, "cols": 90, "rows": 25}
        cookie = "ceo_session=test-private-token"
        self.request("/api/terminals/input", {"host": "local", "thread_id": "t", "data": "exact\r"}, Cookie=cookie, Origin=self.server.origin)
        output = self.request("/api/terminals/output?host=local&thread_id=t&after=0", Cookie=cookie)
        self.assertEqual(calls, [("local", "t", "exact\r", {"expected_terminal_id": None})])
        self.assertEqual(output["ansi"], "Shared")
        self.assertEqual(self.hub.store.outbox(), [])

    def test_directory_picker_is_same_origin_and_allows_empty_path(self):
        calls = []
        self.hub.list_directories = lambda host, path: calls.append((host, path)) or {"path": "C:/Users/Test", "home": "C:/Users/Test", "parent": "C:/Users", "roots": [], "directories": []}
        cookie = "ceo_session=test-private-token"
        with self.assertRaises(HTTPError):
            self.request("/api/directories/list", {"host": "local", "path": ""}, Authorization="Bearer test-private-token")
        result = self.request("/api/directories/list", {"host": "local"}, Cookie=cookie, Origin=self.server.origin)
        self.assertEqual(calls, [("local", "")])
        self.assertEqual(result["home"], "C:/Users/Test")

    def test_create_preflight_connection_error_is_safe_and_does_not_create_terminal(self):
        message = "无法通过 SSH 读取远端目录；请检查机器连接及远端 python3 后重试。"
        self.hub.directories.home = Mock(side_effect=RuntimeError(message))
        cookie = "ceo_session=test-private-token"
        with self.assertRaises(HTTPError) as error:
            self.request("/api/terminals/create", {
                "host": "local", "cwd": "", "title": "New work", "request_key": "directory-preflight"
            }, Cookie=cookie, Origin=self.server.origin)
        self.assertEqual(error.exception.code, 400)
        self.assertEqual(json.load(error.exception), {"error": message})
        self.hub.terminals.create.assert_not_called()
        self.assertEqual(self.hub.store.agents(), [])
        self.assertEqual(self.hub.store.db.execute("select count(*) from operations").fetchone()[0], 0)

    def test_close_route_needs_explicit_confirmation(self):
        calls = []
        def close(host, thread_id, confirm=False, expected_terminal_id=None):
            if confirm is not True:
                raise ValueError("explicit confirmation required")
            calls.append((host, thread_id, expected_terminal_id))
            return {"closed": True, "status": "closed"}
        self.hub.terminal_close = close
        cookie = "ceo_session=test-private-token"
        with self.assertRaises(HTTPError):
            self.request("/api/terminals/close", {"host": "local", "thread_id": "owned"}, Cookie=cookie, Origin=self.server.origin)
        result = self.request("/api/terminals/close", {"host": "local", "thread_id": "owned", "confirm": True}, Cookie=cookie, Origin=self.server.origin)
        self.assertTrue(result["closed"])
        self.assertEqual(calls, [("local", "owned", None)])

    def test_terminal_mutations_forward_exact_captured_generation_without_persisting_ui_input(self):
        terminal_id = "ceohub-" + "a" * 32
        cookie = "ceo_session=test-private-token"
        cases = (
            ("/api/terminals/input", "terminal_input", {"data": "exact\r", "request_key": "not-a-persisted-ui-delivery"},
             ("local", "t", "exact\r"), {"expected_terminal_id": terminal_id}),
            ("/api/terminals/resize", "terminal_resize", {"cols": 90, "rows": 25},
             ("local", "t", 90, 25), {"expected_terminal_id": terminal_id}),
            ("/api/terminals/close", "terminal_close", {"confirm": True},
             ("local", "t", True), {"expected_terminal_id": terminal_id}),
            ("/api/agents/message", "send", {"text": "fresh-generation task", "request_key": "message-generation-once"},
             ("local", "t", "fresh-generation task"), {"request_key": "message-generation-once", "expected_terminal_id": terminal_id}),
        )
        for route, method, changes, arguments, keywords in cases:
            with self.subTest(route=route):
                handler = Mock(return_value={"accepted": True})
                setattr(self.hub, method, handler)
                self.request(route, {"host": "local", "thread_id": "t", "expected_terminal_id": terminal_id, **changes},
                    Cookie=cookie, Origin=self.server.origin)
                handler.assert_called_once_with(*arguments, **keywords)
        self.assertEqual(self.hub.store.outbox(), [])

    def test_reopen_route_requires_browser_session_same_origin_and_explicit_selected_identity(self):
        terminal_id = "ceohub-" + "a" * 32
        body = {"host": "local", "thread_id": "selected", "expected_terminal_id": terminal_id,
            "request_key": "reopen-one-selected-terminal", "confirmed": True}
        receipt = {"operation": "reopen_terminal", "operation_id": body["request_key"],
            "previous_terminal_id": terminal_id, "operation_state": "unknown", "reopened": False,
            "recovery": {"code": "reopen_close_unknown", "next_action": "inspect_connection"}}
        self.hub.reopen_terminal = Mock(return_value=receipt)
        denied = ({"Authorization": "Bearer test-private-token"},
                  {"Cookie": "ceo_session=test-private-token"},
                  {"Cookie": "ceo_session=test-private-token", "Origin": "https://evil.example"})
        for headers in denied:
            with self.subTest(headers=sorted(headers)), self.assertRaises(HTTPError) as error:
                self.request("/api/terminals/reopen", body, **headers)
            self.assertEqual(error.exception.code, 403)
        self.hub.reopen_terminal.assert_not_called()
        result = self.request("/api/terminals/reopen", body,
            Cookie="ceo_session=test-private-token", Origin=self.server.origin)
        self.assertEqual(result, receipt, "Unknown lifecycle evidence must not be relabelled as success")
        self.hub.reopen_terminal.assert_called_once_with("local", "selected", body["request_key"], terminal_id, confirm=True)
        self.assertEqual(self.hub.store.outbox(), [])

    def test_reopen_route_does_not_coerce_truthy_or_legacy_confirmation_into_permission(self):
        terminal_id = "ceohub-" + "a" * 32
        attempts = []
        def reopen(host, thread_id, request_key, expected_terminal_id, confirm=False):
            attempts.append((host, thread_id, request_key, expected_terminal_id, confirm))
            if confirm is not True:
                raise ValueError("explicit confirmation required")
            return {"reopened": True}
        self.hub.reopen_terminal = reopen
        base = {"host": "local", "thread_id": "selected", "request_key": "same-reopen-intent", "expected_terminal_id": terminal_id}
        for changes in ({}, {"confirmed": False}, {"confirmed": 1}, {"confirmed": "true"}, {"confirm": True}):
            with self.subTest(changes=changes), self.assertRaises(HTTPError) as error:
                self.request("/api/terminals/reopen", {**base, **changes},
                    Cookie="ceo_session=test-private-token", Origin=self.server.origin)
            self.assertEqual(error.exception.code, 400)
        self.assertTrue(all(attempt[-1] is False for attempt in attempts))
        self.assertEqual(len(attempts), 5)
        self.assertEqual(self.hub.store.outbox(), [])

    def test_output_association_is_only_ui_origin_and_read_forwards_pagination(self):
        calls = []
        self.hub.bind_output = lambda host, thread, session: calls.append(("bind", host, thread, session)) or {"associated": True}
        self.hub.read_agent = lambda host, thread, limit, **kw: calls.append(("read", host, thread, limit, kw)) or {"source": "codex_rollout"}
        cookie = "ceo_session=test-private-token"
        payload = {"host": "local", "thread_id": "selected", "session_id": "11111111-1111-4111-8111-111111111111"}
        with self.assertRaises(HTTPError):
            self.request("/api/agents/bind-output", payload, Authorization="Bearer test-private-token")
        self.request("/api/agents/bind-output", payload, Cookie=cookie, Origin=self.server.origin)
        result = self.request("/api/agents/read", {"host": "local", "thread_id": "selected", "limit": 3,
            "cursor": "page", "include_tools": True}, Cookie=cookie, Origin=self.server.origin)
        self.assertEqual(result["source"], "codex_rollout")
        self.assertEqual(calls[-1], ("read", "local", "selected", 3, {"cursor": "page", "after_cursor": None, "include_tools": True}))


if __name__ == "__main__":
    unittest.main()
