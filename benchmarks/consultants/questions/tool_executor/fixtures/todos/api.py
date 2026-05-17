"""Demo HTTP API — fixture for medium-01-multifile-audit.

This file contains TODO comments the bench question asks the
tool_executor to find. The oracle checks both the comment text
and the file/line citations.
"""

from __future__ import annotations


def get_user(user_id: str) -> dict:
    """Return the user record for ``user_id``."""
    # TODO(api): replace placeholder lookup with a real DB call
    return {"id": user_id, "name": "placeholder"}


def list_users() -> list[dict]:
    """Return every user record."""
    # TODO(api): add pagination — current impl loads the full
    # table into memory, which won't scale past 10k rows
    return []


def delete_user(user_id: str) -> bool:
    """Remove the user record with id ``user_id``."""
    return True
