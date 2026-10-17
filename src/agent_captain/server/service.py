"""Per-user background launcher. Never starts models or connects machines."""
import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

from agent_captain.server.app import connection_file
from agent_captain.core.hub import ROOT, VERSION
from agent_captain.infrastructure.background_process import launch_background


def status():
    try:
        descriptor = json.loads(connection_file().read_text(encoding="utf-8"))
        from agent_captain.integrations.mcp_bridge import read_connection, NoRedirect
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
    process = launch_background([sys.executable, "-u", str(ROOT / "scripts" / "captain.py"), "serve", "--port", str(port)],
                                cwd=ROOT, stdout_path=runtime / "server.out.log", stderr_path=runtime / "server.err.log")
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("服务启动失败，请查看 .runtime/server.err.log。")
        current = status()
        if current["running"] and current["version"] == VERSION and current["pid"] == process.pid:
            return current
        time.sleep(.2)
    raise RuntimeError("服务未在限时内就绪，请查看 .runtime/server.err.log。")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("start", "status"))
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(start(args.port) if args.action == "start" else status(), ensure_ascii=False))


if __name__ == "__main__":
    main()
