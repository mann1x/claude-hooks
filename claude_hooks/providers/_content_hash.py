"""Shared content_hash for memory provider idempotency.

Both ``PgvectorProvider`` and ``SqliteVecProvider`` use the same hash
function so a row migrated from one store to the other collides on
the same key. ``scripts/migrate_to_pgvector.py`` uses the same hash
too — production stores share one key space.

Algorithm: SHA-256 of the **whitespace-normalised** UTF-8 string.
Normalisation collapses any whitespace run (spaces, tabs, newlines)
to a single space and strips leading/trailing whitespace; everything
else (case, punctuation, unicode) is preserved.

Why normalise: a user who pastes "  foo\\n  bar  " and a tool that
sends "foo bar" should hit the same idempotency bucket. We don't go
further than whitespace (no lowercasing, no NFKC) because that's
where the principle of least surprise lives.
"""

from __future__ import annotations

import hashlib


def content_hash(text: str) -> bytes:
    """Return SHA-256(normalised(text)) as raw bytes (32 bytes).

    Whitespace normalisation: ``" ".join(text.split())`` collapses
    runs of whitespace to a single space and strips leading +
    trailing whitespace. Empty / whitespace-only input hashes the
    empty string — callers should reject empty content upstream of
    this function, not rely on the hash to distinguish them.
    """
    normalised = " ".join(text.split())
    return hashlib.sha256(normalised.encode("utf-8")).digest()
