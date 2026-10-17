import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_captain.sessions import conversation_output as output
def event(kind, payload):
    return {"type": kind, "timestamp": "2026-10-07T10:00:00Z", "payload": payload}


def message(text, role="assistant", channel="final"):
    return event("response_item", {"type": "message", "role": role, "channel": channel,
                                   "content": [{"type": "output_text", "text": text}]})


class ConversationOutputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve() / "codex"
        self.path = self.home / "sessions" / "2026" / "selected.jsonl"
        self.path.parent.mkdir(parents=True)
        self.meta = event("session_meta", {"id": "selected-session", "cwd": "/work/selected"})
        self.write([self.meta])

    def write(self, records, append=False):
        with self.path.open("ab" if append else "wb") as stream:
            for record in records:
                stream.write((json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"))

    def read(self, **kwargs):
        return output.main(dict(op="read_output", session_id="selected-session",
                                rollout_path=str(self.path), codex_home=str(self.home), **kwargs))

    def pages(self, first, limit=None, include_tools=False):
        result = first
        yield result
        for _ in range(1000):
            if not result["next_cursor"]:
                return
            args = {"cursor": result["next_cursor"], "include_tools": include_tools}
            if limit:
                args["limit"] = limit
            result = self.read(**args)
            yield result
        self.fail("cursor did not terminate")

    def test_complete_small_history(self):
        self.write([self.meta, message("我的问题", "user", None), message("完整答案")])
        result = self.read()
        self.assertEqual([m["text"] for m in result["messages"]], ["我的问题", "完整答案"])
        self.assertTrue(result["history_complete"])
        self.assertTrue(result["content_complete"])
        self.assertFalse(result["truncated"])
        self.assertEqual(result["source"], "codex_rollout")

    def test_long_first_middle_tail_all_recoverable_unicode(self):
        huge = "FIRST\n" + "汉🙂CJK\n" * 220000 + "\nMIDDLE-VALIDATION\n" + "tail" * 190000 + "\nLAST"
        self.assertGreater(len(huge.encode("utf-8")), 1024 * 1024)
        self.write([self.meta, message("older"), message(huge), message("latest")])
        chunks = {}
        first = self.read(limit=2)
        self.assertTrue(first["has_more"])
        self.assertFalse(first["messages"][0]["content_complete"])
        for page in self.pages(first):
            self.assertLessEqual(sum(len(m["text"].encode("utf-8")) for m in page["messages"]), output.PAGE_BYTES)
            for msg in page["messages"]:
                chunks.setdefault(msg["message_id"], []).append((msg["chunk_offset"], msg["text"]))
        texts = ["".join(text for _, text in sorted(parts)) for parts in chunks.values()]
        self.assertCountEqual(texts, ["older", huge, "latest"])
        for parts in chunks.values():
            self.assertEqual(len({offset for offset, _ in parts}), len(parts))

    def test_latest_window_then_older_without_duplicates(self):
        self.write([self.meta] + [message(str(i)) for i in range(11)])
        result = self.read(limit=3)
        self.assertEqual([m["text"] for m in result["messages"]], ["8", "9", "10"])
        recovered = [m["text"] for page in self.pages(result) for m in page["messages"]]
        self.assertCountEqual(recovered, [str(i) for i in range(11)])
        self.assertEqual(len(recovered), len(set(recovered)))

    def test_dual_event_response_deduplicated_both_orders(self):
        agent = event("event_msg", {"type": "agent_message", "message": "same final"})
        self.write([self.meta, agent, message("same final"), message("second"),
                    event("event_msg", {"type": "agent_message", "message": "second"})])
        self.assertEqual([m["text"] for m in self.read()["messages"]], ["same final", "second"])

    def test_dual_event_response_append_no_duplicate(self):
        self.write([self.meta, event("event_msg", {"type": "agent_message", "message": "same final"})])
        first = self.read()
        self.write([message("same final"), message("new answer")], append=True)
        polled = self.read(after_cursor=first["after_cursor"])
        self.assertEqual([m["text"] for m in polled["messages"]], ["new answer"])

    def test_identical_report_in_separate_turns_not_deduplicated(self):
        self.write([self.meta, message("same final"), event("event_msg", {"type": "task_started"}),
                    event("event_msg", {"type": "agent_message", "message": "same final"}),
                    message("same final")])
        self.assertEqual([m["text"] for m in self.read()["messages"]], ["same final", "same final"])

    def test_append_frozen_older_pages_and_after_poll(self):
        self.write([self.meta] + [message(str(i)) for i in range(5)])
        first = self.read(limit=2)
        self.write([message("appended")], append=True)
        recovered = [m["text"] for page in self.pages(first) for m in page["messages"]]
        self.assertCountEqual(recovered, [str(i) for i in range(5)])
        polled = self.read(after_cursor=first["after_cursor"])
        self.assertEqual([m["text"] for m in polled["messages"]], ["appended"])
        empty = self.read(after_cursor=polled["after_cursor"])
        self.assertEqual(empty["messages"], [])

    def test_live_burst_every_message_recovered(self):
        first = self.read()
        self.write([message("new-" + str(i)) for i in range(12)], append=True)
        polled = self.read(after_cursor=first["after_cursor"], limit=3)
        recovered = [m["text"] for page in self.pages(polled, limit=3) for m in page["messages"]]
        self.assertEqual(recovered, ["new-" + str(i) for i in range(12)])

    def test_limit_change_does_not_drop_pending_messages(self):
        self.write([self.meta, message("X" * (output.CHUNK_CHARS + 1))] + [message(str(i)) for i in range(5)])
        first = self.read(limit=6)
        recovered = [m for page in self.pages(first, limit=1) for m in page["messages"]]
        texts = [m["text"] for m in recovered if m["total_chars"] == 1]
        self.assertEqual(texts, [str(i) for i in range(5)])

    def test_replacement_and_prefix_rewrite_reject_cursor(self):
        self.write([self.meta, message("first"), message("second")])
        first = self.read(limit=1)
        replacement = self.path.with_suffix(".replacement")
        replacement.write_bytes(self.path.read_bytes())
        os.replace(replacement, self.path)
        with self.assertRaisesRegex(ValueError, "stale"):
            self.read(cursor=first["next_cursor"])
        first = self.read(limit=1)
        with self.path.open("r+b") as stream:
            payload = stream.read().replace(b"first", b"FIRST")
            stream.seek(0)
            stream.write(payload)
        with self.assertRaisesRegex(ValueError, "stale"):
            self.read(cursor=first["next_cursor"])

    def test_truncation_and_wrong_session_fail_closed(self):
        self.write([self.meta, message("first"), message("second")])
        first = self.read(limit=1)
        self.write([self.meta])
        with self.assertRaisesRegex(ValueError, "stale"):
            self.read(cursor=first["next_cursor"])
        self.write([event("session_meta", {"id": "another-session"}), message("secret")])
        with self.assertRaisesRegex(ValueError, "session_meta"):
            self.read()

    def test_path_escape_and_cursor_binding_rejected(self):
        outside = Path(self.temp.name) / "outside.jsonl"
        outside.write_bytes(self.path.read_bytes())
        with self.assertRaisesRegex(ValueError, "outside"):
            output.main(dict(op="read_output", session_id="selected-session", rollout_path=str(outside),
                             codex_home=str(self.home)))
        self.write([self.meta, message("first"), message("second")])
        first = self.read(limit=1)
        other = self.path.with_name("other.jsonl")
        other.write_bytes(self.path.read_bytes())
        with self.assertRaisesRegex(ValueError, "bound"):
            output.main(dict(op="read_output", session_id="selected-session", rollout_path=str(other),
                             codex_home=str(self.home), cursor=first["next_cursor"]))

    def test_symlink_escape_rejected_when_supported(self):
        outside = Path(self.temp.name) / "outside.jsonl"
        outside.write_bytes(self.path.read_bytes())
        alias = self.path.with_name("alias.jsonl")
        try:
            alias.symlink_to(outside)
        except OSError:
            self.skipTest("symlink privilege not available")
        with self.assertRaisesRegex(ValueError, "outside"):
            output.main(dict(op="read_output", session_id="selected-session", rollout_path=str(alias),
                             codex_home=str(self.home)))

    def test_hidden_context_reasoning_and_phase_filter(self):
        records = [self.meta, message("thinking", channel="analysis"), message("reason", channel="reasoning"),
                   message("developer instruction", role="developer", channel=None),
                   message("system instruction", role="system", channel=None),
                   message("# AGENTS.md instructions for x", role="user", channel=None),
                   message("<environment_context>hidden</environment_context>", role="user", channel=None),
                   message("<permissions instructions>hidden", role="user", channel=None),
                   message("<skills_instructions>hidden", role="user", channel=None),
                   message("<environment_context>hidden</environment_context>\n## My request:\nvisible request", role="user", channel=None),
                   event("response_item", {"type": "message", "role": "assistant", "phase": "commentary",
                                           "content": [{"type": "output_text", "text": "progress"}]}),
                   event("response_item", {"type": "message", "role": "assistant", "phase": "final_answer",
                                           "content": [{"type": "output_text", "text": "final report"}]})]
        self.write(records)
        self.assertEqual([m["text"] for m in self.read()["messages"]], ["visible request", "progress", "final report"])

    def test_generated_goal_and_ambient_context_not_user_messages(self):
        goal = '<codex_internal_context source="goal">\nActive goal: internal injected state.\n</codex_internal_context>'
        ambient = '<in-app-browser-context source="ambient-ui-state">\nAutomatically supplied browser state.\n</in-app-browser-context>'
        self.write([self.meta, message(goal, role="user", channel=None),
                    event("event_msg", {"type": "user_message", "message": goal}),
                    message(ambient, role="user", channel=None),
                    message(goal + "\n\n" + ambient + "\n\n## My request:\n真正的请求", role="user", channel=None),
                    message(goal + "\n\n下一步请检查测试", role="user", channel=None),
                    message("真实回答")])
        result = self.read()
        self.assertEqual([m["text"] for m in result["messages"]], ["真正的请求", "下一步请检查测试", "真实回答"])
        self.assertTrue(result["history_complete"])

    def test_generated_context_literal_quotes_and_unknown_sources_preserved(self):
        goal = '<codex_internal_context source="goal">state</codex_internal_context>'
        texts = ["```xml\n" + goal + "\n```", "> " + goal, '"' + goal + '"',
                 "请解释这个字面引用：\n" + goal,
                 '<codex_internal_context source="example">not injected</codex_internal_context>',
                 '<codex_internal_context data-source="goal">ordinary attribute</codex_internal_context>',
                 '<codex_internal_context source="goal">an unclosed literal tag',
                 "# AGENTS.md\n这是我手工写的文档标题", "我想建立一个 goal，不是内部上下文"]
        self.write([self.meta] + [message(text, role="user", channel=None) for text in texts])
        self.assertEqual([m["text"] for m in self.read()["messages"]], texts)

    def test_generated_context_known_wrappers_preserve_following_human_text(self):
        self.write([self.meta,
                    message("<environment_context>machine context</environment_context>\n直接提出的问题", role="user", channel=None),
                    message("<app-context>generated state</app-context>\n<skills_instructions>generated skill</skills_instructions>\n## My request:\n保留这个请求", role="user", channel=None)])
        self.assertEqual([m["text"] for m in self.read()["messages"]], ["直接提出的问题", "保留这个请求"])

    def test_malformed_and_partial_lines_explicit_then_completed(self):
        self.write([self.meta, message("good")])
        partial = json.dumps(message("completed later"), ensure_ascii=False).encode("utf-8")
        with self.path.open("ab") as stream:
            stream.write(b"not-json\n")
            stream.write(partial[:20])
        result = self.read()
        self.assertFalse(result["snapshot_complete"])
        self.assertFalse(result["history_complete"])
        self.assertEqual(result["read_issues"]["malformed_records"], 1)
        self.assertTrue(result["read_issues"]["incomplete_last_line"])
        with self.path.open("ab") as stream:
            stream.write(partial[20:] + b"\n")
        polled = self.read(after_cursor=result["after_cursor"])
        self.assertEqual([m["text"] for m in polled["messages"]], ["completed later"])

    def test_tools_signals_are_explicitly_bounded(self):
        self.write([self.meta, event("response_item", {"type": "function_call_output", "output": "x" * 4000}),
                    event("event_msg", {"type": "task_complete"})])
        result = self.read()
        self.assertEqual(len(result["recent_signals"][0]["text"]), output.SIGNAL_CHARS)
        self.assertFalse(result["recent_signals"][0]["text_complete"])
        self.assertFalse(result["signals_complete"])
        self.assertEqual(result["status"], "completed_in_history")

    def test_full_long_tool_output_opt_in_paginated_no_silent_cut(self):
        long_text = "START\n" + "全量工具输出🙂\n" * 30000 + "\nEND"
        self.write([self.meta,
                    event("response_item", {"type": "function_call", "name": "exec_command", "call_id": "tool-1",
                                            "arguments": '{"cmd":"read-only diagnostic"}'}),
                    event("response_item", {"type": "function_call_output", "call_id": "tool-1", "output": long_text}),
                    event("response_item", {"type": "custom_tool_call", "name": "custom", "call_id": "tool-2", "input": "input"}),
                    event("response_item", {"type": "custom_tool_call_output", "call_id": "tool-2", "output": {"value": 123}}),
                    message("report")])
        self.assertEqual([m["text"] for m in self.read()["messages"]], ["report"])
        parts = []
        calls = []
        for page in self.pages(self.read(include_tools=True), include_tools=True):
            for msg in page["messages"]:
                if msg.get("tool_type") == "function_call_output":
                    self.assertEqual(msg["role"], "tool")
                    self.assertEqual(msg["call_id"], "tool-1")
                    parts.append((msg["chunk_offset"], msg["text"]))
                if msg.get("tool_type") in ("function_call", "custom_tool_call", "custom_tool_call_output"):
                    calls.append(msg)
        self.assertEqual("".join(text for _, text in sorted(parts)), long_text)
        self.assertEqual(len(calls), 3)
        self.assertIn("read-only diagnostic", calls[0]["text"])

    def test_tool_cursor_bound_to_opt_in_and_boolean_validation(self):
        self.write([self.meta, event("response_item", {"type": "function_call_output", "output": "x" * 70000})])
        first = self.read(include_tools=True)
        with self.assertRaisesRegex(ValueError, "bound"):
            self.read(cursor=first["next_cursor"])
        with self.assertRaisesRegex(ValueError, "boolean"):
            self.read(include_tools="true")

    def test_oversized_record_explicit_not_silently_accepted(self):
        self.write([self.meta, message("x" * 3000), message("after oversized")])
        with patch.object(output, "MAX_RECORD_BYTES", 1000):
            result = self.read()
        self.assertEqual([m["text"] for m in result["messages"]], ["after oversized"])
        self.assertEqual(result["read_issues"]["oversized_records"], 1)
        self.assertFalse(result["snapshot_complete"])
        self.assertTrue(result["truncated"])

    def test_multibyte_page_budget_and_no_zero_length_chunks(self):
        self.write([self.meta] + [message("🙂" * 50000) for _ in range(4)])
        chunks = {}
        for page in self.pages(self.read(limit=4)):
            self.assertLessEqual(sum(len(m["text"].encode("utf-8")) for m in page["messages"]), output.PAGE_BYTES)
            for msg in page["messages"]:
                self.assertTrue(msg["text"])
                chunks.setdefault(msg["message_id"], []).append((msg["chunk_offset"], msg["text"]))
        self.assertEqual(len(chunks), 4)
        for parts in chunks.values():
            self.assertEqual("".join(text for _, text in sorted(parts)), "🙂" * 50000)

    def test_corrupt_cursor_rejected(self):
        self.write([self.meta, message("one"), message("two")])
        first = self.read(limit=1)
        for cursor in ("not a cursor", first["next_cursor"][:-5],
                       output._encode_cursor({"version": 1, "path": str(self.path.resolve()),
                                              "session_id": "selected-session", "mode": "window",
                                              "dev": 0, "ino": 0, "size": 10,
                                              "digest": "0" * 64, "upper": "invalid"})):
            with self.assertRaisesRegex(ValueError, "Invalid"):
                self.read(cursor=cursor)

    def test_archive_exact_file_and_no_catalog_scan(self):
        archive = self.home / "archived_sessions" / "selected.jsonl"
        archive.parent.mkdir()
        archive.write_bytes(self.path.read_bytes())
        with patch.object(Path, "rglob", side_effect=AssertionError("must not scan")), \
                patch.object(Path, "glob", side_effect=AssertionError("must not scan")):
            result = output.main(dict(op="read_output", session_id="selected-session", rollout_path=str(archive),
                                      codex_home=str(self.home)))
        self.assertTrue(result["snapshot_complete"])

    def test_portable_stdin_source(self):
        self.write([self.meta, message("portable 你好")])
        import base64
        request = dict(op="read_output", session_id="selected-session", rollout_path=str(self.path), codex_home=str(self.home))
        args = base64.urlsafe_b64encode(json.dumps(request).encode()).decode()
        source = Path(output.__file__).read_text(encoding="utf-8")
        process = subprocess.run([sys.executable, "-", args], input=source, text=True, encoding="utf-8",
                                 capture_output=True, check=True, env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        envelope = json.loads(process.stdout)
        self.assertTrue(envelope["ok"])
        self.assertEqual(envelope["result"]["messages"][0]["text"], "portable 你好")


if __name__ == "__main__":
    unittest.main()
