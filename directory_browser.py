"""Bounded, on-demand directory navigation for the selected local/SSH host."""
from __future__ import annotations

import base64
import json
import os
import string
import subprocess
import sys
import threading
from pathlib import Path

MAX_OUTPUT = 256 * 1024
MAX_DIRECTORIES = 300
MAX_ENTRIES = 5000


def validate_path(value):
    if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value):
        raise ValueError("目录路径最长 4096 字符，不能含控制字符。")


def list_local(path="", home_only=False):
    validate_path(path)
    home = Path.home().resolve()
    if home_only:
        return {"home": str(home)}
    selected = Path(path).expanduser() if path else home
    if not selected.is_absolute():
        raise ValueError("请选择该机器上的绝对目录。")
    try:
        selected = selected.resolve(strict=True)
        if not selected.is_dir():
            raise ValueError("选择的路径不是目录。")
        directories, truncated, output_bytes = [], False, 0
        with os.scandir(selected) as entries:
            for index, entry in enumerate(entries):
                if index >= MAX_ENTRIES:
                    truncated = True
                    break
                try:
                    is_directory = entry.is_dir()
                except OSError:
                    continue
                if not is_directory:
                    continue
                if len(directories) >= MAX_DIRECTORIES:
                    truncated = True
                    break
                # The name/path are filesystem data only; never shell text.
                if any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in entry.name):
                    continue
                item = {"name": entry.name, "path": str(selected / entry.name)}
                item_bytes = len(json.dumps(item, ensure_ascii=False).encode("utf-8"))
                if output_bytes + item_bytes > MAX_OUTPUT // 2:
                    truncated = True
                    break
                directories.append(item)
                output_bytes += item_bytes
    except FileNotFoundError:
        raise ValueError("目录不存在，请选择其他路径。") from None
    except PermissionError:
        raise ValueError("没有权限读取此目录，请选择其他路径。") from None
    except OSError:
        raise ValueError("无法读取此目录，请检查路径和机器连接。") from None
    if os.name == "nt":
        drives = os.listdrives() if hasattr(os, "listdrives") else [f"{letter}:\\" for letter in string.ascii_uppercase if os.path.isdir(f"{letter}:\\")]
        roots = [{"name": drive.rstrip("\\/"), "path": drive} for drive in drives]
    else:
        roots = [{"name": "/", "path": "/"}]
    roots.insert(0, {"name": "主目录", "path": str(home)})
    directories.sort(key=lambda item: item["name"].casefold())
    return {"path": str(selected), "home": str(home), "parent": str(selected.parent) if selected.parent != selected else None,
            "roots": roots, "directories": directories, "truncated": truncated}


def _bounded_ssh(target, request):
    if not isinstance(target, str) or not target or target.startswith("-") or any(c.isspace() or ord(c) < 32 for c in target):
        raise ValueError("无效的 SSH 机器地址。")
    payload = base64.urlsafe_b64encode(json.dumps(request, ensure_ascii=False).encode("utf-8")).decode("ascii")
    process = subprocess.Popen(["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", target, "python3 - " + payload],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    exceeded = threading.Event()

    def collect(name, stream, limit):
        while True:
            chunk = stream.read(8192)
            if not chunk:
                return
            if len(buffers[name]) + len(chunk) > limit:
                exceeded.set()
                process.kill()
                return
            buffers[name].extend(chunk)

    threads = [threading.Thread(target=collect, args=(name, stream, limit), daemon=True)
               for name, stream, limit in (("stdout", process.stdout, MAX_OUTPUT), ("stderr", process.stderr, 8192))]
    for thread in threads:
        thread.start()
    try:
        process.stdin.write(Path(__file__).read_bytes())
        process.stdin.close()
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
        raise RuntimeError("远端目录读取超时；请检查 SSH 连接后重试。") from None
    finally:
        for thread in threads:
            thread.join(timeout=1)
        for stream in (process.stdout, process.stderr):
            stream.close()
    if exceeded.is_set():
        raise RuntimeError("远端目录输出超过限制；请选择范围更小的目录。")
    if process.returncode:
        # Startup stderr can include provider/login output; never expose it.
        raise RuntimeError("无法通过 SSH 读取目录；请确认机器已连接且远端可运行 python3。")
    marker = b"CEO_HUB_DIRECTORY_RESULT="
    line = next((line[len(marker):] for line in reversed(bytes(buffers["stdout"]).splitlines()) if line.startswith(marker)), None)
    if line is None:
        raise RuntimeError("远端未返回有效的目录结果。")
    try:
        reply = json.loads(line)
    except (ValueError, TypeError):
        raise RuntimeError("远端目录结果格式无效。") from None
    if not reply.get("ok"):
        raise ValueError(reply.get("error", "无法读取远端目录。"))
    return reply["result"]


class DirectoryBrowser:
    def list(self, config, path=""):
        validate_path(path)
        if config["transport"] == "local":
            return list_local(path)
        return _bounded_ssh(config["target"], {"path": path})

    def home(self, config):
        if config["transport"] == "local":
            return str(Path.home().resolve())
        return _bounded_ssh(config["target"], {"op": "home"})["home"]


if __name__ == "__main__":
    try:
        request = json.loads(base64.urlsafe_b64decode(sys.argv[1]))
        result = {"ok": True, "result": list_local(request.get("path", ""), request.get("op") == "home")}
    except Exception as error:
        result = {"ok": False, "error": str(error)}
    print("CEO_HUB_DIRECTORY_RESULT=" + json.dumps(result, ensure_ascii=False), flush=True)
