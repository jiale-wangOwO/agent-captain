"""Read-only OS process birth identity shared by Python and Node owners.

PID existence alone cannot identify a broker after PID reuse. No function in
this module terminates a process, changes a lock, or inspects credentials.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys


def observe_process(pid):
    if type(pid) is not int or not 0 < pid <= 2147483647:
        return {"state": "unknown"}
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.GetProcessTimes.restype = wintypes.BOOL
        kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return {"state": "dead" if ctypes.get_last_error() == 87 else "unknown"}
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                return {"state": "unknown"}
            if code.value != 259:
                return {"state": "dead"}
            created, exited, system, user = (wintypes.FILETIME() for _ in range(4))
            size = wintypes.DWORD(32768)
            name = ctypes.create_unicode_buffer(size.value)
            if not kernel.GetProcessTimes(handle, *[ctypes.byref(part) for part in (created, exited, system, user)]):
                return {"state": "unknown"}
            if not kernel.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)):
                return {"state": "unknown"}
            birth = (created.dwHighDateTime << 32) | created.dwLowDateTime
            # Both values come from the same handle, not two PID lookups.
            return {"state": "alive", "birth": "win-filetime:" + str(birth), "executable": name.value}
        finally:
            kernel.CloseHandle(handle)
    if sys.platform.startswith("linux"):
        try:
            root = Path("/proc") / str(pid)
            before = (root / "stat").read_text()
            birth = before[before.rfind(")") + 2:].split()[19]
            executable = os.readlink(root / "exe")
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            after = (root / "stat").read_text()
            if birth != after[after.rfind(")") + 2:].split()[19]:
                return {"state": "unknown"}
            return {"state": "alive", "birth": f"linux-start:{boot}:{birth}", "executable": executable}
        except FileNotFoundError:
            return {"state": "dead"} if not (Path("/proc") / str(pid)).exists() else {"state": "unknown"}
        except (OSError, ValueError, IndexError):
            return {"state": "unknown"}
    return {"state": "unknown"}


def valid_owner_identity(value, script):
    return (isinstance(value, dict) and set(value) == {"birth", "executable", "script"}
            and isinstance(value["birth"], str)
            and re.fullmatch(r"(?:win-filetime:[0-9]{1,24}|linux-start:[a-f0-9-]{36}:[0-9]{1,24})", value["birth"]) is not None
            and all(isinstance(value[key], str) and 0 < len(value[key]) <= 4096
                    and not any(ord(char) < 32 for char in value[key]) for key in ("executable", "script"))
            and Path(value["executable"]).is_absolute() and Path(value["script"]).is_absolute()
            and os.path.normcase(os.path.normpath(value["script"])) == os.path.normcase(os.path.abspath(script))
            and Path(value["executable"]).name.lower() in ("node", "node.exe"))


def broker_owner_state(pid, identity, script, observation=None):
    if identity is not None and not valid_owner_identity(identity, script):
        return "unknown"
    current = observe_process(pid) if observation is None else observation
    if current.get("state") != "alive":
        return "dead" if current.get("state") == "dead" else "unknown"
    executable, birth = current.get("executable"), current.get("birth")
    if not isinstance(executable, str) or not isinstance(birth, str):
        return "unknown"
    if identity is not None:
        if identity["birth"] != birth:
            return "dead"  # This *owner* is gone; the reused PID is never killed.
        if os.path.normcase(identity["executable"]) != os.path.normcase(executable):
            return "unknown"  # Contradictory evidence for the same birth.
        return "alive"
    # Old descriptors have no birth record. A non-Node executable definitively
    # cannot be the old Node broker. Any Node (including an unrelated one) is
    # ambiguous: do not infer death from its age, command line, or a timeout.
    return "dead" if Path(executable).name.lower() not in ("node", "node.exe") else "unknown"


if __name__ == "__main__":
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "observe":
            result = observe_process(int(sys.argv[2]))
        elif len(sys.argv) == 5 and sys.argv[1] == "owner":
            result = {"state": broker_owner_state(int(sys.argv[2]), json.loads(sys.argv[3]), sys.argv[4])}
        else:
            result = {"state": "unknown"}
    except (ValueError, TypeError, OSError):
        result = {"state": "unknown"}
    print(json.dumps(result, separators=(",", ":")))
