"""Auth module — handles login + token issuance for the demo app.

Hand-authored synthetic fixture. The function the bench question
asks about is ``handle_auth`` below. The oracle checks that the
answer cites the line where ``def handle_auth`` is defined; if
you reflow this file, update the oracle's expected line number.
"""

from __future__ import annotations

import hashlib
import secrets

from utils import now_iso


# Hash algorithm pinned to keep tokens stable across upgrades.
HASH_ALG = "sha256"


def _hash_password(password: str, salt: str) -> str:
    """Return ``salt:hexdigest`` of the password under HASH_ALG."""
    h = hashlib.new(HASH_ALG)
    h.update((salt + password).encode("utf-8"))
    return f"{salt}:{h.hexdigest()}"


def issue_token() -> str:
    """Return a fresh opaque bearer token."""
    return secrets.token_urlsafe(32)


def handle_auth(username: str, password: str, store: dict) -> str:
    """Authenticate ``username`` + ``password`` against ``store``.

    Returns a fresh bearer token on success. Raises
    ``PermissionError`` on bad credentials. The caller is
    responsible for persisting the token; this function does not
    touch the database.
    """
    record = store.get(username)
    if record is None:
        raise PermissionError("unknown user")
    salt, expected_hex = record["password_hash"].split(":", 1)
    actual = _hash_password(password, salt).split(":", 1)[1]
    if not secrets.compare_digest(actual, expected_hex):
        raise PermissionError("bad password")
    token = issue_token()
    record["last_login"] = now_iso()
    return token


def revoke_token(token: str, store: dict) -> bool:
    """Mark a token revoked. Returns True if found, False otherwise."""
    for record in store.values():
        if record.get("token") == token:
            record["token"] = None
            return True
    return False
