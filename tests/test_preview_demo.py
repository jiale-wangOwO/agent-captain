"""No production services, credentials, remote hosts or model processes."""

from contextlib import contextmanager
from http.client import HTTPConnection
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen


SPEC = importlib.util.spec_from_file_location("captain_preview_demo", Path(__file__).resolve().parents[1] / "scripts/preview_demo.py")
demo = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(demo)


@contextmanager
def preview():
    with demo.DemoServer(0) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server, f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join(timeout=2)


def request(base, path, body=None, method=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = Request(base + path, data=data, method=method, headers=headers or {})
    try:
        result = urlopen(req, timeout=3)
    except HTTPError as error:
        result = error
    with result:
        return result.status, result.headers, result.read()


class PreviewDemoTests(unittest.TestCase):
    def test_fixture_records_are_synthetic_and_detached(self):
        state = demo.fixture_state()
        self.assertTrue(state["demo"])
        self.assertEqual(len(state["hosts"]), 3)
        self.assertTrue(all(worker["thread_id"].startswith("demo-") for worker in state["agents"]))
        state["hosts"][0]["label"] = "changed"
        self.assertEqual(demo.fixture_state()["hosts"][0]["label"], "Local workstation")

    def test_delivery_times_and_response_samples_have_api_compatible_numeric_types(self):
        state = demo.fixture_state()
        delivery = state["outbox"][0]
        self.assertIsInstance(delivery["time"], (int, float))
        self.assertIsInstance(delivery["submitted_at"], (int, float))
        self.assertLess(delivery["time"], delivery["submitted_at"])
        self.assertIsNone(state["hosts"][0]["latency"]["last_ms"])
        for host in state["hosts"][1:]:
            self.assertEqual(host["latency"]["metric"], "ssh_response")
            self.assertGreater(host["latency"]["last_ms"], 0)
            self.assertEqual(host["latency"]["last_success_at"], delivery["submitted_at"])

    def test_shipped_ui_has_clear_notice_local_assets_and_no_cookie(self):
        with preview() as (server, base):
            self.assertEqual(server.server_address[0], "127.0.0.1")
            code, headers, body = request(base, "/")
            self.assertEqual(code, 200)
            self.assertIn(demo.DEMO_NOTICE, body.decode())
            self.assertIn('/ui.js', body.decode())
            self.assertIn('/demo-guard.js', body.decode())
            self.assertIsNone(headers.get("Set-Cookie"))
            self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
            self.assertIn("connect-src 'self'", headers.get("Content-Security-Policy"))

    def test_compass_is_served_only_through_its_explicit_png_route(self):
        with preview() as (_, base):
            code, headers, body = request(base, "/agent-captain-mark.png")
            self.assertEqual(code, 200)
            self.assertEqual(headers.get("Content-Type"), "image/png")
            self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
            self.assertIsNone(headers.get("Set-Cookie"))
            canonical = Path(demo.__file__).resolve().parents[1] / "assets/brand/agentcaptain-mark.png"
            self.assertEqual(body, canonical.read_bytes())

    def test_read_endpoints_use_only_fixed_fixture_identities(self):
        with preview() as (_, base):
            code, _, body = request(base, "/api/state")
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(body)["agents"][0]["thread_id"], "demo-release-review")
            code, _, body = request(base, "/api/agents/read", {"host": "demo-build", "thread_id": "demo-release-review"})
            self.assertEqual(code, 200)
            self.assertTrue(json.loads(body)["demo"])
            self.assertIn("Synthetic demo fixtures", json.loads(body)["note"])
            self.assertEqual(request(base, "/api/agents/read", {"host": "private-machine", "thread_id": "unknown"})[0], 403)
            self.assertEqual(request(base, "/api/controllers/get?id=real-session")[0], 404)

    def test_all_task_and_lifecycle_mutations_are_rejected(self):
        with preview() as (server, base):
            initial = demo.fixture_state()
            for path in ["/api/hosts/connect", "/api/hosts/upsert", "/api/agents/update", "/api/agents/reconnect", "/api/agents/reconcile-reconnect", "/api/agents/reconcile-original-worker", "/api/agents/create", "/api/agents/message", "/api/agents/activate", "/api/agents/attach", "/api/agents/bind-output", "/api/agents/discover", "/api/terminals/create", "/api/terminals/input", "/api/terminals/close", "/api/controllers/prepare", "/api/controllers/start", "/api/controllers/resume", "/api/controllers/desktop", "/api/controllers/input", "/api/controllers/scope", "/api/controllers/close", "/api/terminal-service/recover", "/api/directories/list"]:
                with self.subTest(path=path):
                    self.assertEqual(request(base, path, {"host": "demo-build", "thread_id": "demo-release-review", "text": "do not send"})[0], 403)
            self.assertEqual(server.geometry, {})
            self.assertEqual(demo.fixture_state(), initial)

    def test_resize_changes_only_in_memory_demo_display_dimensions(self):
        with preview() as (server, base):
            code, _, body = request(base, "/api/terminals/resize", {"host": "demo-build", "thread_id": "demo-release-review", "cols": 88, "rows": 24})
            self.assertEqual(code, 200)
            self.assertTrue(json.loads(body)["fixture_only"])
            code, _, body = request(base, "/api/terminals/output?host=demo-build&thread_id=demo-release-review&after=0")
            frame = json.loads(body)
            self.assertEqual((frame["cols"], frame["rows"]), (88, 24))
            self.assertIn("Synthetic demo", frame["data"])
            self.assertEqual(request(base, "/api/terminals/resize", {"host": "demo-build", "thread_id": "demo-release-review", "cols": True, "rows": 24})[0], 400)
            self.assertEqual(request(base, "/api/terminals/output?host=real&thread_id=unknown")[0], 404)
            self.assertEqual(server.geometry, {"demo-release-review": (88, 24)})

    def test_unknown_files_foreign_origins_and_nonlocal_hosts_are_denied(self):
        with preview() as (_, base):
            for path in ["/.runtime/config.json", "/config.json", "/../service.py", "/assets/brand/../../config.json", "/.git/config", "/%2e%2e/config.json", "/agent-captain-mark.svg", "/assets/brand/agentcaptain-mark.png"]:
                self.assertEqual(request(base, path)[0], 404)
            self.assertEqual(request(base, "/api/state", headers={"Host": "untrusted.example"})[0], 403)
            self.assertEqual(request(base, "/api/agents/read", {}, headers={"Origin": "https://untrusted.example"})[0], 403)
            self.assertEqual(request(base, "/api/state", method="DELETE")[0], 403)

    def test_missing_explicit_assets_fail_closed_without_filesystem_fallback(self):
        with TemporaryDirectory() as directory, mock.patch.object(demo, "ROOT", Path(directory)):
            with preview() as (_, base):
                for path in ["/agent-captain-mark.png", "/vendor/xterm.js", "/style.css"]:
                    code, headers, body = request(base, path)
                    self.assertEqual(code, 404)
                    self.assertIn("application/json", headers["Content-Type"])
                    self.assertIn("Preview asset missing", json.loads(body)["error"])
                    self.assertNotIn(directory, body.decode())

    def test_invalid_identity_types_and_bounded_request_body_are_rejected(self):
        with preview() as (server, base):
            for host in [[], ["demo-build", "other"], {}, 3, "x" * 201]:
                self.assertEqual(request(base, "/api/agents/read", {"host": host, "thread_id": "demo-release-review"})[0], 403)
            self.assertEqual(request(base, "/api/agents/read", ["not-an-object"])[0], 400)
            self.assertEqual(request(base, "/api/agents/read", {"padding": "x" * (demo.MAX_BODY_BYTES + 1)})[0], 400)
            self.assertEqual(request(base, "/api/terminals/output?host=demo-build&host=other&thread_id=demo-release-review")[0], 404)
            self.assertEqual(server.geometry, {})

    def test_module_has_no_production_or_network_client_imports(self):
        # The preview has no forwarding/client primitive at all, even if a
        # request supplies a production URL, host alias or service descriptor.
        import ast
        source = Path(demo.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.add(node.module)
        self.assertEqual(imports, {"__future__", "argparse", "copy", "datetime", "http.server", "json", "pathlib", "urllib.parse"})

    def test_absolute_proxy_style_requests_are_never_forwarded(self):
        with preview() as (server, _):
            for method in ["GET", "POST"]:
                connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                try:
                    connection.request(method, "http://127.0.0.1:8766/api/state", headers={"Host": f"127.0.0.1:{server.server_port}"})
                    response = connection.getresponse()
                    self.assertEqual(response.status, 400)
                    self.assertIn("proxying is not supported", json.loads(response.read())["error"])
                finally:
                    connection.close()


if __name__ == "__main__":
    unittest.main()
