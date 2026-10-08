"""Per-user background launcher. Never starts models or connects machines."""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from app import connection_file
from hub import ROOT, VERSION


def status():
    try:
        descriptor = json.loads(connection_file().read_text(encoding="utf-8"))
        from mcp_bridge import read_connection, NoRedirect
        url, _ = read_connection()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(url + "/health", timeout=2) as response:
            health = json.load(response)
        if health.get("service") != "ceo_hub":
            return {"running": False}
        if health.get("pid") is not None and health["pid"] != descriptor.get("pid"):
            return {"running": False}
        return {"running": True, "url": url, "pid": descriptor.get("pid"), "version": health.get("version")}
    except Exception:
        return {"running": False}


def start(port=8766):
    current = status()
    if current["running"]:
        if current["version"] != VERSION:
            raise RuntimeError("旧版服务仍在运行，请先完成工作并关闭旧版，再启动新版。")
        return current
    runtime = ROOT / ".runtime"
    runtime.mkdir(exist_ok=True)
    with (runtime / "server.out.log").open("ab") as output, (runtime / "server.err.log").open("ab") as error:
        process = subprocess.Popen([sys.executable, "-u", str(ROOT / "app.py"), "serve", "--port", str(port)],
                                   cwd=ROOT, stdin=subprocess.DEVNULL, stdout=output, stderr=error,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), start_new_session=os.name != "nt")
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("服务启动失败，请查看 .runtime/server.err.log。")
        current = status()
        if current["running"] and current["version"] == VERSION:
            return current
        time.sleep(.2)
    raise RuntimeError("服务未在限时内就绪，请查看 .runtime/server.err.log。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("start", "status"))
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(start(args.port) if args.action == "start" else status(), ensure_ascii=False))
