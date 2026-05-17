"""Tiny in-memory user store used by auth.handle_auth.

A red-herring file for the bench: it sits in the cohort but does
NOT contain the function the question asks about. A model that
reads every file blindly will waste tokens here.
"""

from __future__ import annotations


SEED_USERS = {
    "alice": {
        "password_hash": "abc:e9d71f5ee7c92d6dc9e92ffdad17b8bd",
        "token": None,
        "last_login": None,
    },
    "bob": {
        "password_hash": "def:8b1a9953c4611296a827abf8c47804d7",
        "token": None,
        "last_login": None,
    },
}


def load_users() -> dict:
    """Return a fresh copy of the seed user dict."""
    return {k: dict(v) for k, v in SEED_USERS.items()}
