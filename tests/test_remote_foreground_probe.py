"""Pure process-metadata tests; no /proc, SSH, PTY, models, or user input."""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_captain.transport import remote_foreground_probe as probe
UID = 1003
TTY = 34831
PANE = 100
GROUP = 200


def process(pid, ppid, pgid, exe, **extra):
    return {"pid": pid, "ppid": ppid, "pgid": pgid, "tpgid": GROUP, "tty_nr": TTY,
            "uid": UID, "start_time": 10000 + pid, "exe": exe, **extra}


def launcher_tree():
    return [process(PANE, 1, PANE, "/bin/bash"), process(GROUP, PANE, GROUP, "/usr/bin/node"),
            process(201, GROUP, GROUP, "/opt/node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/codex/codex")]


class FakeProcReader:
    def __init__(self):
        self.root = Path("/fake-proc")
        self.records = {row["pid"]: row for row in launcher_tree()}
        self.changed = None

    def stat(self, pid):
        keys = ("pid", "ppid", "pgid", "tpgid", "tty_nr", "uid", "start_time")
        result = {key: self.records[pid][key] for key in keys}
        if self.changed and pid == 201:
            result.update(self.changed)
        return result

    def read(self, pid, tty_nr):
        return {**self.records[pid], "stdin_matches_tty": True}

    def children(self, pid):
        return [row["pid"] for row in self.records.values() if row["ppid"] == pid]

    def launcher(self, pid):
        raise AssertionError("Native Codex match must not read launcher arguments")


class RemoteForegroundProbeTests(unittest.TestCase):
    def classify(self, records, **extra):
        options = {"pane_pid": PANE, "pane_tty_nr": TTY, "foreground_pgid": GROUP,
                   "uid": UID, "command": "node", "stable": True}
        options.update(extra)
        return probe.classify_foreground(records, **options)

    def assert_closed(self, result):
        self.assertNotEqual(result["state"], "agent")
        self.assertNotEqual(result.get("agent_kind"), "codex")
        self.assertFalse(result.get("agent_pid"))

    def test_foreground_node_and_same_group_native_codex_is_identified(self):
        result = self.classify(launcher_tree())
        self.assertEqual(result["state"], "agent")
        self.assertEqual(result["agent_kind"], "codex")
        self.assertEqual(result["agent_pid"], 201)
        self.assertEqual(result["foreground_pgid"], GROUP)
        self.assertEqual(result["command"], "node")

    def test_plain_node_without_codex_is_not_an_agent(self):
        self.assert_closed(self.classify(launcher_tree()[:2]))

    def test_official_node_launcher_is_recognized_before_native_child_starts(self):
        records = launcher_tree()[:2]
        records[1]["launcher_path"] = "/usr/local/lib/node_modules/@openai/codex/bin/codex.js"
        result = self.classify(records)
        self.assertEqual(result["state"], "agent")
        self.assertEqual(result["agent_kind"], "codex")
        self.assertEqual(result["agent_pid"], GROUP)

    def test_codexish_script_or_backup_filename_is_not_official_launcher(self):
        for path in ("/tmp/codex.js", "/tmp/node_modules/@openai/codex/bin/codex.js.bak", "/tmp/not-openai/codex/bin/codex.js"):
            with self.subTest(path=path):
                records = launcher_tree()[:2]
                records[1]["launcher_path"] = path
                self.assert_closed(self.classify(records))

    def test_official_path_mentioned_only_in_later_arguments_cannot_authorize_generic_node(self):
        records = launcher_tree()[:2]
        records[1]["argv"] = ["node", "/tmp/generic.js", "/usr/local/lib/node_modules/@openai/codex/bin/codex.js"]
        self.assert_closed(self.classify(records))

    def test_background_native_codex_cannot_authorize_foreground_shell(self):
        records = [process(PANE, 1, PANE, "/bin/bash", tpgid=PANE),
                   process(201, PANE, 201, "/usr/bin/codex", tpgid=PANE)]
        self.assert_closed(self.classify(records, foreground_pgid=PANE, command="bash"))

    def test_shell_with_background_descendant_even_same_group_stays_shell(self):
        records = [process(PANE, 1, PANE, "/bin/bash", tpgid=PANE),
                   process(201, PANE, PANE, "/usr/bin/codex", tpgid=PANE)]
        self.assert_closed(self.classify(records, foreground_pgid=PANE, command="bash"))

    def test_agent_must_match_uid_tty_and_actual_foreground_group(self):
        changes = ({"uid": 0}, {"tty_nr": TTY + 1}, {"pgid": 999}, {"tpgid": 999}, {"stdin_matches_tty": False})
        for change in changes:
            with self.subTest(change=change):
                records = launcher_tree()
                records[-1].update(change)
                self.assert_closed(self.classify(records))

    def test_unrelated_codex_same_uid_tty_and_group_is_not_a_pane_descendant(self):
        records = launcher_tree()[:2] + [process(900, 899, GROUP, "/usr/bin/codex")]
        self.assert_closed(self.classify(records))

    def test_duplicate_pid_with_conflicting_identity_fails_closed(self):
        records = launcher_tree()
        replacement = copy.deepcopy(records[-1])
        replacement["start_time"] += 10
        self.assert_closed(self.classify(records + [replacement]))

    def test_changed_pid_or_foreground_between_snapshots_is_not_trusted(self):
        result = self.classify(launcher_tree(), stable=False)
        self.assert_closed(result)
        self.assertEqual(result["reason"], "process_changed")

    def test_missing_process_identity_or_foreground_leader_fails_closed(self):
        missing_start = launcher_tree()
        missing_start[-1].pop("start_time")
        self.assert_closed(self.classify(missing_start))
        self.assert_closed(self.classify([launcher_tree()[0], launcher_tree()[-1]]))

    def test_foreign_uid_ancestor_and_parent_cycles_fail_closed(self):
        foreign = launcher_tree()
        foreign[1]["uid"] = 0
        self.assert_closed(self.classify(foreign))
        cycle = launcher_tree()[:2] + [process(201, 202, GROUP, "/usr/bin/codex"),
                                      process(202, 201, GROUP, "/usr/bin/node")]
        self.assert_closed(self.classify(cycle))

    def test_metadata_result_never_exposes_executable_paths_or_launcher_arguments(self):
        result = self.classify(launcher_tree())
        self.assertNotIn("exe", result)
        self.assertNotIn("launcher_path", result)
        self.assertNotIn("argv", result)
        self.assertNotIn("environment", result)
        self.assertNotIn("node_modules", str(result))

    def test_probe_rechecks_exact_ancestry_and_never_executes_worker_or_reads_arguments_for_native_match(self):
        reader = FakeProcReader()
        metadata = {"pid": PANE, "tty": "/dev/pts/15", "command": "node"}
        with patch.object(probe, "pane_metadata", return_value=metadata) as pane_query, \
             patch.object(probe, "ProcReader", return_value=reader), \
             patch.object(probe.os, "geteuid", return_value=UID, create=True), \
             patch.object(probe.os, "readlink", side_effect=lambda path: reader.records[int(path.parent.name)]["exe"]), \
             patch.object(probe.subprocess, "run", side_effect=AssertionError("no worker executable may run")):
            result = probe.probe("ceohub-0123456789abcdef01234567")
        self.assertEqual(result["state"], "agent")
        self.assertEqual(result["agent_pid"], 201)
        self.assertEqual(result["agent_start_ticks"], 10201)
        self.assertEqual(pane_query.call_count, 2)

    def test_probe_changed_start_ticks_or_parent_after_collection_is_untrusted(self):
        for changed in ({"start_time": 99999}, {"ppid": PANE}, {"tty_nr": TTY + 1}, {"pgid": 999}):
            with self.subTest(changed=changed):
                reader = FakeProcReader()
                reader.changed = changed
                with patch.object(probe, "pane_metadata", return_value={"pid": PANE, "tty": "/dev/pts/15", "command": "node"}), \
                     patch.object(probe, "ProcReader", return_value=reader), \
                     patch.object(probe.os, "geteuid", return_value=UID, create=True), \
                     patch.object(probe.os, "readlink", side_effect=lambda path: reader.records[int(path.parent.name)]["exe"]):
                    result = probe.probe("ceohub-0123456789abcdef01234567")
                self.assert_closed(result)
                self.assertEqual(result["reason"], "process_changed")

    def test_probe_changed_tmux_pane_metadata_fails_closed(self):
        reader = FakeProcReader()
        first = {"pid": PANE, "tty": "/dev/pts/15", "command": "node"}
        with patch.object(probe, "pane_metadata", side_effect=[first, {**first, "command": "bash"}]), \
             patch.object(probe, "ProcReader", return_value=reader), \
             patch.object(probe.os, "geteuid", return_value=UID, create=True), \
             patch.object(probe.os, "readlink", side_effect=lambda path: reader.records[int(path.parent.name)]["exe"]):
            result = probe.probe("ceohub-0123456789abcdef01234567")
        self.assert_closed(result)
        self.assertEqual(result["reason"], "process_changed")

    def test_official_launcher_entry_script_change_with_same_pid_and_executable_fails_closed(self):
        reader = FakeProcReader()
        reader.records.pop(201)  # No native child; prove only the entry script.
        official = "/usr/local/lib/node_modules/@openai/codex/bin/codex.js"
        with patch.object(probe, "pane_metadata", return_value={"pid": PANE, "tty": "/dev/pts/15", "command": "node"}), \
             patch.object(probe, "ProcReader", return_value=reader), \
             patch.object(probe.os, "geteuid", return_value=UID, create=True), \
             patch.object(probe.os, "readlink", side_effect=lambda path: reader.records[int(path.parent.name)]["exe"]), \
             patch.object(reader, "launcher", side_effect=[official, None]) as launcher:
            result = probe.probe("ceohub-0123456789abcdef01234567")
        self.assertEqual(launcher.call_count, 2, "The official entry script must still match at final identity verification")
        self.assert_closed(result)
        self.assertEqual(result["reason"], "process_changed")

    def test_stat_parser_handles_parentheses_in_command_without_misreading_identity(self):
        fields = ["S", str(PANE), str(GROUP), str(PANE), str(TTY), str(GROUP)] + ["0"] * 13 + ["10200"]
        raw = (str(GROUP) + " (node (launcher) name) " + " ".join(fields) + "\n").encode()
        reader = probe.ProcReader("/fake-proc")
        with patch.object(reader, "_read", return_value=raw), patch.object(Path, "stat", return_value=SimpleNamespace(st_uid=UID)):
            result = reader.stat(GROUP)
        self.assertEqual(result["ppid"], PANE)
        self.assertEqual(result["tty_nr"], TTY)
        self.assertEqual(result["start_time"], 10200)

    def test_stat_parser_refuses_malformed_or_replaced_process_record(self):
        reader = probe.ProcReader("/fake-proc")
        for raw in (b"not a proc stat", b"201 (node) S 100 200 100 34831 200 0", b"200 (node) S not-a-parent"):
            with self.subTest(raw=raw), patch.object(reader, "_read", return_value=raw):
                with self.assertRaises((ValueError, probe.ProcessChanged)):
                    reader.stat(GROUP)

    def test_launcher_reader_uses_only_node_entry_script_not_later_task_arguments(self):
        reader = probe.ProcReader("/fake-proc")
        official = b"/usr/local/lib/node_modules/@openai/codex/bin/codex.js"
        with patch.object(reader, "_read", return_value=b"node\0/tmp/generic.js\0" + official + b"\0synthetic-private-argument\0"):
            self.assertIsNone(reader.launcher(GROUP))
        with patch.object(reader, "_read", return_value=b"node\0" + official + b"\0synthetic-private-argument\0"):
            self.assertEqual(reader.launcher(GROUP), official.decode())


if __name__ == "__main__":
    unittest.main()
