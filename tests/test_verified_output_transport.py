"""Execute one-SSH output transactions locally; no network, tmux or worker input."""
import base64
import copy
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from agent_captain.core.terminal_hub import Hub, HistoryReadError
from agent_captain.paths import source_path
from agent_captain.sessions.response_wire import ENCODING, decode_reply, encode_reply
from agent_captain.transport.ssh_source import pack_python_source
from tests.test_ssh_source import unpack_source
from tests.test_terminal_hub import FakeTerminals


TERMINAL = "ceohub-0123456789abcdef01234567"
SESSION = "11111111-2222-4333-8444-555555555555"
SECRET = "FIXTURE_PRIVATE_DIAGNOSTIC_NOT_FOR_OUTPUT"


def reply(value=None, *, stdout=None, stderr="", returncode=0):
    return types.SimpleNamespace(returncode=returncode, stderr=stderr,
        stdout=json.dumps({"ok": True, "result": value}, ensure_ascii=False) if stdout is None else stdout)


def event(kind, payload):
    return {"type": kind, "timestamp": "2026-10-18T10:00:00Z", "payload": payload}


def message(text):
    return event("response_item", {"type": "message", "role": "assistant", "channel": "final",
        "content": [{"type": "output_text", "text": text}]})


class VerifiedOutputTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "isolated-codex-home"
        self.rollout = self.home / "sessions" / "selected.jsonl"
        self.rollout.parent.mkdir(parents=True)
        self.append([event("session_meta", {"id": SESSION, "cwd": "/fixture-project", "source": "cli"}),
                     message("OLDER: 完整历史🙂"),
                     event("response_item", {"type": "function_call_output", "call_id": "fixture-tool", "output": "COMPLETE TOOL OUTPUT"}),
                     message("LATEST: full report, not a visible tail")])
        self.terminals = FakeTerminals()
        self.hub = Hub(self.root / "hub", terminals=self.terminals)
        self.addCleanup(self.hub.close)
        # No periodic thread participates in call counts or fixture state.
        self.hub._stop.set()
        self.hub._watcher.join(3)
        self.hub.hosts = {"remote": {"transport": "ssh", "target": "isolated-test-alias", "label": "Remote"}}
        self.terminals.add(TERMINAL, host="remote", screen="VISIBLE TAIL ONLY")
        self.hub.store.remember("remote", "worker", "Fixture", mode="terminal", terminal_id=TERMINAL)
        self.binding = {"state": "bound", "binding_evidence": "held_rollout", "session_id": SESSION,
            "rollout_path": str(self.rollout), "codex_home": str(self.home), "source": "cli", "cwd": "/fixture-project",
            "terminal_id": TERMINAL, "historical": False, "live_session_verified": True,
            "process_identity": {"pid": 4242, "start_ticks": 100, "pane_pid": 1234, "pane_start_ticks": 99}}
        self.output = {"id": SESSION, "source": "codex_rollout", "messages": [{"role": "assistant", "text": "FULL SEMANTIC BODY"}],
            "content_complete": True, "history_complete": True, "status": "completed_in_history"}

    def append(self, records):
        with self.rollout.open("a", encoding="utf-8", newline="\n") as stream:
            for record in records:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    def request(self, **changes):
        return {"op": "read_bound_output", "terminal_id": TERMINAL, "saved_binding": None,
                "historical_allowed": False, "limit": 18, "include_tools": False, **changes}

    def batch(self):
        return {"state": "bound", "binding": copy.deepcopy(self.binding), "output": copy.deepcopy(self.output)}

    def assert_read_only(self):
        self.assertEqual(self.terminals.writes, [])
        self.assertFalse(any(call[0] in ("create", "write", "close") for call in self.terminals.calls))

    def test_fresh_cursor_after_and_tool_reads_each_use_one_semantic_ssh_transaction(self):
        for paging in ({}, {"cursor": "opaque-original-page"}, {"after_cursor": "opaque-original-after"}, {"include_tools": True}):
            with self.subTest(paging=paging):
                self.terminals.calls.clear()
                with patch("agent_captain.core.terminal_hub.subprocess.run", return_value=reply(self.batch())) as run, \
                     patch.object(self.hub, "_probe_output", side_effect=AssertionError("old bind-read-bind SSH path must not run")):
                    result = self.hub.read_agent("remote", "worker", limit=7, **paging)
                self.assertEqual(run.call_count, 1, "The display snapshot is independent; semantic output uses one SSH")
                self.assertEqual(run.call_args.kwargs["timeout"], 75)
                args = run.call_args.args[0]
                remote = shlex.split(args[-1])
                self.assertEqual(remote[:2], ["python3", "-"])
                sent = json.loads(base64.urlsafe_b64decode(remote[2]))
                self.assertEqual(sent["op"], "read_bound_output")
                self.assertEqual(sent["terminal_id"], TERMINAL)
                self.assertEqual(sent["limit"], 7)
                self.assertEqual(sent.get("include_tools"), paging.get("include_tools", False))
                for key in ("cursor", "after_cursor"):
                    self.assertEqual(sent.get(key), paging.get(key))
                self.assertEqual(result["messages"], self.output["messages"])
                self.assertTrue(result["output_binding"]["live_session_verified"])
                self.assertEqual(result["screen"], "VISIBLE TAIL ONLY")
                self.assertEqual(result["read_transport"]["attempts"], 1)
                self.assertEqual(sum(call[0] == "snapshot" for call in self.terminals.calls), 1)
                self.assert_read_only()

    def test_real_four_module_catalog_bootstrap_runs_compressed_paged_reads_in_isolated_python(self):
        actual_run = subprocess.run
        transactions = []
        responses = []

        def local_python(args, **kwargs):
            self.assertEqual(args[0], "ssh")
            self.assertEqual(args[-2], "isolated-test-alias")
            self.assertEqual(kwargs["timeout"], 75)
            remote = shlex.split(args[-1])
            self.assertEqual(remote[:2], ["python3", "-"])
            self.assertLess(len(args[-1]), 16384, "Portable source belongs only in stdin")
            sent = json.loads(base64.urlsafe_b64decode(remote[2]))
            self.assertEqual(sent["op"], "read_bound_output")
            source = unpack_source(kwargs["input"])
            for name in ("session_binding", "conversation_output", "verified_output", "response_wire"):
                self.assertIn("types.ModuleType(" + repr(name) + ")", source)
                self.assertIn(repr(source_path("sessions", name + ".py").read_text(encoding="utf-8")), source)
            self.assertIn('if request["op"] == "read_bound_output":', source)
            before, marker, after = source.rpartition('\nif __name__ == "__main__":')
            self.assertTrue(marker, "Keep the real catalog argv/JSON envelope entrypoint")
            encoded_binding = base64.b64encode(json.dumps(self.binding).encode()).decode()
            # Replace only kernel/process evidence in the isolated child. The
            # real verified-output orchestration, pagination and JSONL reader
            # execute unchanged, including both sides of the identity fence.
            fixture = (
                "\nimport session_binding as _fixture_binding, conversation_output as _fixture_output, verified_output as _fixture_verified\n"
                "_fixture_trace=[]\n"
                f"_fixture_proof=json.loads(base64.b64decode({encoded_binding!r}))\n"
                "def _fixture_bind(request):\n"
                f" assert request=={{'op':'bind_output','terminal_id':{TERMINAL!r}}}\n"
                " _fixture_trace.append('bind');return dict(_fixture_proof)\n"
                "_fixture_binding.main=_fixture_bind\n"
                "_fixture_original_read=_fixture_output.main\n"
                "def _fixture_read(request):\n"
                " _fixture_trace.append('read');return _fixture_original_read(request)\n"
                "_fixture_output.main=_fixture_read\n"
                "_fixture_original_main=_fixture_verified.main\n"
                "def _fixture_transaction(request):\n"
                " result=_fixture_original_main(request)\n"
                " assert _fixture_trace==['bind','read','bind'],_fixture_trace\n"
                " return result\n"
                "_fixture_verified.main=_fixture_transaction\n"
                "def _fixture_no_subprocess(*args,**kwargs):\n"
                " raise AssertionError('No tmux, SSH, model or attachment may be started')\n"
                "import subprocess as _fixture_subprocess\n"
                "_fixture_subprocess.run=_fixture_no_subprocess\n"
                "assert sys.stdin.read()==''\n"
            )
            local_options = {**kwargs, "input": pack_python_source(before + fixture + marker + after),
                             "timeout": 10, "env": dict(os.environ, PYTHONIOENCODING="utf-8"), "cwd": self.root}
            response = actual_run([sys.executable, "-I", "-X", "utf8", *remote[1:]], **local_options)
            self.assertEqual(response.returncode, 0, response.stderr)
            self.assertEqual(response.stderr, "")
            wire = json.loads(response.stdout)
            self.assertEqual(wire.get("encoding"), ENCODING, "Only the batch entrypoint compresses its response envelope")
            envelope = decode_reply(response.stdout)
            self.assertLess(len(response.stdout.encode("utf-8")), len(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")),
                            "The real returned response is smaller than its decoded JSON body")
            responses.append(envelope)
            self.assertTrue(envelope.get("ok"), envelope)
            self.assertEqual(envelope["result"]["state"], "bound", envelope)
            transactions.append(sent)
            return response

        def read(**options):
            before = len(transactions)
            try:
                result = self.hub.read_agent("remote", "worker", **options)
            except ValueError:
                self.fail(repr(responses[-1] if responses else "No portable transaction response"))
            self.assertEqual(len(transactions), before + 1)
            self.assertEqual(result["source"], "codex_rollout")
            self.assertTrue(result["output_binding"]["live_session_verified"])
            self.assert_read_only()
            return result

        with patch("agent_captain.core.terminal_hub.subprocess.run", side_effect=local_python):
            first = read(limit=1)
            self.assertEqual([row["text"] for row in first["messages"]], ["LATEST: full report, not a visible tail"])
            older = read(limit=1, cursor=first["next_cursor"])
            self.assertEqual([row["text"] for row in older["messages"]], ["OLDER: 完整历史🙂"])
            self.append([message("APPENDED AFTER THE ORIGINAL READ")])
            appended = read(after_cursor=first["after_cursor"])
            self.assertEqual([row["text"] for row in appended["messages"]], ["APPENDED AFTER THE ORIGINAL READ"])
            tools = read(include_tools=True)
            self.assertIn("COMPLETE TOOL OUTPUT", [row["text"] for row in tools["messages"]])
        self.assertEqual(len(transactions), 4)

    def test_only_batched_read_retries_transient_ssh_failures_once_with_the_same_request(self):
        failures = (
            subprocess.TimeoutExpired(SECRET, 45, output=SECRET, stderr=SECRET),
            BrokenPipeError(SECRET),
            reply(stdout=SECRET, stderr="kex_exchange_identification: Connection reset " + SECRET, returncode=255),
            reply(stdout=SECRET, stderr="ssh: connect to host example port 22: Connection refused " + SECRET, returncode=255),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                with patch("agent_captain.core.terminal_hub.subprocess.run", side_effect=[failure, reply(self.batch())]) as run:
                    result = self.hub.history("remote", self.request(cursor="same-original-cursor", include_tools=True))
                self.assertEqual(run.call_count, 2)
                first, retry = run.call_args_list
                self.assertEqual(first.args, retry.args, "The bounded retry preserves its target and request cursor")
                self.assertEqual(first.kwargs["timeout"], 75)
                self.assertEqual(retry.kwargs["timeout"], 45)
                self.assertEqual({key: value for key, value in first.kwargs.items() if key != "timeout"},
                                 {key: value for key, value in retry.kwargs.items() if key != "timeout"},
                                 "The shorter retry still sends identical source, payload and encoding options")
                self.assertEqual(result["read_transport"]["attempts"], 2)
                self.assertTrue(result["read_transport"]["batched_identity_checks"])
                self.assertNotIn(SECRET, json.dumps(result))
                self.assert_read_only()
        with patch("agent_captain.core.terminal_hub.subprocess.run", side_effect=subprocess.TimeoutExpired(SECRET, 45)) as run:
            with self.assertRaises(HistoryReadError):
                self.hub.history("remote", {"op": "bind_output", "terminal_id": TERMINAL})
        self.assertEqual(run.call_count, 1, "The retry policy cannot spread to other operations")

    def test_transient_failure_exhausts_after_two_attempts_without_changing_display_or_sending_input(self):
        with patch("agent_captain.core.terminal_hub.subprocess.run", side_effect=subprocess.TimeoutExpired(SECRET, 45)) as run:
            result = self.hub.read_agent("remote", "worker")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(result["messages"], [])
        self.assertEqual(result["output_binding"]["reason_code"], "ssh_timeout")
        self.assertTrue(result["connected"])
        self.assertNotIn(SECRET, json.dumps(result))
        self.assert_read_only()

    def test_auth_hostkey_protocol_and_remote_cursor_failures_never_retry(self):
        responses = (
            (reply(stdout=SECRET, stderr="Permission denied (publickey). " + SECRET, returncode=255), "ssh_authentication_failed"),
            (reply(stdout=SECRET, stderr="Host key verification failed. " + SECRET, returncode=255), "ssh_host_key_failed"),
            (reply(stdout=SECRET), "protocol_invalid"),
            (reply(stdout=json.dumps({"ok": False, "error": "Invalid or differently bound output cursor. " + SECRET})), "remote_read_failed"),
            (reply(stdout=SECRET, stderr="python3: command not found " + SECRET, returncode=127), "remote_read_failed"),
        )
        for response, reason in responses:
            with self.subTest(reason=reason), patch("agent_captain.core.terminal_hub.subprocess.run", return_value=response) as run:
                with self.assertRaises(HistoryReadError) as caught:
                    self.hub.history("remote", self.request(cursor="unchanged-page-cursor"))
                self.assertEqual(run.call_count, 1)
                self.assertEqual(caught.exception.reason_code, reason)
                self.assertNotIn(SECRET, str(caught.exception))
                self.assert_read_only()

    def test_compressed_success_decodes_and_invalid_compressed_protocol_never_retries(self):
        encoded = encode_reply({"ok": True, "result": self.batch()})
        with patch("agent_captain.core.terminal_hub.subprocess.run", return_value=reply(stdout=encoded)) as run:
            result = self.hub.history("remote", self.request())
        self.assertEqual(result["output"]["messages"], self.output["messages"])
        self.assertEqual(run.call_count, 1)
        unknown_encoding = json.loads(encoded)
        unknown_encoding["encoding"] = "invalid-fixture-encoding"
        corrupt_data = json.loads(encoded)
        corrupt_data["data"] = base64.b64encode(("invalid-zlib-body " + SECRET).encode()).decode()
        for bad in (encoded[:-5], json.dumps(unknown_encoding), json.dumps(corrupt_data)):
            with self.subTest(wire=bad[:30]), patch("agent_captain.core.terminal_hub.subprocess.run", return_value=reply(stdout=bad)) as run:
                with self.assertRaises(HistoryReadError) as caught:
                    self.hub.history("remote", self.request())
                self.assertEqual(caught.exception.reason_code, "protocol_invalid")
                self.assertNotIn(SECRET, str(caught.exception))
                self.assertEqual(run.call_count, 1)
                self.assert_read_only()

    def test_semantic_identity_failures_never_retry_or_fall_back_to_visible_output(self):
        for code in ("session_identity_changed", "output_binding_invalid", "remote_read_failed"):
            with self.subTest(code=code), patch("agent_captain.core.terminal_hub.subprocess.run",
                    return_value=reply({"state": "unavailable", "reason_code": code})) as run:
                result = self.hub.read_agent("remote", "worker")
            self.assertEqual(run.call_count, 1)
            self.assertEqual(result["messages"], [])
            self.assertFalse(result["content_complete"])
            self.assertFalse(result["history_complete"])
            self.assertTrue(result["connected"])
            self.assertEqual(result["screen"], "VISIBLE TAIL ONLY")
            self.assert_read_only()


if __name__ == "__main__":
    unittest.main()
