"""CSV serialization layer.

Used for export endpoints where downstream consumers are
spreadsheets, not other services. Type information is lost on
encode — every field becomes a string.
"""

from __future__ import annotations

import csv
import io
from typing import Iterable


def encode(rows: Iterable[dict]) -> bytes:
    """Encode an iterable of dicts as CSV bytes."""
    rows_list = list(rows)
    if not rows_list:
        return b""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows_list[0].keys()))
    writer.writeheader()
    writer.writerows(rows_list)
    return buf.getvalue().encode("utf-8")


def decode(raw: bytes) -> list[dict]:
    """Decode ``raw`` CSV into a list of dicts."""
    text = raw.decode("utf-8")
    reader = csv.DictReader(io.StringIO(text))
    return list(reader)
