"""Pure fake/local-owned controller tests: never run Codex, SSH or user state."""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from controllers import ControllerService, INITIALIZATION, resolve_codex_command, default_workspace_root, ROOT
from terminal_client import BrokerError
from store import Store

SID = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


class FakeDirectories:
    def __init__(self):
        self.calls = []

    def list(self, config, cwd):
        self.calls.append((config, cwd))
        return {"path": str(Path(cwd).resolve()) if config["transport"] == "local" else cwd}


class FakeHub:
    def __init__(self, root):
        self.runtime = Path(root)
        self.store = Store(self.runtime / "fake.sqlite")
        self.registry_lock = threading.RLock()
        self.hosts = {"local": {"transport": "local"}, "remote": {"transport": "ssh", "target": "fake"}}
        self.directories = FakeDirectories()
        self.revoked = []

    @staticmethod
    def agent_id(host, thread_id):
        return host + "::" + thread_id

    def split_agent_id(self, aid):
        if "::" not in aid:
            raise ValueError("Invalid worker")
        host, tid = aid.split("::", 1)
        self.host(host)
        return host, tid

    def host(self, host):
        if host not in self.hosts:
            raise ValueError("Unknown host")
        return self.hosts[host]

    @staticmethod
    def _directory(config, path):
        if not isinstance(path, str) or not path or any(ord(c) < 32 for c in path):
            raise ValueError("Invalid path")
        if not (path.startswith("/") if config["transport"] == "ssh" else Path(path).is_absolute()):
            raise ValueError("Absolute path required")

    def scope_revoking(self, cid, removed):
        self.revoked.append((cid, removed))


class FakeTerminals:
    def __init__(self):
        self.records = {}
        self.creates, self.writes = [], []
        self.create_error = None
        self.write_error = None
        self.on_create = None

    def create(self, body):
        self.creates.append(body)
        if not re.fullmatch(r"ceohub-[a-f0-9]{16,48}", body["session_id"]):
            raise ValueError("Broker terminal ID grammar")
        if body["session_id"] in self.records and not self.records[body["session_id"]]["connected"]:
            raise ValueError("Exited local PTY cannot be resurrected")
        if self.create_error:
            raise self.create_error
        row = {"id": body["session_id"], "connected": True, "agent_ready": True, "human_busy": False,
               "state": "agent", "command": "codex", "cols": 110, "rows": 32, "current_seq": 1,
               "screen": "safe fake terminal", "ansi": "safe fake terminal", "blocked_reason": None}
        self.records[row["id"]] = row
        if self.on_create:
            self.on_create(body)
        return row

    def request(self, path, timeout=2):
        return {"features": ["direct_program", "input_busy", "idle_close"]}

    def sessions(self):
        return list(self.records.values())

    def snapshot(self, tid, lines=50):
        return dict(self.records[tid])

    def output(self, tid, after=0):
        return self.snapshot(tid) | {"reset": after == 0, "data": "", "seq": 1}

    def write(self, tid, action, body):
        self.writes.append((tid, action, body))
        if self.write_error:
            raise self.write_error
        if action == "close":
            self.records[tid]["connected"] = False
            return {"id": tid, "closed": True}
        return {"submitted": True}


class FakeProcess:
    def __init__(self, events, callback=None, code=0, timeout=False, pause=None):
        self.stdout = io.StringIO("\n".join(json.dumps(e) if not isinstance(e, str) else e for e in events))
        self.callback, self.code, self.timeout, self.pause = callback, code, timeout, pause
        self.terminated = self.killed = False

    def wait(self, timeout=None):
        if self.pause:
            self.pause.wait(3)
        if self.callback:
            self.callback()
            self.callback = None
        if self.timeout and not self.terminated:
            raise subprocess.TimeoutExpired("owned fake", timeout)
        return self.code

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.hub, self.terminals = FakeHub(self.temp.name), FakeTerminals()
        self.launches = []
        self.events = [{"type": "thread.started", "thread_id": SID}, {"type": "turn.completed"}]
        self.code, self.handshake, self.timeout, self.pause = 0, True, False, None
        self.processes = []
        self.service = ControllerService(self.hub, terminals=self.terminals, launcher=self.launch)
        self.terminals.on_create = lambda body: self.service.mark_tool_seen(body["controller_id"])
        self.service.command_resolver = lambda: [str(Path(self.temp.name) / "fake-codex.exe")]
        self.service.set_origin("http://127.0.0.1:12345")

    def tearDown(self):
        if self.pause:
            self.pause.set()
        for thread in list(self.service._threads.values()):
            thread.join(5)
        self.service.shutdown()
        self.hub.store.close()
        self.temp.cleanup()

    def launch(self, argv, **kwargs):
        self.launches.append((argv, kwargs))
        cid = next(c["id"] for c in self.service.list() if c["workspace"] == kwargs["cwd"])
        callback = (lambda: self.service.mark_tool_seen(cid)) if self.handshake else None
        process = FakeProcess(self.events, callback, self.code, self.timeout, self.pause)
        self.processes.append(process)
        return process

    def worker(self, tid="worker", active=True, host="local", cwd=None):
        self.hub.store.remember(host, tid, "Worker", active=active, mode="terminal", cwd=cwd or str(Path(self.temp.name) / "business"))
        return host + "::" + tid

    def prepare(self, **body):
        return self.service.prepare(body)

    def started(self):
        c = self.prepare()
        self.service.start(c["id"], "start-1")
        self.settled(c["id"])
        return self.service.get(c["id"], refresh=True)

    def settled(self, cid):
        deadline = time.monotonic() + 5
        while cid in self.service._threads and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertNotIn(cid, self.service._threads)

    def test_owned_workspace_and_private_capability_no_response_leak(self):
        c = self.prepare()
        row = self.service._row(c["id"])
        descriptor = json.loads(Path(row["descriptor_file"]).read_text())
        self.assertEqual(c["state"], "prepared")
        self.assertEqual(self.service.authenticate(descriptor["token"]), c["id"])
        self.assertIsNone(self.service.authenticate("wrong" * 10))
        serialized = json.dumps(self.service.list())
        self.assertNotIn(descriptor["token"], serialized)
        self.assertNotIn("cap_hash", serialized)
        self.assertNotIn("descriptor_file", serialized)
        self.assertEqual(self.hub.store.agents(), [])
        workspace = Path(c["workspace"])
        self.assertIn("human", (workspace / "AGENTS.md").read_text())
        self.assertIn("PRON_HUB_CONNECTION_FILE", (workspace / ".codex/config.toml").read_text())
        if os.name != "nt":
            self.assertEqual(Path(row["descriptor_file"]).stat().st_mode & 0o777, 0o600)

    def test_default_primary_and_explicit_new(self):
        c = self.prepare()
        self.assertEqual(c["id"], self.prepare()["id"])
        self.assertNotEqual(c["id"], self.prepare(new=True)["id"])
        with self.assertRaises(ValueError):
            self.prepare(title="a different manager")

    def test_created_worker_attaches_without_revalidating_inactive_old_grants(self):
        old = self.worker('old-worker')
        c = self.prepare(agent_ids=[old])
        with self.hub.store.lock, self.hub.store.db:
            self.hub.store.db.execute('update agents set active=0 where thread_id=?', ('old-worker',))
        new = self.worker('new-worker')
        self.service.add_created_agent(c['id'], 'local', 'new-worker')
        self.assertEqual(set(self.service.allowed_agents(c['id'])), {old, new})
        self.assertFalse(self.service.authorize(c['id'], 'local', 'old-worker'))

    def test_manager_ancestor_directory_does_not_block_home_or_other_work(self):
        c = self.prepare()
        workspace = Path(c['workspace'])
        self.assertFalse(self.service.is_controller_reference('local', '', str(workspace.parent)))
        self.assertFalse(self.service.is_controller_reference('local', '', str(workspace.parent / 'other-project')))
        self.assertTrue(self.service.is_controller_reference('local', '', str(workspace)))
        self.assertTrue(self.service.is_controller_reference('local', '', str(workspace / 'nested')))

    def test_explicit_workspace_root_outside_checkout_and_only_owned_cid(self):
        base = Path(self.temp.name) / "separate-management-workspaces"
        service = ControllerService(self.hub, terminals=self.terminals, launcher=self.launch, workspace_root=base)
        service.set_origin("http://127.0.0.1:12345")
        c = service.prepare({})
        workspace = Path(c["workspace"])
        self.assertEqual(workspace, base / c["id"])
        self.assertFalse(workspace.resolve().is_relative_to(ROOT.resolve()))
        self.assertEqual([p.name for p in base.iterdir()], [c["id"]])
        self.assertTrue((workspace / "AGENTS.md").is_file())
        self.assertTrue((workspace / ".codex/config.toml").is_file())
        self.assertNotEqual(Path(service._row(c["id"])["descriptor_file"]).parent, workspace)

    def test_preexisting_workspace_file_is_never_overwritten(self):
        cid = SID
        workspace = self.service.workspace_root / cid
        workspace.mkdir()
        sentinel = workspace / "AGENTS.md"
        sentinel.write_text("user-owned guidance", encoding="utf-8")
        with patch("controllers.uuid.uuid4", return_value=cid):
            with self.assertRaises(FileExistsError):
                self.prepare()
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "user-owned guidance")
        self.assertEqual(self.service.list(), [])
        self.assertFalse((self.service.runtime / cid / "connection.json").exists())

    def test_default_workspace_root_explicit_runtime_is_owned_and_non_mutating(self):
        explicit = Path(self.temp.name) / "not-created"
        self.assertEqual(default_workspace_root(explicit), explicit / "workspaces")
        self.assertFalse(explicit.exists())
        if os.name == "nt":
            with patch.dict(os.environ, {"LOCALAPPDATA": str(explicit)}):
                selected = default_workspace_root()
                self.assertEqual(selected, explicit / "PRONHub" / "management-workspaces")
        else:
            with patch.dict(os.environ, {"XDG_STATE_HOME": str(explicit)}):
                selected = default_workspace_root()
                self.assertEqual(selected, explicit / "pron-hub" / "management-workspaces")
        self.assertFalse(explicit.exists())

    def test_prepare_request_key_conflict_and_replay(self):
        c = self.prepare(title="One", request_key="prepare-1")
        self.assertEqual(c["id"], self.prepare(title="One", request_key="prepare-1")["id"])
        with self.assertRaises(ValueError):
            self.prepare(title="Two", request_key="prepare-1")

    def test_scope_known_active_and_single_owner(self):
        aid = self.worker()
        c = self.prepare(agent_ids=[aid])
        self.assertFalse(self.service.authorize(c["id"], "local", "worker"))
        self.service.start(c["id"], "start-scope")
        self.settled(c["id"])
        self.assertTrue(self.service.authorize(c["id"], "local", "worker"))
        with self.assertRaises(PermissionError):
            self.prepare(new=True, agent_ids=[aid])
        c2 = self.prepare(new=True)
        self.assertFalse(self.service.authorize(c2["id"], "local", "worker"))
        self.assertFalse(self.service.authorize(c["id"], "local", "other"))
        self.hub.store.set_active("local", "worker", False)
        self.assertFalse(self.service.authorize(c["id"], "local", "worker"))

    def test_scope_rejects_inactive_unknown_and_closed(self):
        aid = self.worker(active=False)
        with self.assertRaises(ValueError):
            self.prepare(agent_ids=[aid])
        with self.assertRaises(ValueError):
            self.prepare(agent_ids=["local::missing"])

    def test_revoke_hook_before_scope_changes(self):
        aid = self.worker()
        c = self.prepare(agent_ids=[aid])
        self.service.set_scope(c["id"], [])
        self.assertEqual(self.hub.revoked, [(c["id"], [aid])])
        self.assertFalse(self.service.authorize(c["id"], "local", "worker"))

    def test_manager_cannot_be_worker_by_id_or_workspace(self):
        c = self.started()
        self.assertTrue(self.service.is_controller_reference("local", SID))
        self.assertTrue(self.service.is_controller_reference("local", c["terminal_id"]))
        aid = self.worker(tid="bad-alias", cwd=c["workspace"])
        with self.assertRaises(PermissionError):
            self.service.set_scope(c["id"], [aid])

    def test_host_permission_requires_explicit_canonical_root(self):
        c = self.prepare(allowed_hosts=["remote"])
        self.assertFalse(self.service.authorize_creation(c["id"], "remote", "/work/project"))
        self.service.set_scope(c["id"], [], host_roots={"remote": ["/work"]})
        self.service.start(c["id"], "start-roots")
        self.settled(c["id"])
        self.assertTrue(self.service.authorize_creation(c["id"], "remote", "/work/project"))
        self.assertFalse(self.service.authorize_creation(c["id"], "remote", "/workspace"))
        self.assertFalse(self.service.authorize_creation(c["id"], "remote", "/work/../other"))
        self.assertEqual(self.hub.directories.calls[-1][1], "/work")

    def test_bootstrap_exact_native_identity_read_only_and_tools_handshake(self):
        c = self.started()
        self.assertEqual(c["session_id"], SID)
        self.assertEqual(c["state"], "ready")
        self.assertTrue(c["tools_ready"])
        argv, kwargs = self.launches[0]
        self.assertIn("--json", argv)
        self.assertIn("read-only", argv)
        self.assertNotIn("--yolo", argv)
        self.assertNotIn("--ignore-user-config", argv)
        self.assertNotIn("env", kwargs)
        self.assertEqual(argv[-1], INITIALIZATION)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        body = self.terminals.creates[0]
        self.assertEqual(body["cwd"], c["workspace"])
        self.assertIn(SID, body["args"])
        self.assertNotIn(INITIALIZATION, body["args"])

    def test_duplicate_start_and_new_request_during_bootstrap_never_spawn_twice(self):
        self.pause = threading.Event()
        c = self.prepare()
        self.service.start(c["id"], "start-1")
        self.service.start(c["id"], "start-1")
        # The native SID can arrive before the bootstrap completes. A second
        # start may safely report pending rather than raise, but never attaches
        # or creates a second process while the first turn is running.
        self.service.start(c["id"], "start-2")
        self.pause.set()
        self.settled(c["id"])
        self.assertEqual(len(self.launches), 1)

    def test_missing_identity_unknown_and_different_key_no_retry(self):
        self.events = [{"type": "item.completed", "item": {"type": "agent_message", "text": "Session: " + SID}}]
        c = self.started()
        self.assertEqual(c["state"], "unknown")
        self.assertIsNone(c["session_id"])
        with self.assertRaises(RuntimeError):
            self.service.start(c["id"], "new-start")
        self.assertEqual(len(self.launches), 1)

    def test_inflight_start_before_native_identity_reports_same_operation(self):
        c = self.prepare()
        self.service._update(c['id'], state='bootstrapping', phase='native_session')
        with patch.dict(self.service._threads, {c['id']: object()}):
            result = self.service.start(c['id'], 'early-duplicate-start')
        self.assertEqual(result['id'], c['id'])
        self.assertEqual(result['state'], 'bootstrapping')
        self.assertIsNone(result['session_id'])
        self.assertEqual(self.launches, [])
        self.assertEqual(self.terminals.creates, [])

    def test_unclassified_failed_turn_is_not_success_even_with_zero_exit(self):
        self.events = [{'type': 'thread.started', 'thread_id': SID},
                       {'type': 'turn.failed', 'error': {'message': 'Synthetic unknown failure'}}]
        c = self.started()
        self.assertEqual(c['state'], 'failed')
        self.assertEqual(c['error']['code'], 'bootstrap_failed')
        self.assertEqual(c['session_id'], SID)
        self.assertEqual(self.terminals.creates, [])

    def test_reasoning_and_invalid_json_not_saved_or_identity(self):
        self.events = ["not-json", {"type": "item.completed", "item": {"type": "reasoning", "text": "PRIVATE THOUGHTS " + OTHER}},
                       {"type": "thread.started", "thread_id": SID}]
        c = self.started()
        self.assertEqual(c["session_id"], SID)
        self.assertNotIn("PRIVATE THOUGHTS", json.dumps(self.service.list()))
        self.assertNotIn(OTHER, json.dumps(self.service.list()))

    def test_conflicting_or_invalid_native_identity_is_not_ready(self):
        self.events += [{"type": "thread.started", "thread_id": OTHER}]
        c = self.started()
        self.assertEqual(c["state"], "unknown")
        self.assertEqual(c["error"]["code"], "invalid_session_event")
        self.assertEqual(self.terminals.creates, [])

    def test_tools_not_seen_never_claim_ready(self):
        self.handshake = False
        c = self.started()
        self.assertEqual(c["session_id"], SID)
        self.assertEqual(c["error"]["code"], "tools_unverified")
        self.assertFalse(c["tools_ready"])
        self.assertEqual(self.terminals.creates, [])

    def test_failed_bootstrap_keeps_session_and_resume_does_not_init_again(self):
        self.code = 1
        c = self.started()
        self.assertEqual(c["state"], "failed")
        self.assertEqual(c["session_id"], SID)
        resumed = self.service.resume(c["id"], "resume-1")
        self.assertEqual(resumed["session_id"], SID)
        self.assertEqual(len(self.launches), 1)
        self.assertNotIn(INITIALIZATION, self.terminals.creates[0]["args"])

    def test_timeout_terminates_only_owned_bootstrap(self):
        self.timeout = True
        c = self.started()
        self.assertEqual(c["error"]["code"], "bootstrap_timeout")
        self.assertTrue(self.processes[0].terminated)
        self.assertEqual(self.terminals.writes, [])

    def test_structured_error_classification_never_exposes_secret_error_body(self):
        self.events += [{"type": "turn.failed", "error": {"message": "Background server has incompatible feature settings SECRET_TOKEN"}}]
        self.code = 1
        c = self.started()
        self.assertEqual(c["error"]["code"], "daemon_settings_conflict")
        self.assertNotIn("SECRET_TOKEN", json.dumps(c))
        self.assertEqual(c["session_id"], SID)

    def test_bootstrap_handshake_does_not_prove_interactive_tool_readiness(self):
        self.terminals.on_create = None
        c = self.started()
        self.assertEqual(c["session_id"], SID)
        self.assertFalse(c["tools_ready"])
        self.assertEqual(c["state"], "needs_attention")
        self.assertEqual(c["error"]["code"], "tools_unverified")
        self.service.mark_tool_seen(c["id"])
        self.assertEqual(self.service.list()[0]["state"], "ready")

    def test_desktop_atomic_busy_rejection_preserves_hub_entry(self):
        c = self.started()
        self.terminals.write_error = BrokerError("raw secret", "human_busy", 409)
        with self.assertRaisesRegex(RuntimeError, "原输入入口保留"):
            self.service.desktop(c["id"], True)
        self.assertEqual(self.service.get(c["id"])["input_owner"], "hub")
        self.assertTrue(self.terminals.records[c["terminal_id"]]["connected"])

    def test_desktop_older_broker_rejected_before_close(self):
        c = self.started()
        self.terminals.request = lambda *args, **kwargs: {"features": ["direct_program"]}
        with self.assertRaisesRegex(RuntimeError, "尚不支持安全移交"):
            self.service.desktop(c["id"], True)
        self.assertEqual(self.terminals.writes, [])
        self.assertEqual(self.service.get(c["id"])["input_owner"], "hub")

    def test_terminal_unknown_uses_deterministic_id_and_no_blind_retry(self):
        self.terminals.create_error = RuntimeError("SECRET token in backend error")
        c = self.started()
        self.assertEqual(c["state"], "unknown")
        self.assertRegex(c["terminal_id"], r"^ceohub-[a-f0-9]{32}$")
        self.assertNotIn("SECRET", json.dumps(c))
        with self.assertRaises(RuntimeError):
            self.service.resume(c["id"], "resume-1")
        self.assertEqual(len(self.terminals.creates), 1)

    def test_same_session_resume_idempotent_and_no_worker_close(self):
        c = self.started()
        self.service.resume(c["id"], "resume-1")
        self.service.resume(c["id"], "resume-1")
        self.assertEqual(len(self.terminals.creates), 1)
        self.service.close(c["id"], True)
        self.assertEqual([w[0] for w in self.terminals.writes], [c["terminal_id"]])
        closed = self.service.get(c["id"])
        self.assertEqual(closed["state"], "closed")
        descriptor = json.loads(Path(self.service._row(c["id"])["descriptor_file"]).read_text())
        self.assertIsNone(self.service.authenticate(descriptor["token"]))
        self.assertEqual(self.service.resume(c["id"], "return-1")["session_id"], SID)
        self.assertEqual(len(self.launches), 1)

    def test_desktop_requires_confirm_ready_and_no_draft_truthful_state(self):
        c = self.started()
        with self.assertRaises(ValueError):
            self.service.desktop(c["id"])
        self.terminals.records[c["terminal_id"]]["human_busy"] = True
        with self.assertRaises(RuntimeError):
            self.service.desktop(c["id"], True)
        self.terminals.records[c["terminal_id"]]["human_busy"] = False
        result = self.service.desktop(c["id"], True)
        self.assertEqual(result["url"], "codex://threads/" + SID)
        self.assertEqual(result["state"], "desktop_unverified")
        self.assertFalse(result["desktop_verified"])
        self.assertEqual(result["input_owner"], "desktop")
        with self.assertRaises(PermissionError):
            self.service.input(c["id"], "should not enter second writer")
        self.assertEqual(self.service.resume(c["id"], "return-1")["session_id"], SID)

    def test_desktop_close_unknown_never_opens_or_restarts_other_entry(self):
        c = self.started()
        self.terminals.write_error = RuntimeError("secret credential")
        with self.assertRaisesRegex(RuntimeError, "释放结果未知"):
            self.service.desktop(c["id"], True)
        self.assertEqual(self.service.get(c["id"])["state"], "unknown")
        self.assertEqual(len(self.terminals.creates), 1)

    def test_setup_menu_is_not_ready_even_when_codex_process_exists(self):
        c = self.started()
        self.terminals.records[c["terminal_id"]].update(agent_ready=False, blocked_reason="daemon_settings_conflict")
        after = self.service.get(c["id"], refresh=True)
        self.assertEqual(after["state"], "needs_attention")
        self.assertFalse(after["agent_ready"])

    def test_context_scoped_self_safe_not_worker(self):
        c = self.prepare()
        context = self.service.view_context(c["id"])
        self.assertEqual(context["mode"], "scoped")
        self.assertEqual(context["controller_id"], c["id"])
        self.assertFalse(context["policy"]["operating_system_isolation"])
        self.assertEqual(self.hub.store.agents(), [])

    def test_shutdown_does_not_kill_manager_or_workers(self):
        c = self.started()
        self.service.shutdown()
        self.assertEqual(self.terminals.writes, [])
        self.assertFalse(self.processes[0].terminated)
        self.assertTrue(self.terminals.records[c["terminal_id"]]["connected"])

    def test_restart_does_not_repeat_inflight_launch_and_updates_only_owned_descriptor(self):
        c = self.prepare()
        self.service._update(c["id"], state="bootstrapping")
        new = ControllerService(self.hub, terminals=self.terminals, launcher=self.launch)
        new.set_origin("http://127.0.0.1:23456")
        self.assertEqual(new.get(c["id"])["state"], "unknown")
        descriptor = json.loads(Path(new._row(c["id"])["descriptor_file"]).read_text())
        self.assertEqual(descriptor["url"], "http://127.0.0.1:23456")
        self.assertEqual(self.launches, [])

    def test_origin_refuses_remote_and_credential_urls(self):
        for url in ("https://127.0.0.1:1", "http://example.test:1", "http://secret@127.0.0.1:1", "http://127.0.0.1:1/?token=x"):
            with self.assertRaises(ValueError):
                self.service.set_origin(url)

    def test_resolver_missing_never_uses_shell_or_unknown_cmd(self):
        with patch("controllers.shutil.which", return_value=None):
            with self.assertRaises(RuntimeError):
                resolve_codex_command()


if __name__ == "__main__":
    unittest.main()
