"""Explicit maintenance for the pinned worker display service, never agents.

Use ``python scripts/captain.py terminal status`` for a read-only check, or
``python scripts/captain.py terminal recover --confirm`` for a safe offline-owner
recovery. This command does not connect machines or initialize work sessions.
"""
import argparse
import json
import sys

from agent_captain.transport.terminal_client import TerminalClient


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "recover"))
    parser.add_argument("--confirm", action="store_true", help="Confirm display-service recovery, not task replay")
    args = parser.parse_args(argv)
    client = TerminalClient()
    result = client.bound_owner_status() if args.action == "status" else client.recover_bound_owner(confirm=args.confirm)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["state"] in ("ready", "offline") else 1


if __name__ == "__main__":
    sys.exit(main())
