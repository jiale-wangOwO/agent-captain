"""Publication checker fixtures are synthetic; no real runtime state is opened."""
from __future__ import annotations

import contextlib
import importlib.util
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "check_publication.py"
SPEC = importlib.util.spec_from_file_location("publication_check", SOURCE)
publication = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = publication
SPEC.loader.exec_module(publication)


class PublicationTests(unittest.TestCase):
    def test_high_confidence_credentials_never_appear_in_cli_output(self):
        token = "gh" + "p_" + "A" * 32
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "source.py").write_text("token = " + repr(token), encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = publication.main(["--root", str(root)])
            self.assertEqual(result, 1)
            self.assertIn("source.py:1: github_token", output.getvalue())
            self.assertNotIn(token, output.getvalue())

    def test_private_state_is_excluded_without_opening_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for private in (".runtime", ".schemas", "node_modules", ".agents"):
                (root / private).mkdir()
                (root / private / "unreadable.txt").touch()
            (root / "auth.json").touch()
            (root / "README.md").write_text("Public example", encoding="utf-8")
            original = Path.read_bytes
            reads = []
            def guarded(path):
                reads.append(path.relative_to(root).as_posix())
                return original(path)
            with patch.object(Path, "read_bytes", guarded):
                findings, checked, _ = publication.check(root)
            self.assertEqual(findings, [])
            self.assertEqual(checked, 1)
            self.assertEqual(reads, ["README.md"])

    def test_tracked_private_artifact_is_rejected_without_reading_blob(self):
        with tempfile.TemporaryDirectory() as temporary:
            calls = []
            def git(root, *arguments):
                calls.append(arguments)
                if arguments[0] == "ls-files":
                    return b".runtime/connection.json\0auth.json\0README.md\0"
                self.assertEqual(arguments, ("show", ":README.md"))
                return b"Public example"
            with patch.object(publication, "git_command", git):
                findings, checked, _ = publication.check(Path(temporary), tracked=True)
            self.assertEqual({finding.category for finding in findings}, {"private_artifact"})
            self.assertEqual(checked, 1)
            self.assertEqual(len(calls), 2)

    def test_only_canonical_plugin_catalog_is_opened_under_agents(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plugins = root / ".agents" / "plugins"
            plugins.mkdir(parents=True)
            catalog = plugins / "marketplace.json"
            catalog.write_text('{"name":"example-source","plugins":[]}', encoding="utf-8")
            (plugins / "private.json").touch()
            (root / ".agents" / "private-state.json").touch()
            (root / ".agents" / "private-cache").mkdir()
            (root / ".agents" / "private-cache" / "state.txt").touch()
            reads = []
            original = Path.read_bytes
            def guarded(path):
                reads.append(path.relative_to(root).as_posix())
                return original(path)
            with patch.object(Path, "read_bytes", guarded):
                findings, checked, _ = publication.check(root)
            self.assertEqual(findings, [])
            self.assertEqual(checked, 1)
            self.assertEqual(reads, [publication.PUBLIC_SOURCE_METADATA])

    def test_tracked_catalog_is_scanned_but_other_agents_state_is_not_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            calls = []
            catalog = publication.PUBLIC_SOURCE_METADATA
            def git(root, *arguments):
                calls.append(arguments)
                if arguments == ("ls-files", "--cached", "-z"):
                    return (catalog + "\0.agents/plugins/private.json\0.agents/private-state.json\0").encode()
                if arguments == ("ls-files", "--stage", "-z", "--", catalog):
                    return ("100644 " + "0" * 40 + " 0\t" + catalog + "\0").encode()
                self.assertEqual(arguments, ("show", ":" + catalog))
                return b'{"name":"example-source","plugins":[]}'
            with patch.object(publication, "git_command", git):
                findings, checked, _ = publication.check(Path(temporary), tracked=True)
            self.assertEqual({finding.category for finding in findings}, {"private_artifact"})
            self.assertEqual({finding.path for finding in findings}, {".agents/plugins/private.json", ".agents/private-state.json"})
            self.assertEqual(checked, 1)
            self.assertEqual(len(calls), 3)

    def test_tracked_catalog_symlink_is_rejected_without_reading_blob(self):
        with tempfile.TemporaryDirectory() as temporary:
            catalog = publication.PUBLIC_SOURCE_METADATA
            def git(root, *arguments):
                if arguments == ("ls-files", "--cached", "-z"):
                    return (catalog + "\0").encode()
                self.assertEqual(arguments, ("ls-files", "--stage", "-z", "--", catalog))
                return ("120000 " + "0" * 40 + " 0\t" + catalog + "\0").encode()
            with patch.object(publication, "git_command", git):
                findings, checked, _ = publication.check(Path(temporary), tracked=True)
            self.assertEqual([finding.category for finding in findings], ["unreviewed_source_metadata_mode"])
            self.assertEqual(checked, 0)

    def test_gitignore_permits_only_the_canonical_agents_catalog(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
            (root / ".gitignore").write_bytes((SOURCE.parents[1] / ".gitignore").read_bytes())
            for name, ignored in (
                (publication.PUBLIC_SOURCE_METADATA, False),
                (".agents/plugins/private.json", True),
                (".agents/plugins/cache/private.json", True),
                (".agents/private-state.json", True),
            ):
                result = subprocess.run(["git", "-C", str(root), "check-ignore", "--quiet", "--", name], capture_output=True)
                self.assertEqual(result.returncode, 0 if ignored else 1, name)
            catalog = root / publication.PUBLIC_SOURCE_METADATA
            catalog.parent.mkdir(parents=True)
            catalog.write_text('{"name":"example-source","plugins":[]}', encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "--", publication.PUBLIC_SOURCE_METADATA], check=True, capture_output=True)
            findings, checked, _ = publication.check(root, tracked=True)
            self.assertEqual(findings, [])
            self.assertEqual(checked, 1)

    def test_index_content_is_scanned_instead_of_modified_working_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
            source = root / "example.py"
            token = "sk" + "-" + "B" * 32
            source.write_text(repr(token), encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "example.py"], check=True, capture_output=True)
            source.write_text("Safe working tree", encoding="utf-8")
            findings, _, _ = publication.check(root, tracked=True)
            self.assertEqual([finding.category for finding in findings], ["api_key"])

    def test_synthetic_values_and_public_owner_attribution_are_allowed(self):
        blob = b'https://github.com/example-owner/pron-hub\n/home/example/project\n00000000-0000-7000-8000-000000000001\n'
        self.assertEqual(publication.inspect_blob("test.py", blob), ([], False))

    def test_fixture_path_exception_does_not_hide_secrets_hosts_or_sessions(self):
        # Deliberately synthetic values; no real deployment identifiers.
        paths = b"/home/fixtureperson/project\nC:/fixturework/project\n"
        self.assertEqual(publication.inspect_blob("tests/fixture.py", paths), ([], False))
        detected, _ = publication.inspect_blob("example.py", paths)
        self.assertEqual({finding.category for finding in detected}, {"personal_path"})
        token = "gh" + "p_" + "D" * 32
        host = "host" + str(987) + "public"
        session = "01abcdef" + "-1234-7000-8000-" + "0123456789ab"
        detected, _ = publication.inspect_blob("tests/fixture.py", (token + "\n" + host + "\n" + session).encode())
        self.assertEqual({finding.category for finding in detected}, {"github_token", "private_host", "session_identifier"})

    def test_artwork_is_explicitly_not_text_scanned(self):
        self.assertEqual(publication.inspect_blob("docs/logo.png", b"\x89PNG\0"), ([], True))
        detected, _ = publication.inspect_blob("docs/logo.svg", b"<svg>Public text</svg>")
        self.assertEqual(detected, [])

    def test_unsupported_binary_and_oversized_text_fail_closed(self):
        self.assertEqual(publication.inspect_blob("binary.exe", b"\0")[0][0].category, "unsupported_binary")
        blob = b"x" * (publication.MAX_TEXT_BYTES + 1)
        self.assertEqual(publication.inspect_blob("large.txt", blob)[0][0].category, "oversized_unreviewed_file")

    def test_secret_patterns_are_detected_by_category(self):
        cases = (
            ("private_key", "-----BEGIN " + "OPENSSH PRIVATE KEY-----"),
            ("github_token", "github" + "_pat_" + "C" * 55),
            ("credential_url", "https://" + "example:synthetic-password@example.invalid"),
            ("session_identifier", "01abcdef" + "-1234-7000-8000-" + "0123456789ab"),
        )
        for expected, value in cases:
            with self.subTest(expected=expected):
                findings, _ = publication.inspect_blob("fixture.txt", value.encode())
                self.assertIn(expected, {finding.category for finding in findings})

    def test_private_path_matching_includes_sidecars_and_template_exception(self):
        for path in ("x.sqlite-wal", "x.sqlite3-shm", ".env.production", "dist/archive.zip", "auth.json"):
            self.assertTrue(publication.private_path(path), path)
        self.assertFalse(publication.private_path(".env.example"))


if __name__ == "__main__":
    unittest.main()
