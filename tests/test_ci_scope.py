"""Offline CI-routing fixtures; no GitHub credentials or remote state are used."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "ci_scope.py"
SPEC = importlib.util.spec_from_file_location("ci_scope", SOURCE)
ci_scope = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci_scope)
BASE, HEAD = "a" * 40, "b" * 40


class CiScopeTests(unittest.TestCase):
    def test_only_static_documentation_and_branding_can_skip_matrix(self):
        for path in (
            "README.md", "README.zh-CN.md", "CONTRIBUTING.md", "LICENSE",
            "docs/guides/operator-guide.md", "docs/images/workspace-demo.jpg",
            "assets/brand/agentcaptain-mark.png", "assets/brand/README.md",
            ".github/PULL_REQUEST_TEMPLATE.md",
        ):
            with self.subTest(path=path):
                self.assertTrue(ci_scope.documentation_only(path))

    def test_code_configuration_tests_and_unknown_paths_require_full_matrix(self):
        for path in (
            "src/agent_captain/app.py", "terminal/terminal_broker.cjs", "web/app.js",
            "tests/test_publication.py", "scripts/check_publication.py", "scripts/README.md",
            ".github/workflows/ci.yml", ".github/ISSUE_TEMPLATE/config.yml",
            ".agents/plugins/marketplace.json", "pyproject.toml", "package-lock.json",
            "AGENTS.md", "unknown-root.md", "docs/AGENTS.md", "docs/CLAUDE.md",
            "docs/SKILL.md", "assets/brand/AGENTS.md", "plugins/skills/example/SKILL.md",
            ".agents/instructions.md", "plugins/skills/example/README.md",
            "docs/example.py", "docs/images/example.html", "assets/brand/generate.py",
            "other/picture.png", "web/logo.svg", "docs/../src/example.md", "",
        ):
            with self.subTest(path=path):
                self.assertFalse(ci_scope.documentation_only(path))

    def test_nul_delimited_filenames_and_mixed_changes(self):
        for paths, expected in (
            (b"README.md\0docs/a file.md\0docs/a\nfile.md\0", False),
            (b"README.md\0src/example.py\0", True),
            (b"", True),
            (b"docs/\xff.md\0", True),
        ):
            with self.subTest(paths=paths), patch.object(ci_scope.subprocess, "run") as run:
                run.return_value.stdout = paths
                full, _ = ci_scope.select_scope(Path("."), "push", BASE, HEAD)
                self.assertEqual(full, expected)
                self.assertIn("--no-renames", run.call_args.args[0])
                self.assertIn("-z", run.call_args.args[0])

    def test_manual_new_branch_and_invalid_inputs_fail_closed_without_git(self):
        for event, base, head in (
            ("workflow_dispatch", BASE, HEAD), ("unknown", BASE, HEAD),
            ("push", "0" * 40, HEAD), ("push", "", HEAD),
            ("pull_request", "--output=anything", HEAD), ("push", BASE, "--help"),
        ):
            with self.subTest(event=event, base=base), patch.object(ci_scope.subprocess, "run") as run:
                self.assertTrue(ci_scope.select_scope(Path("."), event, base, head)[0])
                run.assert_not_called()

    def test_unavailable_history_or_timed_out_git_runs_full_tests(self):
        for failure in (OSError(), subprocess.CalledProcessError(1, "git"), subprocess.TimeoutExpired("git", 30)):
            with self.subTest(failure=type(failure)), patch.object(ci_scope.subprocess, "run", side_effect=failure):
                self.assertEqual(ci_scope.select_scope(Path("."), "push", BASE, HEAD), (True, "diff_unavailable"))

    def test_real_git_docs_then_code_then_code_renamed_into_docs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            def git(*args):
                return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True).stdout.decode().strip()
            def commit():
                git("add", "--all")
                git("-c", "user.name=CI Fixture", "-c", "user.email=ci@example.invalid", "commit", "-qm", "fixture")
                return git("rev-parse", "HEAD")
            git("init", "-q")
            (root / "README.md").write_text("Initial", encoding="utf-8")
            first = commit()
            (root / "README.md").write_text("Documentation update", encoding="utf-8")
            docs = commit()
            self.assertFalse(ci_scope.select_scope(root, "push", first, docs)[0])
            self.assertFalse(ci_scope.select_scope(root, "pull_request", first, docs)[0])
            (root / "example.py").write_text("print('fixture')", encoding="utf-8")
            code = commit()
            self.assertTrue(ci_scope.select_scope(root, "push", docs, code)[0])
            (root / "example.py").rename(root / "example.md")
            renamed = commit()
            self.assertTrue(ci_scope.select_scope(root, "push", code, renamed)[0])


if __name__ == "__main__":
    unittest.main()
