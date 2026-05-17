"""In-memory cache layer.

Contains one TODO comment.
"""

from __future__ import annotations


_STORE: dict[str, object] = {}


def get(key: str) -> object:
    return _STORE.get(key)


def put(key: str, value: object) -> None:
    # TODO(cache): wire eviction so the dict can't grow unbounded
    _STORE[key] = value


def clear() -> None:
    _STORE.clear()
