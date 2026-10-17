"""Launch per-user services outside short-lived Windows tool Job Objects.

Windows uses the local WMI provider only as a hidden, current-user process
factory, never a scheduled task, another account, or an elevation boundary.
The external monitor and a same-route child probe verify that their current
jobs do not kill children on close. Actual target limits are not directly
observable across a process boundary; the nested-parent-exit regression test
validates that transport. Failure is explicit: there is no in-job fallback.
No models, remote connections, or automatic restarts are part of this module.
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

# Isolated Python excludes even the script directory from sys.path. Restore
# only this trusted source directory to load our shared native identity reader.
if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from agent_captain.infrastructure.process_identity import observe_process

MAX_PLAN = 65536
ENVIRONMENT_KEYS = frozenset({
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
    "USERPROFILE", "APPDATA", "LOCALAPPDATA", "HOMEDRIVE", "HOMEPATH", "HOME",
    "USERNAME", "USERDOMAIN", "SHELL", "LANG", "LC_ALL", "PYTHONIOENCODING",
    # Native Windows OpenSSH exits 255 before emitting diagnostics without
    # ProgramData (its system configuration base). Keep ordinary platform
    # directories, not application secrets, across the WMI boundary.
    "PROGRAMDATA", "ALLUSERSPROFILE", "SYSTEMDRIVE", "PROGRAMFILES", "PROGRAMFILES(X86)",
    "PROGRAMW6432", "COMMONPROGRAMFILES", "COMMONPROGRAMFILES(X86)", "COMMONPROGRAMW6432", "OS",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "SSL_CERT_FILE",
    "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "AGENTCAPTAIN_CONNECTION_FILE",
    "PRON_HUB_CONNECTION_FILE", "CEO_HUB_CONNECTION_FILE",
    "CEO_HUB_TERMINAL_FILE", "CEO_HUB_RECOVERY_GUARD", "CEO_HUB_SSH", "CEO_HUB_SHELL",
    "CEO_HUB_IDENTITY_PYTHON", "CEO_HUB_PYTHON",
})


class BackgroundLaunchUnknown(RuntimeError):
    """A Windows launch was attempted; do not assume that no child executed."""
    def __init__(self, message, *, launch_id=None, receipt_path=None):
        super().__init__(message)
        self.launch_id = launch_id
        self.receipt_path = receipt_path


def background_environment(environ=None):
    """Only service-relevant settings cross the external launch boundary.

    In particular, debugger injection, NODE_OPTIONS, API tokens, and the full
    transient tool environment are not serialized into a launch plan.
    """
    source = os.environ if environ is None else environ
    return {k: v for k, v in source.items() if k.upper() in ENVIRONMENT_KEYS}


def _kernel():
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    return kernel


def _creation_time(handle):
    from ctypes import wintypes
    times = [wintypes.FILETIME() for _ in range(4)]
    if not _kernel().GetProcessTimes(handle, *(ctypes.byref(x) for x in times)):
        raise ctypes.WinError(ctypes.get_last_error())
    return times[0].dwHighDateTime << 32 | times[0].dwLowDateTime


def process_job_state(pid=None):
    """Read own job limits, or determine whether a selected child has any job.

    Windows does not expose another process's unnamed job limits. Another
    process therefore returns flags=None rather than inventing target limits;
    startup separately records an exact-route self-inspecting probe.
    """
    if os.name != "nt":
        return {"in_job": False, "job_flags": 0}
    from ctypes import wintypes
    kernel = _kernel()
    handle = kernel.GetCurrentProcess() if pid is None else kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        inside = wintypes.BOOL()
        if not kernel.IsProcessInJob(handle, None, ctypes.byref(inside)):
            raise ctypes.WinError(ctypes.get_last_error())
        flags = None
        if pid is None:
            flags = 0
            if inside.value:
                class BasicLimits(ctypes.Structure):
                    _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                                ("flags", wintypes.DWORD), ("minimum", ctypes.c_size_t), ("maximum", ctypes.c_size_t),
                                ("active", wintypes.DWORD), ("affinity", ctypes.c_size_t),
                                ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]
                limits = BasicLimits()
                kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
                if not kernel.QueryInformationJobObject(None, 2, ctypes.byref(limits), ctypes.sizeof(limits), None):
                    raise ctypes.WinError(ctypes.get_last_error())
                flags = limits.flags
        return {"in_job": bool(inside.value), "job_flags": flags}
    finally:
        if pid is not None:
            kernel.CloseHandle(handle)


def _private_directory(parent):
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = parent / ("launch-" + uuid.uuid4().hex)
    if os.name != "nt":
        directory.mkdir(mode=0o700)
        return directory
    from ctypes import wintypes
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = _kernel()
    token = wintypes.HANDLE()
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x8, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        length = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(length))
        value = ctypes.create_string_buffer(length.value)
        if not advapi.GetTokenInformation(token, 1, value, length, ctypes.byref(length)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.cast(value, ctypes.POINTER(ctypes.c_void_p))[0]
        sid_text = wintypes.LPWSTR()
        advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(sid_text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            # Protected DACL: no inherited access for other local users.
            sddl = "D:P(A;OICI;FA;;;" + sid_text.value + ")"
        finally:
            kernel.LocalFree.argtypes = [ctypes.c_void_p]
            kernel.LocalFree(sid_text)
        security = ctypes.c_void_p()
        advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(security), None):
            raise ctypes.WinError(ctypes.get_last_error())
        class SecurityAttributes(ctypes.Structure):
            _fields_ = [("length", wintypes.DWORD), ("descriptor", ctypes.c_void_p), ("inherit", wintypes.BOOL)]
        attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), security, False)
        try:
            kernel.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(SecurityAttributes)]
            if not kernel.CreateDirectoryW(str(directory), ctypes.byref(attributes)):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel.LocalFree(security)
    finally:
        kernel.CloseHandle(token)
    return directory


def _write_json(path, value):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
        temporary.chmod(0o600)
        # Atomic publication that refuses to replace an existing receipt/plan.
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path):
    with path.open("rb") as stream:
        raw = stream.read(MAX_PLAN + 1)
    if len(raw) > MAX_PLAN:
        raise ValueError("Background launch record is too large")
    return json.loads(raw)


def _event(path, launch_id, event, **fields):
    # Never log argv, environment contents, descriptors, or authentication.
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"at": time.time(), "launch_id": launch_id, "event": event, **fields}, separators=(",", ":")) + "\n")


def _absolute(path, label):
    result = Path(path)
    if not result.is_absolute() or str(result).startswith(("\\\\", "//")) or "\x00" in str(result):
        raise ValueError(label + " must be an absolute local path")
    return result.resolve()


def _validated(argv, cwd, stdout_path, stderr_path, env_overrides):
    if not isinstance(argv, (list, tuple)) or not argv or len(argv) > 128 or any(not isinstance(x, str) or "\x00" in x or len(x) > 16384 for x in argv):
        raise ValueError("Invalid background argv")
    executable = _absolute(argv[0], "Executable")
    cwd = _absolute(cwd, "Working directory")
    stdout_path, stderr_path = _absolute(stdout_path, "Output log"), _absolute(stderr_path, "Error log")
    if not executable.is_file() or not cwd.is_dir():
        raise ValueError("Background executable or working directory is missing")
    environment = background_environment()
    for key, value in (env_overrides or {}).items():
        if not isinstance(key, str) or key.upper() not in ENVIRONMENT_KEYS or not isinstance(value, str) or "\x00" in value or len(value) > 32768:
            raise ValueError("Unsupported background environment override")
        # Windows environment names are case-insensitive, unlike Python dicts.
        for old in list(environment):
            if old.upper() == key.upper():
                del environment[old]
        environment[key] = value
    for key, value in environment.items():
        if key.upper() in {"AGENTCAPTAIN_CONNECTION_FILE", "PRON_HUB_CONNECTION_FILE", "CEO_HUB_CONNECTION_FILE", "CEO_HUB_TERMINAL_FILE", "CEO_HUB_SSH", "CEO_HUB_SHELL", "CEO_HUB_IDENTITY_PYTHON", "CEO_HUB_PYTHON"} and value:
            _absolute(value, key)
    return [str(executable), *argv[1:]], cwd, stdout_path, stderr_path, environment


def _wmi_create(argv, cwd):
    # list2cmdline quotes only the Windows program invocation. The PowerShell
    # program is fixed and interpolates only safely escaped string literals.
    quote = lambda value: "'" + str(value).replace("'", "''") + "'"
    script = ("$ErrorActionPreference='Stop';"
              "$startup=New-CimInstance -Namespace root/cimv2 -ClassName Win32_ProcessStartup -ClientOnly -Property @{ShowWindow=[uint16]0};"
              "$result=Invoke-CimMethod -Namespace root/cimv2 -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine="
              + quote(subprocess.list2cmdline(argv)) + ";CurrentDirectory=" + quote(cwd)
              + ";ProcessStartupInformation=$startup};"
              "@{return_value=[int]$result.ReturnValue;pid=[int]$result.ProcessId}|ConvertTo-Json -Compress")
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    buffer = ctypes.create_unicode_buffer(32768)
    kernel = _kernel()
    kernel.GetWindowsDirectoryW(buffer, len(buffer))
    powershell = Path(buffer.value) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    result = subprocess.run([str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        # PowerShell stderr can reproduce commands; do not include it in errors.
        raise RuntimeError("Windows WMI background launch failed; no in-job fallback was attempted")
    try:
        value = json.loads(result.stdout.strip())
        if value["return_value"] != 0 or type(value["pid"]) is not int or value["pid"] <= 0:
            raise ValueError()
        return value["pid"]
    except (ValueError, KeyError, TypeError):
        raise RuntimeError("Windows WMI did not confirm a background launch; inspect private launch records before retrying") from None


class BackgroundProcess:
    """Popen-compatible pid/poll/wait backed by one retained process handle."""
    def __init__(self, receipt, receipt_path):
        self.pid = receipt["pid"]
        self.creation_time = receipt["creation_time"]
        self.launch_id = receipt["launch_id"]
        self.receipt_path = receipt_path
        self._handle = _kernel().OpenProcess(0x100000 | 0x1000, False, self.pid)
        if not self._handle or _creation_time(self._handle) != self.creation_time:
            self.close()
            raise RuntimeError("Background child identity changed before verification")

    def poll(self):
        from ctypes import wintypes
        code = wintypes.DWORD()
        if not self._handle or not _kernel().GetExitCodeProcess(self._handle, ctypes.byref(code)):
            raise RuntimeError("Background process identity is unavailable")
        return None if code.value == 259 else code.value

    def wait(self, timeout=None):
        milliseconds = 0xFFFFFFFF if timeout is None else max(0, min(int(timeout * 1000), 0xFFFFFFFE))
        result = _kernel().WaitForSingleObject(self._handle, milliseconds)
        if result == 258:
            raise subprocess.TimeoutExpired("background child", timeout)
        if result != 0:
            raise RuntimeError("Background wait failed")
        return self.poll()

    def close(self):
        if getattr(self, "_handle", None):
            _kernel().CloseHandle(self._handle)
            self._handle = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def launch_background(argv, *, cwd, stdout_path, stderr_path, env_overrides=None, startup_timeout=12):
    """Start once; return the actual child, never WMI's intermediate PID.

    A timeout is an unknown launch outcome, not permission to retry. The private
    plan and receipt provide reconciliation evidence. There is no auto restart.
    """
    argv, cwd, stdout_path, stderr_path, environment = _validated(argv, cwd, stdout_path, stderr_path, env_overrides)
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        with stdout_path.open("ab") as output, stderr_path.open("ab") as error:
            return subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=output, stderr=error,
                                    env=environment, start_new_session=True)
    directory = _private_directory(cwd / ".runtime" / "background-launches")
    launch_id = directory.name.removeprefix("launch-")
    plan_path, receipt_path = directory / "plan.json", directory / "receipt.json"
    plan = {"schema": 1, "launch_id": launch_id, "argv": argv, "cwd": str(cwd),
            "stdout": str(stdout_path), "stderr": str(stderr_path), "environment": environment,
            "expires_at": time.time() + 30}
    payload = json.dumps(plan, ensure_ascii=False).encode("utf-8")
    if len(payload) > MAX_PLAN:
        raise ValueError("Background launch plan is too large")
    _write_json(plan_path, plan)
    wrapper = [sys.executable, "-I", "-u", str(Path(__file__).resolve()), "--launch-plan", str(plan_path)]
    try:
        wrapper_pid = _wmi_create(wrapper, cwd)
        deadline = time.monotonic() + startup_timeout
        while time.monotonic() < deadline:
            if receipt_path.exists():
                receipt = _read_json(receipt_path)
                if receipt.get("launch_id") != launch_id or receipt.get("schema") != 1:
                    raise RuntimeError("receipt_identity_mismatch")
                if receipt.get("state") != "started":
                    raise RuntimeError("launch_unconfirmed")
                if receipt.get("wrapper_pid") != wrapper_pid:
                    raise RuntimeError("monitor_identity_mismatch")
                route_probe = receipt.get("route_probe", {})
                if (type(route_probe.get("job_flags")) is not int or route_probe["job_flags"] & 0x2000
                        or type(receipt.get("wrapper_job", {}).get("job_flags")) is not int
                        or receipt["wrapper_job"]["job_flags"] & 0x2000):
                    raise RuntimeError("job_escape_unverified")
                return BackgroundProcess(receipt, receipt_path)
            time.sleep(.05)
        raise RuntimeError("receipt_timeout")
    except Exception:
        # All exceptions after WMI dispatch are conservatively unknown. A
        # malformed receipt or failed identity query does not undo execution.
        try:
            plan_path.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            _event(directory / "lifecycle.jsonl", launch_id, "wmi_launch_unconfirmed")
        except OSError:
            pass
        raise BackgroundLaunchUnknown("Background launch outcome unknown; inspect " + str(receipt_path) + " before retrying",
                                      launch_id=launch_id, receipt_path=receipt_path) from None


def _run_plan(plan_path):
    plan_path = _absolute(plan_path, "Launch plan")
    if plan_path.name != "plan.json" or plan_path.parent.name[:7] != "launch-":
        raise ValueError("Invalid launch plan path")
    launch_id = plan_path.parent.name[7:]
    receipt_path, events = plan_path.parent / "receipt.json", plan_path.parent / "lifecycle.jsonl"
    # Only the one-shot claim winner may spawn, publish, or remove this plan.
    # A duplicate wrapper must not race a failed receipt against a live child.
    try:
        with (plan_path.parent / "claimed").open("x", encoding="ascii") as claim:
            claim.write(str(os.getpid()))
    except FileExistsError:
        return 1
    child = None
    try:
        plan = _read_json(plan_path)
        if plan.get("schema") != 1 or plan.get("launch_id") != launch_id or not 0 <= plan["expires_at"] - time.time() <= 30:
            raise ValueError("expired_or_invalid_plan")
        wrapper_job = process_job_state()
        if wrapper_job["job_flags"] & 0x2000:
            raise RuntimeError("external_wrapper_still_in_kill_job")
        argv, cwd, output_path, error_path, environment = _validated(plan["argv"], plan["cwd"], plan["stdout"], plan["stderr"], plan["environment"])
        # WMI's environment is not the caller's. Use exactly the filtered plan,
        # never retain unexpected provider/tool injection into the real child.
        environment = plan["environment"]
        plan_path.unlink()  # one-shot secrets/settings do not remain at rest
        _event(events, launch_id, "monitor_started", pid=os.getpid(), job=wrapper_job)
        # Some Windows management environments place every new process in a
        # non-killing (0x1800) job. IsProcessInJob alone cannot distinguish it
        # from the transient tool's killing job. Read actual limits from a
        # short-lived child of this exact external launch route before work.
        probe_path = plan_path.parent / "route-probe.json"
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_BREAKAWAY_FROM_JOB
        probe = subprocess.Popen([sys.executable, "-I", str(Path(__file__).resolve()), "--job-probe", str(probe_path)],
                                 cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
        try:
            if probe.wait(timeout=5) != 0:
                raise RuntimeError("route_probe_failed")
        except subprocess.TimeoutExpired:
            probe.terminate()
            probe.wait(timeout=5)
            raise RuntimeError("route_probe_timeout") from None
        route_probe = _read_json(probe_path)
        if type(route_probe.get("job_flags")) is not int or route_probe["job_flags"] & 0x2000:
            raise RuntimeError("route_probe_in_kill_job")
        _event(events, launch_id, "route_verified", probe=route_probe)
        with output_path.open("ab") as output, error_path.open("ab") as error:
            child = subprocess.Popen(argv, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                                     stdout=output, stderr=error, creationflags=flags)
        child_job = process_job_state(child.pid)
        creation_time = _creation_time(child._handle)
        identity = observe_process(child.pid)
        if (identity.get("state") != "alive" or identity.get("birth") != "win-filetime:" + str(creation_time)
                or os.path.normcase(os.path.realpath(identity.get("executable", ""))) != os.path.normcase(os.path.realpath(argv[0]))):
            # The process may legitimately have finished immediately; do not
            # misidentify a reused PID or publish an unverified live service.
            raise RuntimeError("child_identity_unverified")
        receipt = {"schema": 1, "launch_id": launch_id, "state": "started", "pid": child.pid,
                   "creation_time": creation_time, "birth": identity["birth"],
                   "executable": identity["executable"], "wrapper_pid": os.getpid(), "wrapper_job": wrapper_job,
                   "child_job": child_job, "route_probe": route_probe,
                   "target_job_limits": "not_directly_observable_same_route_probe_used", "started_at": time.time()}
        _write_json(receipt_path, receipt)
        _event(events, launch_id, "child_started", pid=child.pid, creation_time=receipt["creation_time"], job=child_job)
        code = child.wait()
        _event(events, launch_id, "child_exited", pid=child.pid, creation_time=receipt["creation_time"], exit_code=code)
    except Exception as error:
        # Only error class/code, never exception text that may contain settings.
        code = str(error) if str(error) in {"expired_or_invalid_plan", "external_wrapper_still_in_kill_job", "child_still_in_job"} else type(error).__name__
        _event(events, launch_id, "launch_failed", error=code)
        if not receipt_path.exists():
            failure = {"schema": 1, "launch_id": launch_id, "state": "unknown" if child is not None else "failed", "error": code}
            if child is not None:
                failure["pid"] = child.pid
                failure["creation_time"] = _creation_time(child._handle)
                failure["birth"] = "win-filetime:" + str(failure["creation_time"])
            _write_json(receipt_path, failure)
        if child is not None:
            # Startup evidence can fail after execution. Keep observing this
            # exact retained child instead of killing it or claiming not-run.
            _event(events, launch_id, "child_launch_unconfirmed", pid=child.pid)
            _event(events, launch_id, "child_exited", pid=child.pid, exit_code=child.wait())
        return 1
    finally:
        if plan_path.exists():
            plan_path.unlink()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Private one-shot background process monitor")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--launch-plan")
    target.add_argument("--job-probe")
    args = parser.parse_args()
    if args.job_probe:
        _write_json(_absolute(args.job_probe, "Job probe output"), process_job_state())
    else:
        raise SystemExit(_run_plan(Path(args.launch_plan)))
