"""Public connection-service entrypoint. No legacy App Server code is imported."""
from terminal_hub import Hub, ROOT

VERSION = "0.5.0"

__all__ = ["Hub", "ROOT", "VERSION"]
