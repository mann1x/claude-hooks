"""Utility helpers shared across the auth fixture."""

from __future__ import annotations

import datetime as _dt


def now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return _dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"


def safe_username(raw: str) -> str:
    """Strip whitespace and lowercase a username; the auth layer
    treats usernames case-insensitively."""
    return raw.strip().lower()
