"""Allow python -m agent_captain after editable installation."""
from agent_captain.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
