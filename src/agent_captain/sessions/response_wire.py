"""Bounded JSON response compression for portable read-only SSH helpers."""
from __future__ import annotations

import base64
import json
import math
import zlib


ENCODING = "agentcaptain.zlib-json.v1"
MAX_REPLY_BYTES = 4 * 1024 * 1024
MAX_WIRE_BYTES = 8 * 1024 * 1024
_ERROR = "Invalid SSH response envelope"


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(_ERROR)
        result[key] = value
    return result


def _constant(_):
    raise ValueError(_ERROR)


def _float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(_ERROR)
    return number


def _json_object(text):
    value = json.loads(text, object_pairs_hook=_object, parse_constant=_constant, parse_float=_float)
    if not isinstance(value, dict):
        raise ValueError(_ERROR)
    # JSON escapes can describe lone UTF-16 surrogates despite a valid UTF-8
    # byte stream. Reject them before downstream serialization can fail.
    def unicode_value(item):
        if isinstance(item, str):
            item.encode("utf-8", errors="strict")
        elif isinstance(item, dict):
            for key, child in item.items():
                unicode_value(key)
                unicode_value(child)
        elif isinstance(item, list):
            for child in item:
                unicode_value(child)
    unicode_value(value)
    return value


def encode_reply(value) -> str:
    """Encode one reply object; no response body enters shell arguments."""
    try:
        if not isinstance(value, dict):
            raise ValueError(_ERROR)
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8", errors="strict")
        if len(raw) > MAX_REPLY_BYTES:
            raise ValueError(_ERROR)
        data = base64.b64encode(zlib.compress(raw, 9)).decode("ascii")
        return json.dumps({"encoding": ENCODING, "data": data}, separators=(",", ":"))
    except Exception:
        raise ValueError(_ERROR) from None


def decode_reply(stdout: str) -> dict:
    """Decode legacy JSON or one strict compressed envelope, with a hard cap."""
    try:
        if not isinstance(stdout, str):
            raise ValueError(_ERROR)
        size = len(stdout.encode("utf-8", errors="strict"))
        if size > MAX_WIRE_BYTES:
            raise ValueError(_ERROR)
        envelope = _json_object(stdout)
        if "encoding" not in envelope:
            if size > MAX_REPLY_BYTES:
                raise ValueError(_ERROR)
            return envelope
        if set(envelope) != {"encoding", "data"} or envelope["encoding"] != ENCODING or not isinstance(envelope["data"], str):
            raise ValueError(_ERROR)
        compressed = base64.b64decode(envelope["data"], validate=True)
        if base64.b64encode(compressed).decode("ascii") != envelope["data"]:
            raise ValueError(_ERROR)
        decoder = zlib.decompressobj()
        raw = decoder.decompress(compressed, MAX_REPLY_BYTES + 1)
        # Never flush without a bounded max_length: flush's size argument is
        # only an initial buffer allocation, not a decompression output cap.
        if len(raw) > MAX_REPLY_BYTES or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            raise ValueError(_ERROR)
        return _json_object(raw.decode("utf-8", errors="strict"))
    except Exception:
        raise ValueError(_ERROR) from None
