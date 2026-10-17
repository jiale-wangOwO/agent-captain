"""Checkout entrypoint: no pip install or current-directory assumptions."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_captain.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
