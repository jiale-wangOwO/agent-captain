"""Read one selected worker's output between local process/session checks.

This stdlib-only orchestration is portable over one SSH request. It never
attaches a terminal, writes input or reads model credentials.
"""
from __future__ import annotations

import copy

if __package__:
    from . import conversation_output, session_binding
else:
    import conversation_output
    import session_binding


_REASONS = set((
    "session_identity_unavailable no_exact_session_evidence ambiguous_session_evidence "
    "foreground_shell foreground_identity_unresolved foreground_codex_ambiguous foreground_not_codex "
    "invalid_terminal_id terminal_pane_unresolved process_changed session_evidence_changed "
    "process_tree_limit process_tree_unavailable process_metadata_limit process_metadata_unavailable "
    "process_handle_limit process_handles_unavailable process_environment_limit process_environment_unavailable "
    "codex_home_unresolved rollout_outside_codex_home invalid_rollout_file rollout_replaced invalid_session_header "
    "session_id_mismatch unsupported_session_source invalid_session_id invalid_session_cwd session_cwd_mismatch "
    "session_index_ambiguous session_index_unavailable session_not_indexed windows_unavailable invalid_root_pid "
    "windows_process_metadata_unavailable selected_codex_ambiguous selected_not_codex "
    "selected_process_owner_mismatch selected_process_owner_unavailable selected_process_unavailable"
).split())


def _failure(code):
    return {"state": "unavailable", "reason_code": code}


def main(request):
    """Preserve binding selection and pagination without extra SSH handshakes."""
    try:
        if (not isinstance(request, dict) or request.get("op") != "read_bound_output"
                or not isinstance(request.get("terminal_id"), str)
                or not session_binding.SESSION.fullmatch(request["terminal_id"])
                or type(request.get("historical_allowed")) is not bool
                or request.get("saved_binding") is not None and not isinstance(request["saved_binding"], dict)
                or type(request.get("limit")) is not int or not 1 <= request["limit"] <= 100
                or type(request.get("include_tools", False)) is not bool
                or request.get("cursor") and request.get("after_cursor")):
            return _failure("remote_read_failed")
        terminal_id = request["terminal_id"]
        probe_request = {"op": "bind_output", "terminal_id": terminal_id}
        probe = session_binding.main(probe_request)
        if not isinstance(probe, dict):
            return _failure("remote_read_failed")
        saved = request.get("saved_binding")
        if probe.get("state") == "bound" and probe.get("binding_evidence") == "held_rollout":
            binding = {**probe, "historical": False, "live_session_verified": True}
        elif (saved and (probe.get("reason") == "no_exact_session_evidence" or probe.get("binding_evidence") == "explicit_resume")
              and probe.get("process_identity") and probe["process_identity"] == saved.get("process_identity")):
            binding = {**saved, "historical": False, "live_session_verified": False}
        elif probe.get("state") == "bound":
            binding = {**probe, "historical": False, "live_session_verified": False}
        elif saved and (request["historical_allowed"] or probe.get("reason") == "foreground_shell"):
            binding = {**saved, "historical": True, "live_session_verified": False}
        else:
            reason = probe.get("reason")
            return {"state": "unbound", "reason": reason if reason in _REASONS else "session_identity_unavailable"}
        # Freeze the first identity; shared or caller-owned dictionaries must
        # not turn a later process observation into the earlier proof.
        binding = copy.deepcopy(binding)
        # Human-selected bindings persist resolve_session's metadata state.
        # Once selected by the rules above, this response describes a bound
        # output choice even when its evidence remains explicitly non-live.
        binding["state"] = "bound"
        binding["terminal_id"] = terminal_id
        if not binding.get("historical") and not binding.get("process_identity"):
            return _failure("output_binding_invalid")
        output_request = {"op": "read_output", "session_id": binding["session_id"],
                          "rollout_path": binding["rollout_path"], "codex_home": binding["codex_home"],
                          "limit": request["limit"], "include_tools": request.get("include_tools", False)}
        output_request.update({key: request[key] for key in ("cursor", "after_cursor") if request.get(key)})
        output = conversation_output.main(output_request)
        if (not isinstance(output, dict) or output.get("id") != binding["session_id"]
                or output.get("source") != "codex_rollout"):
            return _failure("output_binding_invalid")
        if not binding.get("historical"):
            verify = session_binding.main(probe_request)
            if not isinstance(verify, dict):
                return _failure("remote_read_failed")
            if verify.get("process_identity") != binding.get("process_identity"):
                return _failure("session_identity_changed")
            if verify.get("state") == "bound" and verify.get("binding_evidence") == "held_rollout":
                if verify.get("session_id") != binding["session_id"]:
                    return _failure("session_identity_changed")
                binding["live_session_verified"] = True
            elif verify.get("state") == "unbound" and verify.get("reason") != "no_exact_session_evidence":
                return _failure("output_binding_invalid")
            else:
                binding["live_session_verified"] = False
        return {"state": "bound", "binding": binding, "output": output}
    except Exception:
        # Provider/startup, process and file diagnostics can contain private
        # text. An incomplete read never exports its output or raw exception.
        return _failure("remote_read_failed")
