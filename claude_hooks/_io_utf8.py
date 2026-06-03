"""Force UTF-8 on the process stdout/stderr streams.

CLI commands in this repo emit rich glyphs — arrows (``→``), box-drawing,
the occasional emoji — in their reports. On a legacy Windows console the
default code page is cp1252, so ``print("a → b")`` raises
``UnicodeEncodeError: 'charmap' codec can't encode character '\\u2192'`` and the
command dies with a non-zero exit. Reconfiguring the text streams to UTF-8
(available since Python 3.7) keeps the rich output and works on every console.

No-op on POSIX (already UTF-8) and harmless when a stream cannot be reconfigured
(e.g. it was replaced by a non-``TextIOWrapper`` capture object).
"""
from __future__ import annotations

import sys


def force_utf8_streams() -> None:
    """Best-effort switch stdout+stderr to UTF-8 with ``errors="replace"``."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue  # not a TextIOWrapper (e.g. pytest capture) — leave it
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass  # detached / already-closed stream — nothing we can do
