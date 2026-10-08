#!/usr/bin/env python3
"""Bounded publication hygiene check; never prints matched source or secret values.

Default mode examines candidate files, without opening excluded local state.
--tracked examines staged Git blobs, not potentially different working-tree files.
Binary artwork is allowed but not asserted to be metadata-free or secret-free.
Generic absolute fixture paths under tests/ are exempt from the personal-path
heuristic only. Keys, private hosts, session identifiers, and private artifacts
are still checked there. Test paths require human review before publication.
The only .agents/ source exception is .agents/plugins/marketplace.json; no other
agent state is opened, and this catalog is text-scanned like ordinary source.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from typing import Iterable


PRIVATE_DIRS = frozenset({
    ".git", ".runtime", ".schemas", ".agents", ".codex", ".vscode",
    "node_modules", "__pycache__", ".pytest_cache", ".venv", "venv",
    "build", "dist", "htmlcov", ".idea",
})
PRIVATE_NAMES = frozenset({
    "config.local.json", "connection.json", "terminal-connection.json",
    "auth.json", "id_rsa", "id_ed25519", "id_ecdsa", ".coverage",
})
PUBLIC_SOURCE_METADATA = ".agents/plugins/marketplace.json"
PUBLIC_SOURCE_PARENTS = frozenset({".agents", ".agents/plugins"})
BINARY_ARTWORK = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf"})
MAX_TEXT_BYTES = 2 * 1024 * 1024
SYNTHETIC_HOME_NAMES = r"Public|Default|example(?:[-_][A-Za-z0-9]+)?|test(?:er|[-_][A-Za-z0-9]+)?|fake(?:[-_][A-Za-z0-9]+)?|real[-_]user|remote(?:[-_]user)?|target(?:[-_]user)?|user|[A-Za-z]{1,2}"
SYNTHETIC_WINDOWS_ROOTS = r"Windows|Users|tmp|temp|example|test|fake(?:[-_][A-Za-z0-9]+)?|tools|projects|project|workspace|old|new|outside|external|other|opt|cli|repo|work|[A-Za-z]{1,2}"

# Repository-owner URLs are public attribution, not private machine identifiers.
# These expressions flag deployment-shaped identifiers without storing an owner's
# real account names, SSH aliases, or session values in the checker itself.
PATTERNS = (
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b")),
    ("github_token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}\b")),
    ("api_key", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{24,}\b")),
    ("credential_url", re.compile(r"\b(?:https?|ssh)://[^\s/@:]+:[^\s/@]+@")),
    ("private_host", re.compile(r"\bhost\d+(?:public|private)\b", re.IGNORECASE)),
    ("private_host", re.compile(r"\b(?:DGX[-]Spark[-][a-z0-9_-]+[-]tunnelterm|spark[-][0-9a-f]{4})\b", re.IGNORECASE)),
    ("personal_path", re.compile(rf"(?:[A-Za-z]:)?[/\\](?:Users|home)[/\\](?!(?:{SYNTHETIC_HOME_NAMES})(?=[/\\]|\b))[A-Za-z0-9_.-]+(?=[/\\]|\b)", re.IGNORECASE)),
    ("personal_path", re.compile(rf"[A-Za-z]:[/\\](?!(?:{SYNTHETIC_WINDOWS_ROOTS})(?=[/\\]|\b))[A-Za-z0-9_.-]+[/\\]", re.IGNORECASE)),
    ("session_identifier", re.compile(r"\b01[0-9a-f]{6}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE)),
)


@dataclass(frozen=True, order=True)
class Finding:
    path: str
    line: int
    category: str


def private_path(path: str) -> bool:
    parsed = PurePosixPath(path)
    if parsed.as_posix() == PUBLIC_SOURCE_METADATA:
        return False
    parts = tuple(part.lower() for part in parsed.parts)
    name = parsed.name.lower()
    return (
        any(part in PRIVATE_DIRS or part.endswith(".egg-info") for part in parts)
        or name in PRIVATE_NAMES
        or (name.startswith(".env") and name not in {".env.example", ".env.sample"})
        or bool(re.search(r"\.(?:log|jsonl|pem|key|pyc|pyo|vsix)$", name))
        or bool(re.search(r"\.(?:sqlite(?:3)?|db)(?:-.*)?$", name))
    )


def inspect_blob(path: str, blob: bytes) -> tuple[list[Finding], bool]:
    """Return findings and whether an allowed binary asset was not text-scanned."""
    if private_path(path):
        return [Finding(path, 0, "private_artifact")], False
    if PurePosixPath(path).suffix.lower() in BINARY_ARTWORK:
        return [], True
    if len(blob) > MAX_TEXT_BYTES:
        return [Finding(path, 0, "oversized_unreviewed_file")], False
    try:
        source = blob.decode("utf-8-sig")
    except UnicodeDecodeError:
        return [Finding(path, 0, "unsupported_binary")], False
    if "\0" in source:
        return [Finding(path, 0, "unsupported_binary")], False
    findings = set()
    for number, line in enumerate(source.splitlines(), 1):
        for category, expression in PATTERNS:
            # Tests deliberately exercise synthetic platform paths and unsafe
            # path shapes. Do not erase valid fixtures to satisfy a heuristic.
            # This exception never applies to credentials or other identifiers;
            # it does not assert that every private path in tests is detected.
            if category == "personal_path" and PurePosixPath(path).parts[0] == "tests":
                continue
            if expression.search(line):
                findings.add(Finding(path, number, category))
    return sorted(findings), False


def candidate_paths(root: Path) -> Iterable[str]:
    """Exclude private local state before opening any content; do not follow links."""
    for parent, directories, files in os.walk(root, followlinks=False):
        directories[:] = sorted(
            name for name in directories
            if (Path(parent) / name).relative_to(root).as_posix() in PUBLIC_SOURCE_PARENTS
            or not private_path((Path(parent) / name).relative_to(root).as_posix())
        )
        for name in sorted(files):
            relative = (Path(parent) / name).relative_to(root).as_posix()
            if not private_path(relative):
                yield relative


def git_command(root: Path, *arguments: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments], stdin=subprocess.DEVNULL,
        capture_output=True, check=False,
    )
    if completed.returncode:
        # Git's stderr can contain deployment paths; expose only a stable category.
        raise RuntimeError("git_index_unavailable")
    return completed.stdout


def check(root: Path, *, tracked: bool = False) -> tuple[list[Finding], int, int]:
    if tracked:
        paths = sorted(set(
            item.decode("utf-8", errors="strict")
            for item in git_command(root, "ls-files", "--cached", "-z").split(b"\0") if item
        ))
    else:
        paths = list(candidate_paths(root))
    findings: list[Finding] = []
    checked = assets = 0
    for relative in paths:
        if private_path(relative):
            findings.append(Finding(relative, 0, "private_artifact"))
            continue
        if tracked:
            if relative == PUBLIC_SOURCE_METADATA:
                # A symlink with an innocuous target string is not safe source
                # metadata. Check the index mode without following its target.
                entries = [entry for entry in git_command(root, "ls-files", "--stage", "-z", "--", relative).split(b"\0") if entry]
                modes = [entry.split(b" ", 1)[0] for entry in entries]
                if modes != [b"100644"]:
                    findings.append(Finding(relative, 0, "unreviewed_source_metadata_mode"))
                    continue
            blob = git_command(root, "show", ":" + relative)
        else:
            target = root / relative
            if target.is_symlink():
                findings.append(Finding(relative, 0, "unreviewed_symlink"))
                continue
            try:
                blob = target.read_bytes()
            except OSError:
                findings.append(Finding(relative, 0, "unreadable_candidate"))
                continue
        detected, unscanned_asset = inspect_blob(relative, blob)
        findings.extend(detected)
        checked += int(not unscanned_asset)
        assets += int(unscanned_asset)
    return sorted(set(findings)), checked, assets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--tracked", action="store_true", help="Scan Git index blobs, refusing private tracked artifacts")
    arguments = parser.parse_args(argv)
    try:
        findings, checked, assets = check(arguments.root.resolve(), tracked=arguments.tracked)
    except (RuntimeError, UnicodeError, OSError):
        print("publication_check_error: candidate_index_unavailable", file=sys.stderr)
        return 2
    for finding in findings:
        # Only path, line number, and category. Never source text or matched values.
        print(f"{finding.path}:{finding.line}: {finding.category}")
    print(f"publication_check: text_files={checked} binary_assets_unscanned={assets} findings={len(findings)}")
    print("Scope: current candidate source or Git index only; not runtime state, Git history, asset metadata, or a complete security audit.")
    print("Test-fixture exception: generic absolute paths in tests require human review; keys, host/session identifiers, and private artifacts remain checked.")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
