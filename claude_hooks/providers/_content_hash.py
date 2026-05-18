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
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional


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


def compute_expires_at(
    now: Optional[datetime] = None,
    ttl_seconds: Optional[float] = None,
) -> Optional[str]:
    """Return the ISO-8601 expiry timestamp for a row with this TTL.

    M14 helper shared by the pgvector + sqlite_vec providers and the
    ``ProviderBackedStore`` adapter. Returns ``None`` when ``ttl_seconds``
    is ``None``, zero, or negative — those callers want a "never
    expire" row (stored as ``NULL`` in the underlying column).

    Args:
        now: UTC timestamp to anchor the computation to. Defaults to
            ``datetime.now(timezone.utc)`` so tests can pin a fixed
            anchor by passing one in.
        ttl_seconds: TTL in seconds. ``None`` / non-positive → never.

    Returns:
        ISO-8601 string (e.g. ``"2026-06-16T22:00:00+00:00"``) or
        ``None``. Always timezone-aware (UTC) to keep cross-store
        comparisons unambiguous.
    """
    if ttl_seconds is None or ttl_seconds <= 0:
        return None
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        # Naive timestamps are normalized to UTC — the providers
        # store everything in UTC, so a naive "now" from a buggy
        # caller would otherwise produce wrong expiry math.
        now = now.replace(tzinfo=timezone.utc)
    return (now + timedelta(seconds=float(ttl_seconds))).isoformat()


@dataclass(frozen=True)
class ExpiringRow:
    """Provider-agnostic row representation used by the M14 daemon
    sweep. Both pgvector and sqlite_vec return lists of these from
    ``expire_before``; the daemon doesn't care which provider
    produced them.

    Attributes:
        content_hash: SHA-256 of the row's normalised content; the
            stable idempotency key shared across providers (see
            :func:`content_hash`). The daemon uses it to drive
            :meth:`StoreProvider.delete_by_hashes`.
        content: The raw text of the row. Carried so the
            distillation rubric can read it without a second query.
        metadata: The JSON metadata blob the row was stored with.
            Contains the ``namespace`` tuple-as-list, the
            ``_consultants_store`` marker, and any per-row context
            the writer attached (e.g. ``cwd``, ``lane_idx``).
        expires_at: ISO-8601 string the row was filtered on. Carried
            for forensics and so the daemon can recompute a grace
            window if needed.
    """
    content_hash: bytes
    content: str
    metadata: dict = field(default_factory=dict)
    expires_at: str = ""
