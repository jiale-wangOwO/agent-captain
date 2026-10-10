"""Public connection-service entrypoint. No legacy App Server code is imported."""
from terminal_hub import Hub, ROOT

VERSION = "0.6.1"

__all__ = ["Hub", "ROOT", "VERSION"]
