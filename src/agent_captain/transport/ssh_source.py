"""Compact portable Python sent through an existing read-only SSH stdin pipe."""
from __future__ import annotations

import base64
import zlib


def pack_python_source(source: str) -> str:
    """Keep ``python3 -`` argv and EOF semantics while reducing source traffic.

    The ASCII loader travels on stdin, never in Windows command arguments. Its
    nested code retains the normal stdin filename, globals and error behavior.
    """
    if not isinstance(source, str):
        raise TypeError("Python source must be text")
    encoded = base64.b64encode(zlib.compress(source.encode("utf-8"), 9)).decode("ascii")
    return "import base64,zlib\nexec(compile(zlib.decompress(base64.b64decode(" + repr(encoded) + ")),'<stdin>','exec'))\n"
