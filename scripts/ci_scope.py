#!/usr/bin/env python3
"""Choose cheap documentation checks or the full offline test matrix.

Only an explicit set of static documentation/branding paths can skip the matrix.
Unknown paths, missing history, an empty diff, and manual dispatch fail closed to
full tests. This script uses local Git only; no API token or network is needed.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path, PurePosixPath
import re
import subprocess


ARTWORK_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg"})
ROOT_DOCUMENTATION = frozenset({
    "README.md", "README.zh-CN.md", "CONTRIBUTING.md", "SECURITY.md",
    "THIRD_PARTY_NOTICES.md", "LICENSE",
})
AGENT_INSTRUCTIONS = frozenset({"agents.md", "claude.md", "skill.md"})
SHA = re.compile(r"[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?\Z")


def documentation_only(path: str) -> bool:
    """Keep source, tests, tools, workflow files and unknown assets on full CI."""
    parsed = PurePosixPath(path)
    if not path or path.startswith("/") or "\\" in path or ".." in parsed.parts:
        return False
    if parsed.name.lower() in AGENT_INSTRUCTIONS:
        return False
    if len(parsed.parts) == 1:
        return path in ROOT_DOCUMENTATION
    if path == ".github/PULL_REQUEST_TEMPLATE.md":
        return True
    if parsed.parts[0] == "docs":
        return parsed.suffix == ".md" or (
            parsed.parts[:2] == ("docs", "images")
            and parsed.suffix in ARTWORK_SUFFIXES
        )
    if parsed.parts[:2] == ("assets", "brand"):
        return parsed.suffix == ".md" or parsed.suffix in ARTWORK_SUFFIXES
    return False


def select_scope(root: Path, event: str, base: str, head: str) -> tuple[bool, str]:
    if event not in {"push", "pull_request"}:
        return True, "manual_or_unknown_event"
    if not all(SHA.fullmatch(value) and set(value) != {"0"} for value in (base, head)):
        return True, "missing_or_invalid_base"
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "diff", "--name-only", "--no-renames", "-z", base, head, "--"],
            stdin=subprocess.DEVNULL, capture_output=True, check=True, timeout=30,
        )
        paths = [part.decode("utf-8") for part in result.stdout.split(b"\0") if part]
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return True, "diff_unavailable"
    if not paths:
        return True, "empty_diff"
    if all(documentation_only(path) for path in paths):
        return False, "documentation_and_branding_only"
    return True, "code_configuration_or_unknown_path"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", default=os.environ.get("CI_EVENT_NAME", ""))
    parser.add_argument("--base", default=os.environ.get("CI_BASE_SHA", ""))
    parser.add_argument("--head", default=os.environ.get("CI_HEAD_SHA", ""))
    arguments = parser.parse_args()
    full_tests, reason = select_scope(Path(__file__).resolve().parents[1], arguments.event, arguments.base, arguments.head)
    value = "true" if full_tests else "false"
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a", encoding="utf-8") as stream:
            stream.write(f"full_tests={value}\n")
    print(f"CI scope: full_tests={value} reason={reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
