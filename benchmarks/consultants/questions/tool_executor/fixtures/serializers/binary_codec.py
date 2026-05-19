"""Binary (length-prefixed) serialization layer.

Used for high-throughput inter-service messages where wire size
dominates and the schema is fixed by the caller. The format is:

    <4 bytes: big-endian length><payload>

There is no type tagging — the caller knows what's coming.
"""

from __future__ import annotations

import struct


def encode(payload: bytes) -> bytes:
    """Length-prefix ``payload``."""
    return struct.pack(">I", len(payload)) + payload


def decode(raw: bytes) -> bytes:
    """Strip the length prefix and return the inner payload."""
    if len(raw) < 4:
        raise ValueError("binary frame: header truncated")
    (length,) = struct.unpack(">I", raw[:4])
    if len(raw) < 4 + length:
        raise ValueError(
            f"binary frame: payload truncated (need {length} got {len(raw) - 4})"
        )
    return raw[4:4 + length]
