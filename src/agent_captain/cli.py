"""Single command surface; importing it never starts services or workers."""
import argparse
import sys


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog="agent-captain",
        description="AgentCaptain · 队长 — 一个主控，统筹本机与远程 Agent。")
    parser.add_argument("command", choices=("service", "terminal", "mcp", "serve", "find", "read", "probe"),
                        help="service: web lifecycle; terminal: display maintenance; mcp: stdio; serve/find/read/probe: HTTP/catalog")
    if not args or args[0] in ("--help", "-h"):
        parser.print_help()
        return 0
    command = parser.parse_args(args[:1]).command
    from agent_captain.paths import require_checkout
    require_checkout()
    if command == "service":
        from agent_captain.server.service import main as run
        return run(args[1:])
    if command == "terminal":
        from agent_captain.transport.terminal_service import main as run
        return run(args[1:])
    if command == "mcp":
        if len(args) != 1:
            parser.error("mcp does not accept arguments")
        from agent_captain.integrations.mcp_bridge import main as run
        return run()
    from agent_captain.server.app import main as run
    return run(args)
