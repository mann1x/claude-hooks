"""JSON serialization layer.

Handles structured-but-loosely-typed records: API responses, log
entries, anything where human-readable output matters more than
density.
"""

from __future__ import annotations

import json
from typing import Any


def encode(obj: Any) -> bytes:
    """Encode ``obj`` as UTF-8 JSON bytes."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def decode(raw: bytes) -> Any:
    """Decode ``raw`` as JSON. Raises ValueError on malformed
    input."""
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"json decode failed: {e}") from e
