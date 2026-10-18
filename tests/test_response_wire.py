"""Strict bounded response transport, without network or real session data."""
import base64
import json
import math
import unittest
from unittest.mock import patch
import zlib

from agent_captain.sessions import response_wire as wire


def envelope(raw, *, compressed=None):
    data = zlib.compress(raw) if compressed is None else compressed
    return json.dumps({"encoding": wire.ENCODING, "data": base64.b64encode(data).decode("ascii")})


class ResponseWireTests(unittest.TestCase):
    def assert_invalid(self, value):
        with self.assertRaises(ValueError) as caught:
            wire.decode_reply(value)
        self.assertEqual(str(caught.exception), "Invalid SSH response envelope")
        self.assertIsNone(caught.exception.__cause__)
        self.assertNotIn("PRIVATE_SECRET", str(caught.exception))

    def test_large_unicode_and_full_tool_records_round_trip_and_compress(self):
        value = {"ok": True, "result": {"messages": [{"role": "tool", "name": "exec_command",
                 "text": "完整工具正文🙂 ' \" $() ` \\ \n" * 20000}], "content_complete": True}}
        encoded = wire.encode_reply(value)
        self.assertEqual(set(json.loads(encoded)), {"encoding", "data"})
        self.assertEqual(json.loads(encoded)["encoding"], "agentcaptain.zlib-json.v1")
        self.assertEqual(wire.decode_reply(encoded), value)
        self.assertLess(len(encoded), len(json.dumps(value, ensure_ascii=False).encode()) // 20)
        self.assertTrue(encoded.isascii())

    def test_legacy_plain_json_objects_and_newline_remain_compatible(self):
        for value in ({}, {"ok": True, "result": {"text": "中文🙂", "values": [None, False, 3]}},
                      {"ok": False, "error": "fixed error"}, {"data": "ordinary legacy field"}):
            with self.subTest(value=value):
                self.assertEqual(wire.decode_reply(json.dumps(value, ensure_ascii=False) + "\n"), value)

    def test_encoded_schema_requires_known_encoding_and_exact_keys(self):
        original = json.loads(wire.encode_reply({"ok": True}))
        for value in (original | {"encoding": "unknown"}, original | {"encoding": None},
                      original | {"extra": "PRIVATE_SECRET"}, {"encoding": wire.ENCODING},
                      original | {"data": None}, original | {"data": []}, original | {"data": 1}):
            with self.subTest(value=value):
                self.assert_invalid(json.dumps(value))

    def test_invalid_noncanonical_or_whitespace_base64_is_rejected(self):
        for data in ("!!!PRIVATE_SECRET", "not-padding", " YQ==", "YQ==\n", "YQ===", "YR==", "中文"):
            with self.subTest(data=data):
                self.assert_invalid(json.dumps({"encoding": wire.ENCODING, "data": data}))

    def test_invalid_or_truncated_zlib_is_rejected(self):
        original = zlib.compress(b'{"ok":true}')
        for compressed in (b"", b"PRIVATE_SECRET", original[:-1], original[:-4], original[:2]):
            with self.subTest(compressed=compressed):
                self.assert_invalid(envelope(b"", compressed=compressed))

    def test_trailing_bytes_or_second_compressed_stream_are_rejected(self):
        compressed = zlib.compress(b'{"ok":true}')
        for trailing in (b"PRIVATE_SECRET", b"\0", zlib.compress(b'{"other":true}')):
            with self.subTest(trailing=trailing):
                self.assert_invalid(envelope(b"", compressed=compressed + trailing))

    def test_inflate_limit_is_enforced_for_bombs_and_unconsumed_compressed_tail(self):
        bomb = b'{"text":"' + b"a" * wire.MAX_REPLY_BYTES + b'"}'
        self.assert_invalid(envelope(bomb))
        with patch.object(wire, "MAX_REPLY_BYTES", 32):
            self.assert_invalid(envelope(b'{"text":"' + b"b" * 4096 + b'"}'))

    def test_exact_uncompressed_limit_is_accepted_and_one_more_byte_is_rejected(self):
        raw = b'{"text":"' + b"a" * 53 + b'"}'
        self.assertEqual(len(raw), 64)
        with patch.object(wire, "MAX_REPLY_BYTES", 64):
            self.assertEqual(wire.decode_reply(envelope(raw)), {"text": "a" * 53})
            self.assert_invalid(envelope(raw + b" "))
            self.assertEqual(wire.decode_reply(raw.decode()), {"text": "a" * 53})
            self.assert_invalid((raw + b" ").decode())

    def test_invalid_utf8_and_escaped_surrogates_are_rejected(self):
        for raw in (b'{"text":"\xffPRIVATE_SECRET"}', b'{"text":"\xed\xa0\x80"}', b'{"text":"\\ud800"}'):
            with self.subTest(raw=raw):
                self.assert_invalid(envelope(raw))
        self.assert_invalid('{"text":"\ud800"}')
        self.assert_invalid('{"text":"\\ud800"}')

    def test_json_syntax_duplicate_keys_nonfinite_and_nonobjects_are_rejected(self):
        for raw in (b"PRIVATE_SECRET", b"{bad}", b"[]", b"null", b"true", b"1", b'"text"',
                    b'{"ok":true} {"other":true}', b'{"ok":true,"ok":false}',
                    b'{"value":NaN}', b'{"value":Infinity}', b'{"value":-Infinity}',
                    b'{"value":1e99999}', b'{"value":-1e99999}',
                    b'{"nested":{"x":1,"x":2}}'):
            with self.subTest(raw=raw):
                self.assert_invalid(envelope(raw))
                self.assert_invalid(raw.decode())
        self.assert_invalid('{"encoding":"x","encoding":"y","data":""}')

    def test_wire_size_and_input_type_are_bounded_before_parsing(self):
        with patch.object(wire, "MAX_WIRE_BYTES", 32):
            self.assert_invalid(" " * 33)
        for value in (None, b'{"ok":true}', [], 123):
            with self.subTest(value=value):
                self.assert_invalid(value)

    def test_encoder_rejects_invalid_objects_nan_unicode_and_oversized_body_safely(self):
        circular = {}
        circular["self"] = circular
        for value in (None, [], {"float": math.nan}, {"float": math.inf}, {"text": "\ud800"}, circular):
            with self.subTest(kind=type(value).__name__):
                with self.assertRaises(ValueError) as caught:
                    wire.encode_reply(value)
                self.assertEqual(str(caught.exception), "Invalid SSH response envelope")
        with patch.object(wire, "MAX_REPLY_BYTES", 8), self.assertRaises(ValueError):
            wire.encode_reply({"text": "PRIVATE_SECRET"})


if __name__ == "__main__":
    unittest.main()
