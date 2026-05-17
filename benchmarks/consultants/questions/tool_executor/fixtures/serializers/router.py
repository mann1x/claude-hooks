"""Codec router — picks the right encoder by content-type.

Three codecs are wired in: JSON (the default), binary
(length-prefixed for inter-service), and CSV (for export).
Adding a fourth codec requires only a new entry in ``CODECS`` and
a matching pair of encode/decode functions.
"""

from __future__ import annotations

from . import binary_codec, csv_codec, json_codec


CODECS = {
    "application/json": json_codec,
    "application/octet-stream": binary_codec,
    "text/csv": csv_codec,
}


def encode(payload, content_type: str = "application/json") -> bytes:
    """Dispatch to the codec for ``content_type``."""
    codec = CODECS.get(content_type)
    if codec is None:
        raise ValueError(f"unknown content_type: {content_type!r}")
    return codec.encode(payload)


def decode(raw: bytes, content_type: str = "application/json"):
    """Dispatch to the codec for ``content_type``."""
    codec = CODECS.get(content_type)
    if codec is None:
        raise ValueError(f"unknown content_type: {content_type!r}")
    return codec.decode(raw)
