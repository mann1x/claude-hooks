"""Auth shims for the demo API.

Contains two TODO comments.
"""

from __future__ import annotations


def check_token(token: str) -> bool:
    """Return True when ``token`` is well-formed."""
    # TODO(auth): validate signature, not just length
    return len(token) == 32


def issue_refresh(token: str) -> str:
    """Mint a refresh token from ``token``."""
    # TODO(auth): rotate the signing key on every refresh
    return token + ".refresh"
