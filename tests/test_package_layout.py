"""Offline architecture and checkout-migration contracts.

These tests use temporary checkouts and inert imports. They never start the
real web service, a broker, SSH, a model, or inspect personal configuration.
"""
from __future__ import annotations

import ast
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "agent_captain"
COMPATIBILITY_FILES = {
    "app.py", "service.py", "mcp_bridge.py", "terminal_service.py",
    "process_identity.py", "remote_pty_bridge.py", "remote_foreground_probe.py",
    "terminal_broker.cjs",
}
MODULES = (
    "core.controllers", "core.management_policy", "core.terminal_hub",
    "infrastructure.background_process", "infrastructure.process_identity", "infrastructure.store",
    "integrations.mcp_bridge",
    "server.app", "server.service", "sessions.catalog", "sessions.conversation_output",
    "sessions.session_binding", "transport.directory_browser", "transport.remote_foreground_probe",
    "transport.remote_terminal_history", "transport.ssh_source", "transport.terminal_client", "transport.terminal_service",
)


class PackageLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agent-captain-layout-")
        self.base = Path(self.temp.name).resolve()
        self.environment = dict(os.environ)
        self.environment["PYTHONPATH"] = str(ROOT / "src")
        self.environment["CODEX_HOME"] = str(self.base / "empty-codex-home")
        self.environment["PYTHONIOENCODING"] = "utf-8"

    def tearDown(self):
        self.temp.cleanup()

    def python(self, arguments, *, source=None, environment=None):
        return subprocess.run(
            [sys.executable, "-X", "utf8", *arguments], input=source, cwd=self.base,
            env=environment or self.environment, text=True, encoding="utf-8",
            capture_output=True, timeout=25,
        )

    def test_business_modules_are_grouped_not_flattened(self):
        for group in ("core", "server", "sessions", "transport", "infrastructure", "integrations"):
            with self.subTest(group=group):
                self.assertTrue((PACKAGE / group / "__init__.py").is_file())
        self.assertLessEqual(
            {path.name for path in PACKAGE.glob("*.py")},
            {"__init__.py", "__main__.py", "cli.py", "paths.py"},
        )
        for module in MODULES:
            with self.subTest(module=module):
                self.assertTrue((PACKAGE / (module.replace(".", "/") + ".py")).is_file())
        # A deployed old broker can temporarily retain explicit ignored entry
        # points, but business implementations must not return to the root.
        root_source = {path.name for path in ROOT.iterdir() if path.suffix in (".py", ".cjs")}
        self.assertLessEqual(root_source, COMPATIBILITY_FILES)
        self.assertTrue((ROOT / "terminal" / "terminal_broker.cjs").is_file())
        self.assertTrue((ROOT / "terminal" / "input_activity.cjs").is_file())

    def test_imports_from_an_unrelated_directory_are_inert(self):
        program = """import importlib, json, socket, subprocess
def forbidden(*args, **kwargs):
    raise AssertionError('Import unexpectedly started a process or connection')
subprocess.Popen = forbidden
socket.create_connection = forbidden
modules = %r
loaded = [importlib.import_module('agent_captain.' + name) for name in modules]
print(json.dumps([module.__file__ for module in loaded]))
""" % (MODULES,)
        result = self.python(["-c", program])
        self.assertEqual(result.returncode, 0, result.stderr)
        locations = [Path(path).resolve() for path in json.loads(result.stdout)]
        self.assertEqual(len(locations), len(MODULES))
        self.assertTrue(all(PACKAGE.resolve() in path.parents for path in locations))

    def test_private_state_and_assets_remain_anchored_to_checkout(self):
        program = """import json
from agent_captain.paths import ROOT, PACKAGE_ROOT, TERMINAL_ROOT, BROKER_SCRIPT, LEGACY_BROKER_SCRIPT
from agent_captain.server.app import connection_file
print(json.dumps([str(value) for value in (ROOT, PACKAGE_ROOT, TERMINAL_ROOT, BROKER_SCRIPT, LEGACY_BROKER_SCRIPT)]))
"""
        result = self.python(["-c", program])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([Path(value).resolve() for value in json.loads(result.stdout)], [
            ROOT, PACKAGE, ROOT / "terminal", ROOT / "terminal" / "terminal_broker.cjs",
            ROOT / "terminal_broker.cjs",
        ])
        for relative in ("web/index.html", "assets/brand/agentcaptain-mark.png", "config.example.json"):
            self.assertTrue((ROOT / relative).is_file())

    def test_checkout_dispatcher_help_outside_cwd_and_isolated_mode(self):
        for arguments in (("--help",), ("service", "--help"), ("terminal", "--help"), ("serve", "--help")):
            with self.subTest(arguments=arguments):
                result = self.python(["-I", str(ROOT / "scripts" / "captain.py"), *arguments])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("usage:", result.stdout.lower())
                self.assertNotIn("Traceback", result.stderr)

    def test_module_entrypoint_help_outside_cwd(self):
        result = self.python(["-m", "agent_captain", "--help"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("service", result.stdout)
        self.assertIn("terminal", result.stdout)

    def test_project_metadata_uses_src_and_checkout_entrypoint(self):
        metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(metadata["project"]["name"], "agent-captain")
        self.assertIn("agent-captain", metadata["project"]["scripts"])
        self.assertEqual(metadata["tool"]["setuptools"]["packages"]["find"]["where"], ["src"])
        self.assertFalse(metadata["project"].get("dependencies", []), "The Python runtime stays stdlib-only")
        self.assertEqual(metadata["project"].get("dynamic", []), [], "Build metadata must not import the running application")
        self.assertEqual(metadata["build-system"]["build-backend"], "setuptools.build_meta")

    def test_isolated_background_helper_can_bootstrap_its_package(self):
        result = self.python(["-I", str(PACKAGE / "infrastructure" / "background_process.py"), "--help"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage:", result.stdout.lower())

    def test_broker_entrypoint_identity_accepts_only_two_exact_checkout_paths(self):
        from agent_captain.transport.terminal_client import broker_script
        for script in (ROOT / "terminal" / "terminal_broker.cjs", ROOT / "terminal_broker.cjs"):
            identity = {"birth": "win-filetime:1234", "executable": str(self.base / "node.exe"), "script": str(script)}
            self.assertEqual(broker_script(identity), script)
        for script in (self.base / "terminal_broker.cjs", ROOT / "terminal" / "other.cjs"):
            with self.subTest(script=script):
                identity = {"birth": "win-filetime:1234", "executable": str(self.base / "node.exe"), "script": str(script)}
                with self.assertRaises(ValueError):
                    broker_script(identity)

    def test_portable_catalog_runs_from_stdin_without_package(self):
        payload = base64.urlsafe_b64encode(json.dumps({"op": "find", "limit": 1}).encode()).decode()
        source = (PACKAGE / "sessions" / "catalog.py").read_text(encoding="utf-8")
        environment = dict(self.environment)
        environment.pop("PYTHONPATH", None)
        result = self.python(["-I", "-", payload], source=source, environment=environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        reply = json.loads(result.stdout)
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["result"]["threads"], [])
        self.assertFalse(reply["result"]["indexed"])

    def test_catalog_remote_bundle_keeps_bare_module_loading_working(self):
        home = self.base / "empty indexed home"
        home.mkdir()
        request = {"op": "resolve_output", "session_id": "00000000-0000-4000-8000-000000000000", "codex_home": str(home)}
        payload = base64.urlsafe_b64encode(json.dumps(request).encode()).decode()
        source = "import sys,types\n"
        for name in ("session_binding", "conversation_output"):
            code = (PACKAGE / "sessions" / (name + ".py")).read_text(encoding="utf-8")
            source += f"_module=types.ModuleType({name!r});sys.modules[{name!r}]=_module;exec(compile({code!r},{name!r},'exec'),_module.__dict__)\n"
        source += (PACKAGE / "sessions" / "catalog.py").read_text(encoding="utf-8").replace("from __future__ import annotations\n", "", 1)
        result = self.python(["-I", "-", payload], source=source)
        self.assertEqual(result.returncode, 0, result.stderr)
        reply = json.loads(result.stdout)
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["result"], {"state": "unbound", "reason": "session_not_indexed"})

    def test_portable_directory_reader_needs_no_package(self):
        listing = self.base / "directory with spaces"
        (listing / "one child").mkdir(parents=True)
        payload = base64.urlsafe_b64encode(json.dumps({"path": str(listing)}).encode()).decode()
        source = (PACKAGE / "transport" / "directory_browser.py").read_text(encoding="utf-8")
        result = self.python(["-I", "-", payload], source=source)
        self.assertEqual(result.returncode, 0, result.stderr)
        prefix = "CEO_HUB_DIRECTORY_RESULT="
        self.assertTrue(result.stdout.startswith(prefix), result.stdout)
        reply = json.loads(result.stdout[len(prefix):])
        self.assertTrue(reply["ok"], reply)
        self.assertEqual([row["name"] for row in reply["result"]["directories"]], ["one child"])

    def test_remote_helpers_remain_package_independent(self):
        # Linux-only imports such as fcntl are intentionally parsed, not
        # imported on the Windows test runner. No tmux/process probe executes.
        for relative in ("sessions/session_binding.py", "sessions/conversation_output.py",
                         "transport/remote_pty_bridge.py", "transport/remote_foreground_probe.py",
                         "transport/remote_terminal_history.py", "transport/directory_browser.py"):
            with self.subTest(relative=relative):
                tree = ast.parse((PACKAGE / relative).read_text(encoding="utf-8"))
                package_imports = [node for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom) and (node.level or (node.module or "").startswith("agent_captain"))]
                self.assertEqual(package_imports, [], "Remote source must run without installing this package")


class LegacyEntrypointMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agent-captain-migration-")
        self.base = Path(self.temp.name).resolve()
        self.clone = self.base / "retained checkout with spaces"
        self.clone.mkdir()
        shutil.copytree(ROOT / "src", self.clone / "src", ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
        shutil.copytree(ROOT / "scripts", self.clone / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(ROOT / "terminal", self.clone / "terminal", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copyfile(ROOT / "pyproject.toml", self.clone / "pyproject.toml")
        (self.clone / "web").mkdir()
        shutil.copyfile(ROOT / "web" / "index.html", self.clone / "web" / "index.html")
        self.script = ROOT / "scripts" / "prepare_legacy_entrypoints.py"

    def tearDown(self):
        self.temp.cleanup()

    def migrate(self, *arguments):
        return subprocess.run([sys.executable, "-I", str(self.script), "--root", str(self.clone), *arguments],
            cwd=self.base, stdin=subprocess.DEVNULL, capture_output=True,
            text=True, encoding="utf-8", timeout=25)

    def root_sources(self):
        return {path.name for path in self.clone.iterdir() if path.suffix in (".py", ".cjs")}

    def test_compatibility_materialization_is_explicit(self):
        result = self.migrate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.root_sources(), set())
        self.assertFalse((self.clone / ".runtime").exists())

    def test_apply_creates_only_reviewed_compatibility_files_and_is_idempotent(self):
        result = self.migrate("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.root_sources(), COMPATIBILITY_FILES)
        for name in ("remote_pty_bridge.py", "remote_foreground_probe.py"):
            self.assertEqual((self.clone / name).read_bytes(), (PACKAGE / "transport" / name).read_bytes())
        before = {name: (self.clone / name).read_bytes() for name in COMPATIBILITY_FILES}
        repeated = self.migrate("--apply")
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual(before, {name: (self.clone / name).read_bytes() for name in COMPATIBILITY_FILES})
        self.assertFalse((self.clone / ".runtime").exists(), "Migration must not create, read or rewrite service state")

    def test_unknown_existing_file_is_not_overwritten_or_partially_migrated(self):
        existing = self.clone / "service.py"
        existing.write_text("# unrelated user-owned file\n", encoding="utf-8")
        result = self.migrate("--apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(existing.read_text(encoding="utf-8"), "# unrelated user-owned file\n")
        self.assertEqual(self.root_sources(), {"service.py"})

    def test_incomplete_installation_fails_closed_without_creating_private_state(self):
        (self.clone / "web" / "index.html").unlink()
        process = subprocess.run([sys.executable, "-I", "-X", "utf8", str(self.clone / "scripts" / "captain.py"), "mcp"],
            cwd=self.base, stdin=subprocess.DEVNULL, capture_output=True,
            text=True, encoding="utf-8", timeout=25)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("retained source checkout", process.stderr)
        self.assertFalse((self.clone / ".runtime").exists())
        self.assertFalse((self.base / ".runtime").exists())

    def test_generated_python_entries_bootstrap_without_cwd_or_pythonpath(self):
        result = self.migrate("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        for name in ("app.py", "service.py", "terminal_service.py"):
            with self.subTest(name=name):
                process = subprocess.run([sys.executable, "-I", "-X", "utf8", str(self.clone / name), "--help"],
                    cwd=self.base, env=environment, stdin=subprocess.DEVNULL,
                    capture_output=True, text=True, encoding="utf-8", timeout=25)
                self.assertEqual(process.returncode, 0, process.stderr)
                self.assertIn("usage:", process.stdout.lower())
        identity = subprocess.run([sys.executable, "-I", "-X", "utf8", str(self.clone / "process_identity.py"), "observe", "0"],
            cwd=self.base, env=environment, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, encoding="utf-8", timeout=25)
        self.assertEqual(identity.returncode, 0, identity.stderr)
        self.assertEqual(json.loads(identity.stdout), {"state": "unknown"})

    def test_generated_mcp_entry_initializes_without_service_or_credentials(self):
        result = self.migrate("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "isolated-layout-test", "version": "1"}}}
        process = subprocess.run([sys.executable, "-I", "-X", "utf8", str(self.clone / "mcp_bridge.py")],
            cwd=self.base, input=json.dumps(request) + "\n", capture_output=True,
            text=True, encoding="utf-8", timeout=25)
        self.assertEqual(process.returncode, 0, process.stderr)
        reply = json.loads(process.stdout)
        self.assertEqual(reply["id"], 1)
        self.assertEqual(reply["result"]["protocolVersion"], "2025-03-26")
        self.assertFalse((self.clone / ".runtime").exists())


if __name__ == "__main__":
    unittest.main()
