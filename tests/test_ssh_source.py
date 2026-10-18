"""Portable compressed stdin executes locally; no SSH, services or real state."""
import ast
import base64
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib

from agent_captain.core.terminal_hub import Hub
from agent_captain.paths import source_path
from agent_captain.transport.ssh_source import pack_python_source
from tests.test_terminal_hub import FakeTerminals


def unpack_source(packed):
    """Inspect the loader's literal payload without executing its contents."""
    loader = ast.parse(packed).body[-1].value
    literal = loader.args[0].args[0].args[0].args[0]
    return zlib.decompress(base64.b64decode(ast.literal_eval(literal), validate=True)).decode("utf-8")


class PythonSourcePackingTests(unittest.TestCase):
    def run_source(self, source, args=()):
        environment = dict(os.environ, PYTHONIOENCODING="utf-8")
        return subprocess.run([sys.executable, "-", *args], input=pack_python_source(source),
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10, env=environment)

    def test_unicode_quotes_and_large_source_stay_in_stdin_not_command_arguments(self):
        text = "中文🙂 ' \" $() ` ; \\ \n" * 12000
        source = "value=" + repr(text) + "\nassert len(value)==" + str(len(text)) + "\nprint('complete')\n"
        packed = pack_python_source(source)
        self.assertEqual(unpack_source(packed), source)
        self.assertTrue(packed.isascii())
        self.assertLess(len(packed), len(source) // 10)
        result = self.run_source(source)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "complete\n")

    def test_original_argv_filename_main_name_and_stdin_eof_are_preserved(self):
        arguments = ["quoted'\"value", "中文🙂", "--not-a-python-option", "$(not-executed);`literal`", ""]
        source = "import json,sys\nprint(json.dumps([sys.argv,__file__,__name__,sys.stdin.read()],ensure_ascii=False))\n"
        result = self.run_source(source, arguments)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [["-", *arguments], "<stdin>", "__main__", ""])

    def test_future_directive_and_exit_stderr_semantics_are_preserved(self):
        source = "from __future__ import annotations\nimport sys\nprint('before exit')\nprint('diagnostic',file=sys.stderr)\nraise SystemExit(7)\n"
        result = self.run_source(source)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout, "before exit\n")
        self.assertEqual(result.stderr, "diagnostic\n")

    def test_source_traceback_keeps_stdin_filename(self):
        result = self.run_source("raise ValueError('bounded-error')\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn('File "<stdin>"', result.stderr)
        self.assertIn("ValueError: bounded-error", result.stderr)

    def test_only_text_source_is_accepted(self):
        for value in (None, b"print('bytes')", 1):
            with self.subTest(value=value), self.assertRaises(TypeError):
                pack_python_source(value)

    def test_original_remote_probe_with_isolated_missing_tmux_keeps_result_and_exit(self):
        source = source_path("transport", "remote_pty_bridge.py").read_text(encoding="utf-8")
        # Inject only a bounded subprocess result. The complete unmodified probe
        # module still parses its actual argv, validates the terminal and emits
        # its own marker; no tmux or remote command runs in this test.
        missing_tmux = ("import os,subprocess,sys,types\n"
                        "if os.name=='nt':\n"
                        " sys.modules['fcntl']=types.ModuleType('fcntl')\n"
                        " sys.modules['termios']=types.ModuleType('termios')\n"
                        "subprocess.run=lambda *a,**kw:types.SimpleNamespace(returncode=1)\n")
        prefix = source.index("from __future__ import annotations\n") + len("from __future__ import annotations\n")
        source = source[:prefix] + missing_tmux + source[prefix:]
        terminal = "ceohub-0123456789abcdef01234567"
        result = self.run_source(source, ["probe", "--session", terminal])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout.removeprefix("CEO_HUB_PTY_RESULT=")),
                         {"ok": True, "exists": False, "reason": "session_missing"})


class CombinedCatalogSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve() / "private-test-home"
        self.rollout = self.home / "sessions" / "selected.jsonl"
        self.rollout.parent.mkdir(parents=True)
        self.session = "11111111-2222-4333-8444-555555555555"
        records = [{"type": "session_meta", "payload": {"id": self.session, "cwd": "/test-project"}},
                   {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "完整输出 ' \" $() ` 🙂"}]}}]
        self.rollout.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records), encoding="utf-8")
        self.terminals = FakeTerminals()
        self.hub = Hub(Path(self.temp.name) / "hub", terminals=self.terminals)
        self.hub._stop.set()
        self.hub._watcher.join(3)
        self.hub.hosts = {"remote": {"transport": "ssh", "target": "isolated-test-alias", "label": "Remote"}}
        self.addCleanup(self.hub.close)

    def test_complete_catalog_and_two_portable_modules_execute_through_packed_stdin(self):
        actual_run = subprocess.run
        dispatched = []
        def local_python(args, **kwargs):
            self.assertEqual(args[-2], "isolated-test-alias")
            remote = shlex.split(args[-1])
            self.assertEqual(remote[:2], ["python3", "-"])
            self.assertLess(len(args[-1]), 1024, "Source does not enter SSH command arguments")
            source = unpack_source(kwargs["input"])
            self.assertIn("types.ModuleType('session_binding')", source)
            self.assertIn("types.ModuleType('conversation_output')", source)
            self.assertIn("def main(request)", source)
            dispatched.append((source, kwargs["input"]))
            return actual_run([sys.executable, *remote[1:]], env=dict(os.environ, PYTHONIOENCODING="utf-8"), **kwargs)
        request = {"op": "read_output", "session_id": self.session, "rollout_path": str(self.rollout), "codex_home": str(self.home)}
        with patch.dict(os.environ, {"CODEX_HOME": str(self.home)}), \
                patch("agent_captain.core.terminal_hub.subprocess.run", side_effect=local_python):
            result = self.hub.history("remote", request)
        self.assertEqual(result["source"], "codex_rollout")
        self.assertEqual(result["messages"][0]["text"], "完整输出 ' \" $() ` 🙂")
        self.assertTrue(result["history_complete"])
        self.assertEqual(len(dispatched), 1)
        self.assertLess(len(dispatched[0][1]), len(dispatched[0][0]))
        self.assertEqual(self.terminals.writes, [])
        self.assertFalse(any(call[0] == "create" for call in self.terminals.calls))


if __name__ == "__main__":
    unittest.main()
