"""Recovery is opt-in, fails closed, and only replaces a proven-dead display owner."""
import errno
import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch
from urllib.error import URLError

from terminal_client import BrokerError, TerminalClient
from process_identity import broker_owner_state, observe_process
from background_process import BackgroundLaunchUnknown
import terminal_service


class BoundTerminalRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.descriptor = self.root / "owner.json"
        self.reference = self.root / "worker-reference.json"
        self.lockfile = Path(str(self.descriptor) + ".lock")
        self.maintenance = Path(str(self.descriptor) + ".recovery.lock")
        self.env = patch.dict(os.environ, {"CEO_HUB_TERMINAL_FILE": ""})
        self.env.start()
        self.ref_patch = patch("terminal_client.worker_reference_path", return_value=self.reference)
        self.ref_patch.start()
        self.write_descriptor()
        self.reference.write_text(json.dumps({"descriptor_file": str(self.descriptor)}), encoding="utf-8")
        self.client = TerminalClient(runtime=self.root / "logs")

    def tearDown(self):
        self.ref_patch.stop()
        self.env.stop()
        self.temp.cleanup()

    def write_descriptor(self, pid=123, **changes):
        data = {"url": "http://127.0.0.1:39001", "token": "fixture-capability-not-real-012345", "pid": pid, "version": "0.3.0"}
        data.update(changes)
        self.descriptor.write_text(json.dumps(data), encoding="utf-8")
        return data

    @staticmethod
    def refused(*args, **kwargs):
        raise URLError(ConnectionRefusedError(errno.ECONNREFUSED, "fixture refused"))

    def offline(self):
        return (patch.object(self.client, "request", side_effect=self.refused),
                patch.object(self.client, "_pid_state", return_value="dead"),
                patch.object(self.client, "_port_state", return_value="refused"))

    def test_status_is_read_only_and_public_metadata_has_no_private_data(self):
        previous = (self.descriptor.read_bytes(), self.reference.read_bytes())
        request, pid, port = self.offline()
        with request, pid, port, patch("terminal_client.launch_background", side_effect=AssertionError("read only")):
            status = self.client.bound_owner_status()
        self.assertEqual((status["state"], status["code"]), ("offline", "bound_owner_offline"))
        self.assertTrue(status["recoverable"])
        self.assertFalse(status["recovered"])
        self.assertEqual(previous, (self.descriptor.read_bytes(), self.reference.read_bytes()))
        self.assertFalse(self.maintenance.exists())
        for private in ("token", "descriptor_file", "sha256", "fixture-capability", str(self.root)):
            self.assertNotIn(private, json.dumps(status))

    def test_explicit_confirmation_is_required_before_even_inspecting_owner(self):
        with patch.object(self.client, "_bound_context", side_effect=AssertionError("confirmation first")):
            for confirm in (False, None, 1, "yes"):
                self.assertEqual(self.client.recover_bound_owner(confirm=confirm)["code"], "confirmation_required")

    def test_authenticated_healthy_bound_owner_is_a_noop_without_recovery_receipt(self):
        with patch.object(self.client, "request", return_value={"ok": True, "pid": 123, "version": "0.3.0"}) as request, \
                patch("terminal_client.launch_background", side_effect=AssertionError("healthy owner must stay")):
            status = self.client.recover_bound_owner(confirm=True)
        request.assert_called_once_with("/health", timeout=4)
        self.assertEqual(status["state"], "ready")
        self.assertFalse(status["owner_recovered"])
        self.assertFalse(status["recovered"])
        self.assertIsNone(status["previous_pid"])
        self.assertIsNone(status["recovery_id"])

    def test_socket_refusal_observation_waits_four_seconds_without_broadening_classification(self):
        for result, expected in ((0, "open"), (errno.ECONNREFUSED, "refused"), (10061, "refused"),
                                 (10035, "unknown"), (errno.EINPROGRESS, "unknown"), (errno.ETIMEDOUT, "unknown")):
            with self.subTest(result=result), patch("terminal_client.socket.socket", return_value=MagicMock()) as factory:
                probe = factory.return_value.__enter__.return_value
                probe.connect_ex.return_value = result
                self.assertEqual(self.client._port_state("http://127.0.0.1:39001"), expected)
                probe.settimeout.assert_called_once_with(4)
                probe.connect_ex.assert_called_once_with(("127.0.0.1", 39001))

    def test_socket_timeout_or_os_error_is_never_treated_as_refusal(self):
        for error in (TimeoutError("fixture timeout"), OSError("fixture unknown socket failure")):
            with self.subTest(error=type(error).__name__), patch("terminal_client.socket.socket", return_value=MagicMock()) as factory:
                probe = factory.return_value.__enter__.return_value
                probe.connect_ex.side_effect = error
                self.assertEqual(self.client._port_state("http://127.0.0.1:39001"), "unknown")
                probe.settimeout.assert_called_once_with(4)

    def test_affirmative_refusal_after_longer_health_budget_is_still_required(self):
        with patch.object(self.client, "request", side_effect=self.refused) as request, \
                patch.object(self.client, "_pid_state", return_value="dead"), \
                patch.object(self.client, "_port_state", return_value="refused"), \
                patch("terminal_client.launch_background", side_effect=AssertionError("status does not start recovery")):
            status = self.client.bound_owner_status()
        self.assertEqual(status["code"], "bound_owner_offline")
        request.assert_called_once_with("/health", timeout=4)

    def test_live_or_uninspectable_pid_never_gets_replaced(self):
        for state in ("alive", "unknown"):
            with self.subTest(state=state), patch.object(self.client, "request", side_effect=self.refused), \
                    patch.object(self.client, "_owner_state", return_value=state), \
                    patch("terminal_client.launch_background", side_effect=AssertionError("no kill or replacement")):
                result = self.client.recover_bound_owner(confirm=True)
            self.assertFalse(result["can_recover"])
            self.assertEqual(result["code"], "bound_owner_alive" if state == "alive" else "bound_owner_pid_unknown")

    def test_reachable_unauthorized_malformed_or_timed_out_health_is_not_offline(self):
        for error in (BrokerError("Denied", "unauthorized", 401), URLError(TimeoutError()), None):
            with self.subTest(error=type(error).__name__), patch.object(self.client, "request", side_effect=error,
                    return_value={"ok": True, "pid": 999, "version": "0.3.0"}), \
                    patch.object(self.client, "_pid_state", return_value="dead"), \
                    patch.object(self.client, "_port_state", return_value="refused"), \
                    patch("terminal_client.launch_background", side_effect=AssertionError("not proof of offline")):
                result = self.client.recover_bound_owner(confirm=True)
            self.assertEqual(result["code"], "bound_owner_health_unknown")

    def test_failed_tcp_probe_or_open_endpoint_never_permits_replacement(self):
        for port_state in ("open", "unknown"):
            with self.subTest(port_state=port_state), patch.object(self.client, "request", side_effect=self.refused), \
                    patch.object(self.client, "_pid_state", return_value="dead"), \
                    patch.object(self.client, "_port_state", return_value=port_state), \
                    patch("terminal_client.launch_background", side_effect=AssertionError("port not refused")):
                result = self.client.recover_bound_owner(confirm=True)
            self.assertEqual(result["code"], "bound_owner_health_unknown")

    def test_unknown_foreign_or_oversized_broker_lock_is_never_deleted(self):
        for content in (b"", b"malformed", b"456", b"123 " * 30, b"\xff"):
            self.lockfile.write_bytes(content)
            request, pid, port = self.offline()
            with self.subTest(content=content[:10]), request, pid, port, \
                    patch("terminal_client.launch_background", side_effect=AssertionError("lock remains")):
                result = self.client.recover_bound_owner(confirm=True)
            self.assertEqual(result["code"], "bound_owner_lock_unknown")
            self.assertEqual(self.lockfile.read_bytes(), content)

    def test_unknown_existing_maintenance_lock_is_never_reclaimed(self):
        self.maintenance.write_text("other unfinished recovery", encoding="ascii")
        request, pid, port = self.offline()
        with request, pid, port, patch("terminal_client.launch_background", side_effect=AssertionError("busy")):
            result = self.client.recover_bound_owner(confirm=True)
        self.assertEqual(result["code"], "bound_owner_recovery_busy")
        self.assertEqual(self.maintenance.read_text(), "other unfinished recovery")

    def test_changed_or_invalid_reference_wire_version_and_manager_override_fail_closed(self):
        for changes in ({"version": "0.2.0"}, {"url": "http://localhost:3"}, {"pid": -1}):
            self.write_descriptor(**changes)
            with self.subTest(changes=changes), patch("terminal_client.launch_background", side_effect=AssertionError("invalid")):
                self.assertEqual(self.client.recover_bound_owner(confirm=True)["code"], "bound_owner_reference_invalid")
        self.write_descriptor()
        self.reference.write_text('{"descriptor_file":"relative.json"}')
        self.assertEqual(self.client.bound_owner_status()["code"], "bound_owner_reference_invalid")
        self.reference.write_text(json.dumps({"descriptor_file": str(self.descriptor)}))
        with patch.dict(os.environ, {"CEO_HUB_TERMINAL_FILE": str(self.descriptor)}):
            self.assertEqual(self.client.recover_bound_owner(confirm=True)["code"], "bound_owner_reference_invalid")
        self.assertEqual(TerminalClient(descriptor_file=self.descriptor).recover_bound_owner(confirm=True)["code"], "bound_owner_reference_invalid")

    def test_stale_expected_owner_rejects_before_starting(self):
        for owner in ({"pid": 999}, {"pid": 123, "version": "0.1.0"}, {"pid": 123, "token": "not allowed"}):
            with self.subTest(owner=owner), patch("terminal_client.launch_background", side_effect=AssertionError("stale action")):
                self.assertEqual(self.client.recover_bound_owner(confirm=True, expected_owner=owner)["code"], "bound_owner_changed")

    def test_reference_change_during_offline_proof_prevents_spawn(self):
        def changed_port(*args):
            self.reference.write_text(json.dumps({"descriptor_file": str(self.descriptor)}) + "\n")
            return "refused"
        with patch.object(self.client, "request", side_effect=self.refused), patch.object(self.client, "_pid_state", return_value="dead"), \
                patch.object(self.client, "_port_state", side_effect=changed_port), \
                patch("terminal_client.launch_background", side_effect=AssertionError("changed pin")):
            result = self.client.recover_bound_owner(confirm=True)
        self.assertEqual(result["code"], "bound_owner_changed")

    def test_proven_dead_owner_recovers_same_namespace_with_child_only_guard(self):
        self.write_descriptor(recovery_id="c" * 32)
        self.lockfile.write_text("123", encoding="ascii")
        reference_before = self.reference.read_bytes()
        environment_before = dict(os.environ)
        process = Mock(pid=456)
        process.poll.return_value = None
        def health(*args, **kwargs):
            if self.client.descriptor()["pid"] == 123:
                return self.refused()
            if args[0] == "/sessions":
                return {"sessions": []}
            return {"ok": True, "pid": 456, "version": "0.3.0", "recovery_id": self.client.descriptor()["recovery_id"]}
        def publish(*args, **kwargs):
            guard = json.loads(kwargs["env_overrides"]["CEO_HUB_RECOVERY_GUARD"])
            self.write_descriptor(pid=456, recovery_id=guard["recovery_id"])
            return process
        with patch.object(self.client, "request", side_effect=health), patch.object(self.client, "_pid_state", return_value="dead"), \
                patch.object(self.client, "_port_state", return_value="refused"), patch("terminal_client.shutil.which", return_value="fake-node"), \
                patch("terminal_client.launch_background", side_effect=publish) as spawn:
            result = self.client.recover_bound_owner(confirm=True, expected_owner={"pid": 123, "version": "0.3.0"})
        self.assertEqual((result["state"], result["code"]), ("ready", "bound_owner_recovered"))
        self.assertTrue(result["recovered"])
        self.assertEqual((result["previous_pid"], result["pid"]), (123, 456))
        self.assertEqual(result["previous_recovery_id"], "c" * 32)
        self.assertEqual(len(result["recovery_id"]), 32)
        self.assertFalse(result["sessions_restored"])
        self.assertTrue(result["started_empty"])
        self.assertEqual(self.reference.read_bytes(), reference_before)
        self.assertEqual(dict(os.environ), environment_before)
        self.assertFalse(self.maintenance.exists())
        child = spawn.call_args.kwargs["env_overrides"]
        self.assertEqual(child["CEO_HUB_TERMINAL_FILE"], str(self.descriptor))
        guard = json.loads(child["CEO_HUB_RECOVERY_GUARD"])
        self.assertEqual(guard["pid"], 123)
        self.assertEqual(guard["lock_sha256"], hashlib.sha256(b"123").hexdigest())
        self.assertNotIn("token", guard)
        self.assertEqual(spawn.call_args.args[0][0], "fake-node")
        self.assertTrue(spawn.call_args.args[0][1].endswith("terminal_broker.cjs"))
        process.kill.assert_not_called()
        process.terminate.assert_not_called()

    def test_child_failure_and_unknown_readiness_never_claim_receipt_or_kill(self):
        for exited in (True, False):
            process = Mock(pid=456)
            process.poll.return_value = 1 if exited else None
            request, pid, port = self.offline()
            clock = None if exited else patch("terminal_client.time.monotonic", side_effect=[0, 11])
            with request, pid, port, patch("terminal_client.shutil.which", return_value="fake-node"), \
                    patch("terminal_client.launch_background", return_value=process):
                if clock:
                    with clock:
                        result = self.client.recover_bound_owner(confirm=True)
                else:
                    result = self.client.recover_bound_owner(confirm=True)
            self.assertEqual(result["state"], "unknown")
            self.assertFalse(result["recovered"])
            self.assertIsNone(result["recovery_id"])
            self.assertEqual(self.maintenance.exists(), not exited)
            process.kill.assert_not_called()
            process.terminate.assert_not_called()

    def test_unconfirmed_launcher_outcome_is_unknown_and_retains_the_original_fence(self):
        for error in (RuntimeError("fixture launch outcome unknown"), TimeoutError("fixture WMI response unknown"),
                      BackgroundLaunchUnknown("fixture receipt unknown", launch_id="private-launch", receipt_path=self.root / "private-receipt.json")):
            with self.subTest(error=type(error).__name__):
                request, pid, port = self.offline()
                descriptor_before, reference_before = self.descriptor.read_bytes(), self.reference.read_bytes()
                with request, pid, port, patch("terminal_client.shutil.which", return_value="fake-node"), \
                        patch("terminal_client.launch_background", side_effect=error) as launch:
                    result = self.client.recover_bound_owner(confirm=True)
                    self.assertEqual(result["state"], "unknown")
                    self.assertEqual(result["code"], "bound_owner_readiness_unknown")
                    self.assertFalse(result["recovered"])
                    self.assertFalse(result["started_empty"])
                    self.assertNotIn("private-launch", json.dumps(result))
                    self.assertNotIn("private-receipt.json", json.dumps(result))
                    self.assertTrue(self.maintenance.exists())
                    self.assertEqual(self.client.recover_bound_owner(confirm=True)["code"], "bound_owner_recovery_busy")
                launch.assert_called_once()
                self.assertEqual(self.descriptor.read_bytes(), descriptor_before)
                self.assertEqual(self.reference.read_bytes(), reference_before)
                # Only this unit fixture removes its own fence between cases.
                self.maintenance.unlink()

    def test_changed_reference_after_launch_is_unknown_and_never_a_recovery_receipt(self):
        process = Mock(pid=456)
        def publish(*args, **kwargs):
            self.write_descriptor(pid=456)
            self.reference.write_text(json.dumps({"descriptor_file": str(self.descriptor)}) + "\n")
            return process
        request, pid, port = self.offline()
        with request, pid, port, patch("terminal_client.shutil.which", return_value="fake-node"), \
                patch("terminal_client.launch_background", side_effect=publish):
            result = self.client.recover_bound_owner(confirm=True)
        self.assertEqual(result["code"], "bound_owner_changed_after_launch")
        self.assertFalse(result["recovered"])
        process.kill.assert_not_called()

    def test_cli_requires_confirmation_and_outputs_only_lifecycle(self):
        stub = Mock()
        stub.bound_owner_status.return_value = {"state": "offline", "code": "bound_owner_offline"}
        stub.recover_bound_owner.return_value = {"state": "blocked", "code": "confirmation_required"}
        with patch("terminal_service.TerminalClient", return_value=stub), patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(terminal_service.main(["status"]), 0)
            self.assertEqual(terminal_service.main(["recover"]), 1)
            terminal_service.main(["recover", "--confirm"])
        self.assertEqual(stub.recover_bound_owner.call_args_list[0].kwargs, {"confirm": False})
        self.assertEqual(stub.recover_bound_owner.call_args_list[1].kwargs, {"confirm": True})
        self.assertNotIn("token", output.getvalue())

    def test_public_generation_is_verified_against_authenticated_health(self):
        generation = "a" * 32
        self.write_descriptor(recovery_id=generation)
        with patch.object(self.client, "request", return_value={"ok": True, "pid": 123, "version": "0.3.0", "recovery_id": generation}):
            status = self.client.bound_owner_status()
        self.assertEqual(status["recovery_id"], generation)
        self.assertEqual(status["state"], "ready")
        self.assertFalse(status["recovered"])
        with patch.object(self.client, "request", return_value={"ok": True, "pid": 123, "version": "0.3.0", "recovery_id": "b" * 32}), \
                patch.object(self.client, "_pid_state", return_value="alive"):
            status = self.client.bound_owner_status()
        self.assertNotEqual(status["state"], "ready")

    def test_new_owner_with_sessions_is_not_a_fresh_empty_recovery_proof(self):
        process = Mock(pid=456)
        process.poll.return_value = None
        def publish(*args, **kwargs):
            guard = json.loads(kwargs["env_overrides"]["CEO_HUB_RECOVERY_GUARD"])
            self.write_descriptor(pid=456, recovery_id=guard["recovery_id"])
            return process
        def reply(path, *args, **kwargs):
            if self.client.descriptor()["pid"] == 123:
                return self.refused()
            if path == "/sessions":
                return {"sessions": [{"id": "existing-after-launch"}]}
            return {"ok": True, "pid": 456, "version": "0.3.0", "recovery_id": self.client.descriptor()["recovery_id"]}
        with patch.object(self.client, "request", side_effect=reply), patch.object(self.client, "_pid_state", return_value="dead"), \
                patch.object(self.client, "_port_state", return_value="refused"), patch("terminal_client.shutil.which", return_value="fake-node"), \
                patch("terminal_client.launch_background", side_effect=publish):
            result = self.client.recover_bound_owner(confirm=True)
        self.assertEqual(result["code"], "bound_owner_not_empty_after_launch")
        self.assertFalse(result["recovered"])
        self.assertFalse(result["started_empty"])
        process.kill.assert_not_called()

    def test_broker_lock_replacement_between_offline_checks_is_not_reclaimed(self):
        self.lockfile.write_text("123")
        calls = 0
        def probe(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.lockfile.rename(self.root / "old-lock")
                self.lockfile.write_text("123")
            return self.refused()
        with patch.object(self.client, "request", side_effect=probe), patch.object(self.client, "_pid_state", return_value="dead"), \
                patch.object(self.client, "_port_state", return_value="refused"), \
                patch("terminal_client.launch_background", side_effect=AssertionError("changed lease")):
            result = self.client.recover_bound_owner(confirm=True)
        self.assertEqual(result["code"], "bound_owner_changed")
        self.assertEqual(self.lockfile.read_text(), "123")

    def owner_identity(self, birth="win-filetime:133700000000000001"):
        return {"birth": birth, "executable": str(Path(shutil.which("node")).resolve()),
                "script": str(Path(__file__).resolve().parents[1] / "terminal_broker.cjs")}

    def test_new_birth_identity_distinguishes_live_original_owner_from_pid_reuse(self):
        identity = self.owner_identity()
        self.write_descriptor(owner_identity=identity)
        self.lockfile.write_text(json.dumps({"pid": 123, "owner_identity": identity}))
        for birth, expected in ((identity["birth"], "bound_owner_alive"),
                                ("win-filetime:133700000000000002", "bound_owner_offline")):
            with self.subTest(birth=birth), patch.object(self.client, "request", side_effect=self.refused), \
                    patch.object(self.client, "_pid_state", return_value="alive"), \
                    patch("terminal_client.observe_process", return_value={"state": "alive", "birth": birth, "executable": identity["executable"]}), \
                    patch.object(self.client, "_port_state", return_value="refused"), \
                    patch("terminal_client.launch_background", side_effect=AssertionError("status never replaces or kills a PID")):
                self.assertEqual(self.client.bound_owner_status()["code"], expected)

    def test_birth_identity_failure_and_same_birth_conflicting_image_fail_closed(self):
        identity = self.owner_identity()
        self.write_descriptor(owner_identity=identity)
        for observation in ({"state": "unknown"}, {"state": "alive", "birth": identity["birth"], "executable": os.path.abspath("other.exe")}):
            with self.subTest(observation=observation), patch.object(self.client, "request", side_effect=self.refused), \
                    patch.object(self.client, "_pid_state", return_value="alive"), \
                    patch("terminal_client.observe_process", return_value=observation):
                self.assertEqual(self.client.bound_owner_status()["code"], "bound_owner_pid_unknown")

    def test_legacy_reused_cmd_pid_can_be_proved_not_to_be_the_node_owner(self):
        self.lockfile.write_text("123")
        with patch.object(self.client, "request", side_effect=self.refused), \
                patch.object(self.client, "_pid_state", return_value="alive"), \
                patch("terminal_client.observe_process", return_value={"state": "alive", "birth": "win-filetime:133700000000000001", "executable": os.path.abspath("cmd.exe")}), \
                patch.object(self.client, "_port_state", return_value="refused"), \
                patch("terminal_client.launch_background", side_effect=AssertionError("status only")):
            self.assertEqual(self.client.bound_owner_status()["code"], "bound_owner_offline")
        self.assertEqual(self.lockfile.read_text(), "123")

    def test_legacy_live_node_without_birth_is_ambiguous_not_replaceable(self):
        identity = self.owner_identity()
        with patch.object(self.client, "request", side_effect=self.refused), \
                patch.object(self.client, "_pid_state", return_value="alive"), \
                patch("terminal_client.observe_process", return_value={"state": "alive", "birth": identity["birth"], "executable": identity["executable"]}):
            self.assertEqual(self.client.bound_owner_status()["code"], "bound_owner_pid_unknown")

    def test_new_lock_birth_must_match_original_descriptor_not_just_pid(self):
        identity = self.owner_identity()
        self.write_descriptor(owner_identity=identity)
        self.lockfile.write_text(json.dumps({"pid": 123, "owner_identity": self.owner_identity("win-filetime:133700000000000002")}))
        request, pid, port = self.offline()
        with request, pid, port:
            self.assertEqual(self.client.bound_owner_status()["code"], "bound_owner_lock_unknown")

    def test_birth_record_with_wrong_broker_script_is_not_an_owner_identity(self):
        identity = self.owner_identity()
        identity["script"] = str(self.root / "different.cjs")
        self.write_descriptor(owner_identity=identity)
        self.assertEqual(self.client.bound_owner_status()["code"], "bound_owner_reference_invalid")

    def test_native_observer_reads_own_birth_and_executable_without_side_effects(self):
        observed = observe_process(os.getpid())
        self.assertEqual(observed["state"], "alive")
        self.assertTrue(Path(observed["executable"]).is_absolute())
        self.assertTrue(observed["birth"].startswith("win-filetime:" if os.name == "nt" else "linux-start:"))


if __name__ == "__main__":
    unittest.main()
