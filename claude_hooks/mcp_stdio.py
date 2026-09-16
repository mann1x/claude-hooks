"""UTF-8 on the stdio transport, which Windows does not give us.

MCP frames are UTF-8 by specification, but a stdio server inherits the
*locale* encoding for its pipes. On pandorum that is ``cp1252``, so a
request carrying an accent, an em dash or any CJK arrived decoded
against the wrong codec.

The failure is quiet, which is why this module exists rather than a
comment. cp1252 maps almost every byte to *some* character, so a
mangled body is stored and returned without an error anywhere — a
message the sender wrote and the recipient read, differing. Only the
handful of undefined cp1252 slots raise, and a ``UnicodeDecodeError``
raised by the ``for raw in sys.stdin`` iterator is not catchable by the
per-line handler inside the loop: it propagates out and ends the
server.

Output needs this less than input does — responses go through
``json.dumps``, which escapes non-ASCII by default — but "less" is not
"never", and the only way that guarantee holds is if nobody ever passes
``ensure_ascii=False``.
"""
from __future__ import annotations

import sys


def force_utf8_stdio() -> None:
    """Re-decode stdin/stdout/stderr as UTF-8, in place.

    Safe to call more than once and safe when the streams have been
    replaced by something without ``reconfigure`` — a test's
    ``StringIO`` needs no decoding because it never held bytes.

    ``errors="replace"`` is deliberate on all three. A corrupt byte
    should cost the line it arrived on, which the caller already
    handles as malformed JSON, not the process.
    """
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            # Already-detached or non-seekable exotic stream. Losing
            # UTF-8 is bad; refusing to start is worse.
            pass
