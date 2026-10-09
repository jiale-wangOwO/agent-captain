"""Isolated PRON Hub plugin generation/cache tests; no real Hub or Codex config."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("configure_pron_plugin", ROOT / "scripts" / "configure_plugin.py")
configure = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(configure)


class PluginPackagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pron-plugin-")
        self.base = Path(self.temp.name).resolve()
        self.clone = self.base / "clone with spaces"
        self.clone.mkdir()
        self.source_plugin = self.clone / "plugins" / "pron-hub"
        shutil.copytree(ROOT / "plugins" / "pron-hub", self.source_plugin)
        marketplace = self.clone / ".agents" / "plugins"
        marketplace.mkdir(parents=True)
        shutil.copyfile(ROOT / ".agents" / "plugins" / "marketplace.json", marketplace / "marketplace.json")
        shutil.copyfile(ROOT / "mcp_bridge.py", self.clone / "mcp_bridge.py")
        self.output = self.base / "retained local marketplace"

    def tearDown(self):
        self.temp.cleanup()

    def generate(self, output=None):
        return configure.configure_plugin(output or self.output, repo_root=self.clone)

    @staticmethod
    def load(path):
        return json.loads(Path(path).read_text(encoding="utf-8"))

    def test_source_identity_and_machine_independent_template(self):
        manifest = self.load(self.source_plugin / ".codex-plugin" / "plugin.json")
        self.assertEqual(manifest["name"], "pron-hub")
        self.assertEqual(manifest["version"], "0.5.0")
        self.assertEqual(manifest["interface"]["displayName"], "PRON Hub")
        self.assertEqual(self.load(self.source_plugin / ".mcp.json"), {"mcpServers": {}})
        catalog = self.load(self.clone / ".agents" / "plugins" / "marketplace.json")
        self.assertEqual(catalog["name"], "pron-hub-source")
        self.assertEqual(catalog["plugins"][0]["source"]["path"], "./plugins/pron-hub")
        skill = (self.source_plugin / "skills" / "pron-hub" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("name: pron-hub", skill)
        self.assertIn("用户保留最终决定权", skill)
        self.assertIn("content_complete", skill)

    def test_generated_command_uses_running_python_and_retained_bridge(self):
        result = self.generate()
        plugin = Path(result["plugin_root"])
        server = self.load(plugin / ".mcp.json")["mcpServers"]["pron_hub"]
        self.assertEqual(server["command"], str(Path(sys.executable).resolve()))
        self.assertEqual(server["args"], ["-u", str(plugin / "mcp_bridge.py")])
        self.assertEqual(server["env"], {"CEO_HUB_CONNECTION_FILE": str(self.clone / ".runtime" / "service.json")})
        self.assertEqual((plugin / "mcp_bridge.py").read_bytes(), (self.clone / "mcp_bridge.py").read_bytes())
        self.assertEqual(result["plugin"], "pron-hub@pron-hub-local")
        self.assertTrue(result["retain_generated_directory"])
        self.assertEqual(result["commands"][1], "codex plugin add pron-hub@pron-hub-local")
        generated_catalog = self.load(self.output / ".agents" / "plugins" / "marketplace.json")
        self.assertEqual(generated_catalog["name"], "pron-hub-local")
        self.assertNotEqual(generated_catalog["name"],
                            self.load(self.clone / ".agents" / "plugins" / "marketplace.json")["name"])
        self.assertNotIn("${PLUGIN_ROOT}", json.dumps(server))

    def test_default_output_is_checkout_runtime(self):
        result = configure.configure_plugin(repo_root=self.clone)
        self.assertEqual(Path(result["marketplace_root"]), self.clone / ".runtime" / "plugin-marketplace")

    def test_only_allowlisted_code_is_copied_no_live_descriptor_or_auth(self):
        # Fake fixtures, not actual user secrets. They must never enter a plugin.
        (self.source_plugin / "auth.json").write_text('{"token":"fixture-not-a-credential"}', encoding="utf-8")
        (self.clone / "service.json").write_text('{"token":"fixture-not-a-credential"}', encoding="utf-8")
        (self.clone / ".runtime").mkdir()
        (self.clone / ".runtime" / "terminal-broker.json").write_text("fixture-state", encoding="utf-8")
        result = self.generate()
        all_files = {path.relative_to(self.output).as_posix() for path in self.output.rglob("*") if path.is_file()}
        self.assertEqual(all_files, {
            ".pron-hub-generated.json", ".agents/plugins/marketplace.json",
            "plugins/pron-hub/.codex-plugin/plugin.json", "plugins/pron-hub/.mcp.json",
            "plugins/pron-hub/skills/pron-hub/SKILL.md", "plugins/pron-hub/README.md",
            "plugins/pron-hub/mcp_bridge.py",
        })
        generated_config = self.load(Path(result["plugin_root"]) / ".mcp.json")
        self.assertEqual(generated_config["mcpServers"]["pron_hub"]["env"],
                         {"CEO_HUB_CONNECTION_FILE": str(self.clone / ".runtime" / "service.json")})
        self.assertNotIn("fixture-not-a-credential", json.dumps(generated_config))
        self.assertEqual(self.load(self.output / ".pron-hub-generated.json")["generator"], "pron-hub-configure-plugin")

    def test_existing_unknown_directory_preserved(self):
        self.output.mkdir()
        evidence = self.output / "user-data.txt"
        evidence.write_text("preserve me", encoding="utf-8")
        with self.assertRaises(configure.ConfigurationError):
            self.generate()
        self.assertEqual(evidence.read_text(encoding="utf-8"), "preserve me")
        self.assertEqual(list(self.output.iterdir()), [evidence])

    def test_existing_empty_directory_refused(self):
        self.output.mkdir()
        with self.assertRaises(configure.ConfigurationError):
            self.generate()
        self.assertEqual(list(self.output.iterdir()), [])

    def test_existing_file_preserved(self):
        self.output.write_text("original file", encoding="utf-8")
        with self.assertRaises(configure.ConfigurationError):
            self.generate()
        self.assertEqual(self.output.read_text(encoding="utf-8"), "original file")

    def test_repeat_generation_never_overwrites_known_output(self):
        self.generate()
        before = {path.relative_to(self.output): path.read_bytes() for path in self.output.rglob("*") if path.is_file()}
        with self.assertRaises(configure.ConfigurationError):
            self.generate()
        after = {path.relative_to(self.output): path.read_bytes() for path in self.output.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_new_output_supports_safe_update(self):
        self.generate()
        another = self.base / "marketplace-v2"
        result = self.generate(another)
        self.assertEqual(Path(result["marketplace_root"]), another)
        self.assertTrue(self.output.is_dir())

    def test_machine_specific_source_mcp_refused_before_output(self):
        (self.source_plugin / ".mcp.json").write_text(json.dumps({"mcpServers": {
            "unreviewed": {"command": "/some/private/path", "env": {"secret": "fixture"}}
        }}), encoding="utf-8")
        with self.assertRaises(configure.ConfigurationError):
            self.generate()
        self.assertFalse(self.output.exists())

    def test_invalid_manifest_refused_before_output(self):
        manifest = self.source_plugin / ".codex-plugin" / "plugin.json"
        data = self.load(manifest)
        data["mcpServers"] = "../outside.json"
        manifest.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(configure.ConfigurationError):
            self.generate()
        self.assertFalse(self.output.exists())

    def test_invalid_marketplace_entry_refused_before_output(self):
        path = self.clone / ".agents" / "plugins" / "marketplace.json"
        path.write_text('{"name":"pron-hub-source","plugins":[null]}', encoding="utf-8")
        with self.assertRaises(configure.ConfigurationError):
            self.generate()
        self.assertFalse(self.output.exists())

    def test_missing_bridge_refused_before_output(self):
        (self.clone / "mcp_bridge.py").unlink()
        with self.assertRaises(configure.ConfigurationError):
            self.generate()
        self.assertFalse(self.output.exists())

    def test_unavailable_interpreter_refused_before_output(self):
        with self.assertRaises(configure.ConfigurationError):
            configure.configure_plugin(self.output, repo_root=self.clone,
                                       python_executable=self.base / "missing-python")
        self.assertFalse(self.output.exists())

    def test_dangling_output_symlink_refused(self):
        try:
            self.output.symlink_to(self.base / "missing-output", target_is_directory=True)
        except OSError:
            self.skipTest("Platform does not permit test symlinks")
        with self.assertRaises(configure.ConfigurationError):
            self.generate()
        self.assertTrue(self.output.is_symlink())
        self.assertFalse((self.base / "missing-output").exists())

    def test_cached_plugin_can_initialize_and_list_tools_from_unrelated_cwd(self):
        result = self.generate()
        # Mimic a host copying this package into an installation cache. The
        # absolute bridge reference remains valid without plugin-root expansion.
        cache = self.base / "isolated codex cache" / "pron-hub-local" / "pron-hub" / "0.5.0"
        shutil.copytree(result["plugin_root"], cache)
        cwd = self.base / "unrelated working directory"
        cwd.mkdir()
        # Demonstrate that no imports/resources rely on the original clone.
        moved_clone = self.base / "moved original clone"
        self.clone.rename(moved_clone)
        server = self.load(cache / ".mcp.json")["mcpServers"]["pron_hub"]
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "packaging-test", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ]
        env = dict(os.environ)
        env["CEO_HUB_CONNECTION_FILE"] = str(self.base / "deliberately-missing-service.json")
        env["PRON_HUB_CONNECTION_FILE"] = str(self.base / "deliberately-missing-service.json")
        process = subprocess.run([server["command"], *server["args"]], cwd=cwd, env=env,
                                 input="\n".join(json.dumps(message) for message in messages) + "\n",
                                 text=True, encoding="utf-8", capture_output=True, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stderr, "")
        responses = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual([response["id"] for response in responses], [1, 2])
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2025-11-25")
        names = {tool["name"] for tool in responses[1]["result"]["tools"]}
        self.assertEqual(names, {"list_active_agents", "get_agent", "read_agent", "send_agent_message",
                                 "create_agent", "send_terminal_input", "poll_events", "get_delivery"})
        self.assertFalse((self.base / "deliberately-missing-service.json").exists())
        self.assertTrue(Path(server["args"][1]).is_file())

    def test_cli_generates_json_without_registering_or_installing(self):
        target = self.base / "cli marketplace"
        process = subprocess.run([sys.executable, str(ROOT / "scripts" / "configure_plugin.py"),
                                  "--output", str(target), "--json"], text=True, encoding="utf-8",
                                 capture_output=True, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stderr, "")
        result = json.loads(process.stdout)
        self.assertEqual(Path(result["marketplace_root"]), target)
        self.assertEqual(result["plugin"], "pron-hub@pron-hub-local")
        self.assertTrue(result["retain_generated_directory"])

    def test_cli_refuses_existing_target_with_nonzero_exit(self):
        self.output.mkdir()
        process = subprocess.run([sys.executable, str(ROOT / "scripts" / "configure_plugin.py"),
                                  "--output", str(self.output)], text=True, encoding="utf-8",
                                 capture_output=True, timeout=10)
        self.assertEqual(process.returncode, 1)
        self.assertEqual(process.stdout, "")
        self.assertIn("Output already exists", process.stderr)
        self.assertEqual(list(self.output.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
