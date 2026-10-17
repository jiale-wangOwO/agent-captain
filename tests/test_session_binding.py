"""Exact-session scope and identity-race tests; no worker SSH/input/models."""
from __future__ import annotations

import copy
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_captain.sessions import session_binding as binding
SESSION = "ceohub-0123456789abcdef01234567"
THREAD = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
UID, PANE, GROUP, AGENT, TTY = 1003, 100, 200, 201, 34831


def process(pid, parent, pgid, exe, **extra):
    return {"pid": pid, "ppid": parent, "pgid": pgid, "tty_nr": TTY, "tpgid": GROUP,
            "start_ticks": 10000 + pid, "uid": UID, "exe": exe, **extra}


class FakeLinuxProc:
    def __init__(self, home, cwd, held=(), argv=None):
        self.records = {row["pid"]: row for row in [process(PANE, 1, PANE, "/bin/bash"),
                         process(GROUP, PANE, GROUP, "/usr/bin/node"), process(AGENT, GROUP, GROUP, "/opt/codex/codex")]}
        self.home, self.directory = str(home), str(cwd)
        self.paths = list(held)
        self.args = argv or ["codex"]
        self.identity_reads = {}
        self.change_after_first = None
        self.fd_reads = 0
        self.changed_paths = None

    def identity(self, pid):
        self.identity_reads[pid] = self.identity_reads.get(pid, 0) + 1
        row = {key: value for key, value in self.records[pid].items() if key != "exe"}
        if self.change_after_first and pid == AGENT and self.identity_reads[pid] > 1:
            row.update(self.change_after_first)
        return row

    def executable(self, pid):
        return self.records[pid]["exe"]

    def children(self, pid):
        return [row["pid"] for row in self.records.values() if row["ppid"] == pid]

    def stdin_tty(self, pid, tty_nr):
        return self.records[pid].get("stdin_matches", True)

    def cwd(self, pid):
        return self.directory

    def codex_home(self, pid, uid):
        return self.home

    def argv(self, pid):
        return self.args

    def held_rollouts(self, pid, home):
        self.fd_reads += 1
        return self.changed_paths if self.changed_paths is not None and self.fd_reads > 1 else self.paths


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # Normalize the existing temporary root, including Windows 8.3 aliases.
        self.base = Path(self.temp.name).resolve()
        self.home = self.base / ".codex"
        self.cwd = self.base / "project"
        self.cwd.mkdir()
        (self.home / "sessions" / "2026" / "10" / "07").mkdir(parents=True)
        self.path = self.rollout(THREAD)

    def rollout(self, identifier, source="cli", **payload):
        path = self.home / "sessions" / "2026" / "10" / "07" / ("rollout-2026-10-07T12-00-00-" + identifier + ".jsonl")
        header = {"type": "session_meta", "payload": {"id": identifier, "cwd": str(self.cwd), "source": source, **payload}}
        path.write_text(json.dumps(header) + "\n", encoding="utf-8")
        return path

    def index(self, rows, version=5):
        path = self.home / ("state_" + str(version) + ".sqlite")
        with closing(sqlite3.connect(path)) as connection:
            with connection:
                connection.execute("CREATE TABLE threads(id TEXT, rollout_path TEXT, cwd TEXT)")
                connection.executemany("INSERT INTO threads VALUES(?,?,?)", rows)
        return path

    def probe(self, reader, panes=None):
        pane = {"session": SESSION, "pane_id": "%0", "pid": PANE, "tty": "/dev/pts/15", "command": "node"}
        with patch.object(binding, "LinuxProc", return_value=reader), \
             patch.object(binding, "_pane_metadata", side_effect=panes or [pane, pane]), \
             patch.object(binding.os, "geteuid", return_value=UID, create=True):
            return binding.bind_linux(SESSION)

    def test_held_main_rollout_binds_exact_session_and_returns_only_public_identity(self):
        reader = FakeLinuxProc(self.home, self.cwd, [str(self.path)])
        result = self.probe(reader)
        self.assertEqual(result["state"], "bound")
        self.assertEqual(result["session_id"], THREAD)
        self.assertEqual(result["process_identity"]["pid"], AGENT)
        self.assertEqual(result["process_identity"]["start_ticks"], 10201)
        self.assertEqual(result["binding_evidence"], "held_rollout")
        self.assertNotIn("argv", result)
        self.assertNotIn("environment", result)
        self.assertNotIn("exe", result)

    def test_no_fd_or_resume_uuid_preserves_verified_identity_for_manual_selection(self):
        result = self.probe(FakeLinuxProc(self.home, self.cwd, argv=["codex", "resume", "--last"]))
        self.assertEqual(result["state"], "unbound")
        self.assertEqual(result["reason"], "no_exact_session_evidence")
        self.assertEqual(result["process_identity"]["pid"], AGENT)
        self.assertEqual(result["codex_home"], str(self.home))
        self.assertNotIn("session_id", result)

    def test_explicit_resume_uuid_only_reads_that_indexed_id(self):
        other = self.rollout(OTHER)
        self.index([(THREAD, str(self.path), str(self.cwd)), (OTHER, str(other), str(self.cwd))])
        with patch.object(binding, "_rollout_metadata", wraps=binding._rollout_metadata) as header:
            result = self.probe(FakeLinuxProc(self.home, self.cwd, argv=["codex", "resume", THREAD]))
        self.assertEqual(result["session_id"], THREAD)
        self.assertEqual(result["binding_evidence"], "explicit_resume")
        self.assertTrue(all(str(call.args[0]) == str(self.path) for call in header.call_args_list))

    def test_explicit_resume_path_binds_without_session_catalog_scan(self):
        result = self.probe(FakeLinuxProc(self.home, self.cwd, argv=["codex", "resume", str(self.path)]))
        self.assertEqual(result["state"], "bound")
        self.assertEqual(result["session_id"], THREAD)

    def test_held_main_and_subagent_rollout_does_not_expand_worker_scope(self):
        child = self.rollout(OTHER, source={"subagent": {"thread_spawn": {"parent_thread_id": THREAD}}})
        result = self.probe(FakeLinuxProc(self.home, self.cwd, [str(self.path), str(child)]))
        self.assertEqual(result["state"], "bound")
        self.assertEqual(result["session_id"], THREAD)

    def test_user_cli_fork_remains_valid_main_session(self):
        path = self.rollout(THREAD, forked_from_id=OTHER)
        result = self.probe(FakeLinuxProc(self.home, self.cwd, [str(path)]))
        self.assertEqual(result["state"], "bound")

    def test_only_subagent_rollout_cannot_bind_parent(self):
        child = self.rollout(OTHER, source={"subagent": "review"})
        result = self.probe(FakeLinuxProc(self.home, self.cwd, [str(child)]))
        self.assertEqual(result["state"], "unbound")
        self.assertNotIn("session_id", result)

    def test_two_main_held_rollouts_fail_closed(self):
        other = self.rollout(OTHER)
        result = self.probe(FakeLinuxProc(self.home, self.cwd, [str(self.path), str(other)]))
        self.assertEqual(result["reason"], "ambiguous_session_evidence")

    def test_initial_resume_does_not_override_current_held_rollout(self):
        other = self.rollout(OTHER)
        self.index([(OTHER, str(other), str(self.cwd))])
        result = self.probe(FakeLinuxProc(self.home, self.cwd, [str(self.path)], ["codex", "resume", OTHER]))
        self.assertEqual(result["state"], "bound")
        self.assertEqual(result["session_id"], THREAD)
        self.assertEqual(result["binding_evidence"], "held_rollout")

    def test_ignored_subagent_fd_cannot_promote_resume_selector_to_live_evidence(self):
        child = self.rollout(OTHER, source={"subagent": "review"})
        self.index([(THREAD, str(self.path), str(self.cwd))])
        result = self.probe(FakeLinuxProc(self.home, self.cwd, [str(child)], ["codex", "resume", THREAD]))
        self.assertEqual(result["session_id"], THREAD)
        self.assertEqual(result["binding_evidence"], "explicit_resume")

    def test_process_pid_reuse_parent_tty_group_races_remove_manual_identity(self):
        for changed in ({"start_ticks": 9999}, {"ppid": PANE}, {"tty_nr": TTY + 1}, {"pgid": 999}):
            with self.subTest(changed=changed):
                reader = FakeLinuxProc(self.home, self.cwd, [str(self.path)])
                reader.change_after_first = changed
                result = self.probe(reader)
                self.assertEqual(result["reason"], "process_changed")
                self.assertNotIn("process_identity", result)

    def test_pane_change_removes_manual_identity(self):
        pane = {"session": SESSION, "pane_id": "%0", "pid": PANE, "tty": "/dev/pts/15", "command": "node"}
        result = self.probe(FakeLinuxProc(self.home, self.cwd, [str(self.path)]), [pane, {**pane, "pid": 999}])
        self.assertEqual(result["reason"], "process_changed")
        self.assertNotIn("process_identity", result)

    def test_fd_evidence_disappearing_between_checks_fails_closed(self):
        reader = FakeLinuxProc(self.home, self.cwd, [str(self.path)])
        reader.changed_paths = []
        result = self.probe(reader)
        self.assertEqual(result["reason"], "session_evidence_changed")
        self.assertNotIn("process_identity", result)

    def test_other_tty_uid_or_background_codex_never_binds(self):
        for changed in ({"tty_nr": TTY + 1}, {"uid": 0}, {"pgid": 999}, {"tpgid": 999}, {"stdin_matches": False}):
            with self.subTest(changed=changed):
                reader = FakeLinuxProc(self.home, self.cwd, [str(self.path)])
                reader.records[AGENT].update(changed)
                self.assertEqual(self.probe(reader)["state"], "unbound")

    def test_multiple_native_foreground_children_are_ambiguous(self):
        reader = FakeLinuxProc(self.home, self.cwd, [str(self.path)])
        reader.records[202] = process(202, GROUP, GROUP, "/another/codex")
        self.assertEqual(self.probe(reader)["reason"], "foreground_codex_ambiguous")

    def test_unrelated_native_process_is_not_traversed(self):
        reader = FakeLinuxProc(self.home, self.cwd)
        reader.records[AGENT]["ppid"] = 900
        reader.records[900] = process(900, 1, GROUP, "/usr/bin/node")
        self.assertEqual(self.probe(reader)["reason"], "foreground_not_codex")
        self.assertNotIn(AGENT, reader.identity_reads)

    def test_header_must_be_session_meta_and_matching_cwd(self):
        invalid = [[], {"type": "event_msg", "payload": {}}, {"type": "session_meta", "payload": {"id": "not-an-id", "cwd": str(self.cwd), "source": "cli"}}]
        for header in invalid:
            with self.subTest(header=header):
                self.path.write_text(json.dumps(header) + "\n", encoding="utf-8")
                self.assertEqual(self.probe(FakeLinuxProc(self.home, self.cwd, [str(self.path)]))["state"], "unbound")
        self.rollout(THREAD, cwd=str(self.cwd / "different"))
        self.assertEqual(self.probe(FakeLinuxProc(self.home, self.cwd, [str(self.path)]))["reason"], "session_cwd_mismatch")

    def test_resolve_exact_uuid_ignores_newer_other_thread(self):
        other = self.rollout(OTHER)
        self.index([(OTHER, str(other), str(self.cwd)), (THREAD, str(self.path), str(self.cwd))])
        result = binding.resolve_session(THREAD, str(self.home))
        self.assertEqual(result["state"], "resolved")
        self.assertEqual(result["session_id"], THREAD)

    def test_resolve_missing_index_never_scans_rollout_filenames(self):
        with patch.object(Path, "rglob", side_effect=AssertionError("No session scan allowed")):
            result = binding.resolve_session(THREAD, str(self.home))
        self.assertEqual(result["reason"], "session_not_indexed")

    def test_resolve_exact_id_rejects_spoofed_metadata_id(self):
        other = self.rollout(OTHER)
        self.index([(THREAD, str(other), str(self.cwd))])
        self.assertEqual(binding.resolve_session(THREAD, str(self.home))["reason"], "session_id_mismatch")

    def test_resolve_multiple_versions_agree_but_conflicting_paths_fail(self):
        rows = [(THREAD, str(self.path), str(self.cwd))]
        self.index(rows, 5)
        self.index(rows, 6)
        self.assertEqual(binding.resolve_session(THREAD, str(self.home))["state"], "resolved")
        other_path = self.home / "sessions" / "different.jsonl"
        other_path.write_bytes(self.path.read_bytes())
        with closing(sqlite3.connect(self.home / "state_6.sqlite")) as connection:
            with connection:
                connection.execute("UPDATE threads SET rollout_path=?", (str(other_path),))
        self.assertEqual(binding.resolve_session(THREAD, str(self.home))["reason"], "session_index_ambiguous")

    def test_resolve_index_cannot_point_outside_allowed_home(self):
        outside = Path(self.temp.name) / "outside.jsonl"
        outside.write_bytes(self.path.read_bytes())
        self.index([(THREAD, str(outside), str(self.cwd))])
        self.assertEqual(binding.resolve_session(THREAD, str(self.home))["reason"], "rollout_outside_codex_home")

    def test_resolve_duplicate_same_uuid_rows_fail_closed(self):
        self.index([(THREAD, str(self.path), str(self.cwd))] * 2)
        self.assertEqual(binding.resolve_session(THREAD, str(self.home))["reason"], "session_index_ambiguous")

    def test_resume_parser_never_treats_prompt_or_option_value_as_selector(self):
        for argv in (["codex", "tell", "resume", THREAD], ["codex", "-c", "resume", THREAD],
                     ["codex", "resume", "--last", THREAD], ["codex", "resume", "my-title"], ["codex", "resume"],
                     ["codex", "--unknown", "resume", THREAD]):
            with self.subTest(argv=argv):
                self.assertIsNone(binding._resume_selector(argv))
        self.assertEqual(binding._resume_selector(["codex", "-C", str(self.cwd), "resume", THREAD])["value"], THREAD)

    def test_linux_env_reader_only_returns_codex_home_never_other_keys(self):
        raw = b"PRIVATE_TOKEN=synthetic-do-not-export\0CODEX_HOME=" + str(self.home).encode() + b"\0OTHER=secret\0"
        with patch.object(binding, "_read_limited", return_value=raw):
            value = binding.LinuxProc().codex_home(AGENT, UID)
        self.assertEqual(value, str(self.home))
        self.assertNotIn("synthetic", value)

    def test_linux_env_reader_rejects_duplicate_or_relative_home(self):
        for raw in (b"CODEX_HOME=relative\0", b"CODEX_HOME=/one\0CODEX_HOME=/two\0"):
            with self.subTest(raw=raw), patch.object(binding, "_read_limited", return_value=raw):
                with self.assertRaises(binding.BindingError):
                    binding.LinuxProc().codex_home(AGENT, UID)

    def test_tmux_query_requires_exact_one_owned_session_and_pane(self):
        good = SESSION + "\t%0\t100\t/dev/pts/15\tnode\n"
        for output in (good + good, good.replace(SESSION, "ceohub-aaaaaaaaaaaaaaaa"), "100\t/dev/pts/15\tnode\n"):
            with self.subTest(output=output), patch.object(binding.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=output)):
                with self.assertRaises(binding.BindingError):
                    binding._pane_metadata(SESSION)
        with patch.object(binding.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=good)) as run:
            self.assertEqual(binding._pane_metadata(SESSION)["pid"], 100)
        self.assertIn("=" + SESSION, run.call_args.args[0])

    def test_windows_native_helpers_inspect_our_test_process_only(self):
        if os.name != "nt":
            self.skipTest("Windows native probe fixture")
        reader = binding.WindowsProc()
        identity = reader.identity(os.getpid())
        home, cwd = reader.home_and_cwd(os.getpid())
        self.assertEqual(identity["pid"], os.getpid())
        self.assertGreater(identity["start_ticks"], 0)
        self.assertEqual(Path(cwd), Path.cwd())
        self.assertTrue(Path(home).is_absolute())
        self.assertIsInstance(reader.held_rollouts(os.getpid(), home), list)

    def test_held_rollout_scope_accepts_mixed_short_and_long_path_aliases(self):
        # Simulate 8.3 resolution on every platform rather than requiring a
        # volume with short-name creation enabled or skipping the regression.
        short_home = self.base / "CODEX~1"
        short_rollout = short_home / self.path.relative_to(self.home)
        outside = self.base / "outside.jsonl"
        misleading = self.home / "sessions" / "OUTSID~1.jsonl"
        aliases = {short_home: self.home, short_rollout: self.path, misleading: outside}
        resolve = Path.resolve

        def canonicalize(path, *args, **kwargs):
            if path in aliases:
                return aliases[path]
            return resolve(path, *args, **kwargs)

        with patch.object(binding.Path, "resolve", autospec=True, side_effect=canonicalize):
            for value, home in ((self.path, short_home), (short_rollout, self.home),
                                (short_rollout, short_home)):
                with self.subTest(value=value, home=home):
                    self.assertEqual(binding._held_rollout_candidate(str(value), str(home)), str(self.path))
            self.assertIsNone(binding._held_rollout_candidate(str(outside), str(short_home)))
            self.assertIsNone(binding._held_rollout_candidate(str(misleading), str(short_home)))
            self.assertIsNone(binding._held_rollout_candidate(str(short_home / "auth.json"), str(short_home)))

    def test_windows_native_per_process_handles_find_only_owned_fixture_rollout(self):
        if os.name != "nt":
            self.skipTest("Windows native handles fixture")
        reader = binding.WindowsProc()
        with self.path.open("rb"):
            # Keep the original TEMP spelling here so Windows CI exercises the
            # real short-home/long-handle case, not only a normalized fixture.
            original_home = Path(self.temp.name) / ".codex"
            paths = reader.held_rollouts(os.getpid(), str(original_home))
        self.assertEqual(paths, [str(self.path)])

    def test_windows_unbound_keeps_process_identity_without_private_owner(self):
        owner = b"synthetic-private-SID"
        root = {"pid": PANE, "start_ticks": 10100, "exe": "C:/Windows/pwsh.exe", "session": 1, "owner": owner}
        agent = {"pid": AGENT, "start_ticks": 10201, "exe": "C:/Codex/codex.exe", "session": 1, "owner": owner}
        identities = {PANE: root, AGENT: agent, os.getpid(): {"pid": os.getpid(), "start_ticks": 1, "exe": "python.exe", "session": 1, "owner": owner}}
        fake = SimpleNamespace(identity=lambda pid: identities[pid], descendants=lambda pid: [(PANE, 1), (AGENT, PANE)],
                               home_and_cwd=lambda pid: (str(self.home), str(self.cwd)), held_rollouts=lambda pid, home: [])
        with patch.object(binding, "WindowsProc", return_value=fake):
            result = binding._bind_windows_native(PANE)
        self.assertEqual(result["reason"], "no_exact_session_evidence")
        self.assertEqual(result["process_identity"]["pid"], AGENT)
        self.assertEqual(result["codex_home"], str(self.home))
        self.assertNotIn("owner", result["process_identity"])
        self.assertNotIn("synthetic-private", str(result))


if __name__ == "__main__":
    unittest.main()
