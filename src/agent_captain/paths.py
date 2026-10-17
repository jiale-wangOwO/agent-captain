"""Explicit checkout, source and private-state boundaries.

AgentCaptain is deployed from a retained checkout (including Node dependencies
and web assets). Editable installation exposes the Python CLI; it never moves
the deployment's private state into site-packages or the caller's directory.
"""
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
ROOT = PACKAGE_ROOT.parents[1]
TERMINAL_ROOT = ROOT / "terminal"
BROKER_SCRIPT = TERMINAL_ROOT / "terminal_broker.cjs"
LEGACY_BROKER_SCRIPT = ROOT / "terminal_broker.cjs"


def source_path(group: str, filename: str) -> Path:
    """Locate a bundled source resource, not a user-selected filesystem path."""
    return PACKAGE_ROOT / group / filename


def require_checkout() -> None:
    """Do not silently create private data in an incomplete wheel installation."""
    if not all(path.is_file() for path in (
            ROOT / "pyproject.toml", ROOT / "scripts" / "captain.py",
            ROOT / "web" / "index.html", BROKER_SCRIPT)):
        raise RuntimeError("AgentCaptain requires a retained source checkout. Run python scripts/captain.py there, or pip install -e . from that checkout.")
