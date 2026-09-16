"""The bug that made cclsp unusable, asserted against our framing.

cclsp accumulated the LSP stream in a **JS string** and then sliced it
with ``String.substring`` using the ``Content-Length`` value. That value
is defined by the spec in *bytes*; ``substring`` counts *UTF-16 code
units*. They agree for ASCII and diverge on the first multi-byte
character.

clangd renders every function/method hover as ``→ <return type>``, and
``→`` is U+2192: three bytes, one code unit. So the slice over-ran by
two, ate the head of the following message, and the buffer stayed
misaligned **forever** — every later request on that server timed out,
including ones that had worked seconds earlier. Observed 2026-09-16:

    03:47:59  get_hover  .cpp   ->    17ms  OK
    03:48:11  get_hover  .cu    ->     30s  TIMEOUT
    03:49:42  find_definition   ->     30s  TIMEOUT   <- wedged
    04:04:05  (after restart)   ->      2s  OK
    04:04:18  get_hover         ->     30s  TIMEOUT   <- wedged again

Our reader takes ``stream.read(length)`` from a *binary* stream, so it
consumes exactly N bytes and decodes afterwards. These tests pin that
property rather than trusting it, because "it looks right" is how the
original shipped.
"""
from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from claude_hooks.lsp_engine.lsp import LspProtocolError, LspClient  # noqa: E402

_read_frame = LspClient._read_frame.__func__ \
    if hasattr(LspClient._read_frame, "__func__") else LspClient._read_frame


def frame(payload: dict) -> bytes:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body


class MultibyteFramingTests(unittest.TestCase):

    def test_arrow_hover_then_next_message_still_reads(self):
        """The exact cclsp killer: U+2192 followed by another message."""
        hover = {"id": 1, "result": {"contents": "auto read_tensor() → bool"}}
        following = {"id": 2, "result": {"contents": "int"}}
        stream = io.BytesIO(frame(hover) + frame(following))

        first = _read_frame(stream)
        second = _read_frame(stream)

        self.assertEqual(first["result"]["contents"], "auto read_tensor() → bool")
        self.assertEqual(second["id"], 2, "stream desynced after a multi-byte body")

    def test_byte_length_and_char_length_actually_differ_here(self):
        """Guard the guard: if this ever became ASCII the test would
        pass while proving nothing."""
        body = json.dumps({"contents": "→"}, ensure_ascii=False)
        self.assertNotEqual(len(body), len(body.encode("utf-8")))

    def test_many_multibyte_messages_stay_aligned(self):
        """One over-run is enough to wedge forever, so drift must be
        zero across a run, not merely small."""
        msgs = [{"id": i, "result": {"contents": f"f{i}() → std::vector<T> ✓ ∀"}}
                for i in range(50)]
        stream = io.BytesIO(b"".join(frame(m) for m in msgs))
        for i in range(50):
            self.assertEqual(_read_frame(stream)["id"], i)
        self.assertIsNone(_read_frame(stream), "trailing bytes left over")

    def test_emoji_outside_the_bmp(self):
        """Astral-plane characters are 4 bytes and *two* UTF-16 units —
        the same class of bug, doubled."""
        msg = {"id": 7, "result": {"contents": "status: 🚀 ok"}}
        stream = io.BytesIO(frame(msg) + frame({"id": 8, "result": None}))
        self.assertEqual(_read_frame(stream)["id"], 7)
        self.assertEqual(_read_frame(stream)["id"], 8)

    def test_split_reads_do_not_corrupt_a_character(self):
        """A multi-byte character split across two chunks must not become
        U+FFFD. BufferedReader.read(n) blocks for n bytes, which is the
        property that makes this safe."""
        data = frame({"id": 9, "result": {"contents": "→→→"}})
        raw = io.BufferedReader(io.BytesIO(data), buffer_size=8)
        self.assertEqual(_read_frame(raw)["result"]["contents"], "→→→")

    def test_truncated_body_returns_none_rather_than_garbage(self):
        full = frame({"id": 10, "result": {"contents": "→"}})
        stream = io.BytesIO(full[:-3])          # cut mid-character
        self.assertIsNone(_read_frame(stream))

    def test_malformed_json_raises_instead_of_continuing_mid_stream(self):
        """cclsp logged a parse failure to stderr and kept reading from a
        desynced position, turning one bad frame into a permanent wedge.
        Raising lets the caller tear down and respawn."""
        body = b"{not json"
        data = (b"Content-Length: " + str(len(body)).encode()
                + b"\r\n\r\n" + body)
        with self.assertRaises(LspProtocolError):
            _read_frame(io.BytesIO(data))

    def test_missing_content_length_raises(self):
        with self.assertRaises(LspProtocolError):
            _read_frame(io.BytesIO(b"X-Other: 1\r\n\r\n{}"))


if __name__ == "__main__":
    unittest.main()
