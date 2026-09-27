"""Filters and pages for mailbox listings.

A session that has used the mailbox for weeks can hold hundreds of
messages, and a listing that returns all of them is a wall of text the
reader has to scroll past to find the one it wants. So every listing is
a page: a bounded slice with the total it came from, and the filters
that narrowed it, so the reader can ask for the next slice or a
narrower one instead of the whole thing.

The filtering itself happens in SQL (:mod:`claude_hooks.mailbox.store`);
this module only turns what a caller typed into values the store can
bind.
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

#: What one page holds unless the caller asks otherwise. Twenty one-line
#: entries read at a glance; a hundred is the most any caller may ask for.
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100

#: How many unread messages the hook announcements spell out before
#: summarising the rest as a count.
ANNOUNCE_MAX = 10

ORDERS = ("newest", "oldest")

_RELATIVE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([mhdw])\s*$", re.IGNORECASE)
_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}
_DATE_ONLY = re.compile(r"^\s*\d{4}-\d{2}-\d{2}\s*$")


class FilterError(ValueError):
    """A filter value the caller can fix; the message says how."""


def page_size(value, default: int = DEFAULT_PAGE_SIZE) -> int:
    try:
        n = int(value) if value is not None else default
    except (TypeError, ValueError):
        raise FilterError(f"limit must be a number, not {value!r}.")
    return max(1, min(n, MAX_PAGE_SIZE))


def page_number(value) -> int:
    try:
        n = int(value) if value is not None else 1
    except (TypeError, ValueError):
        raise FilterError(f"page must be a number, not {value!r}.")
    return max(1, n)


def parse_when(text, *, end: bool = False,
               now: Optional[datetime] = None) -> Optional[datetime]:
    """``since`` / ``until`` as an aware UTC datetime, or None.

    Accepts a relative age (``30m``, ``12h``, ``3d``, ``2w`` — that long
    ago), a date (``2026-09-27``) or a date and time
    (``2026-09-27T14:30``, with an optional ``Z`` / ``+02:00``). A time
    without a zone is taken in this host's local zone, which is the zone
    the operator reads the clock in. A bare date used as ``until``
    (``end=True``) includes that whole day.
    """
    if text is None:
        return None
    if isinstance(text, datetime):
        return text if text.tzinfo else text.astimezone()
    s = str(text).strip()
    if not s:
        return None
    now = now or datetime.now(timezone.utc)
    m = _RELATIVE.match(s)
    if m:
        return now - timedelta(
            seconds=float(m.group(1)) * _UNIT_SECONDS[m.group(2).lower()])
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise FilterError(
            f"Could not read {s!r} as a time. Use an age like 3d / 12h / "
            "30m, a date like 2026-09-27, or 2026-09-27T14:30.")
    if dt.tzinfo is None:
        dt = dt.astimezone()          # host-local
    if end and _DATE_ONLY.match(s):
        dt += timedelta(days=1)       # the whole day
    return dt.astimezone(timezone.utc)


def split_terms(query) -> list[str]:
    """Keywords, each of which must appear (case-insensitively).

    ``"exact phrase"`` keeps words together; an unbalanced quote falls
    back to plain whitespace splitting rather than refusing the search.
    """
    if not query:
        return []
    s = str(query)
    try:
        terms = shlex.split(s)
    except ValueError:
        terms = s.split()
    return [t for t in (t.strip() for t in terms) if t]


def like_pattern(term: str) -> str:
    """``%term%`` with LIKE's own wildcards escaped (``ESCAPE '\\'``)."""
    esc = term.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


@dataclass
class Page:
    """One slice of a listing and what it was cut from."""
    rows: list
    total: int
    page: int
    page_size: int

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.page_size))

    @property
    def first(self) -> int:
        return (self.page - 1) * self.page_size + 1 if self.rows else 0

    @property
    def last(self) -> int:
        return self.first + len(self.rows) - 1 if self.rows else 0

    @property
    def has_more(self) -> bool:
        return self.page < self.pages


@dataclass
class ListFilter:
    """Everything that narrows a listing, in the caller's terms, so a
    page can say what it is a page *of* and how to get the next one."""
    query: str = ""
    since: str = ""
    until: str = ""
    party: str = ""                 # sender for the inbox, recipient for sent
    include_read: bool = False
    order: str = ""
    extra: dict = field(default_factory=dict)

    def describe(self, party_word: str) -> str:
        bits = []
        if self.query:
            bits.append(f"matching {self.query!r}")
        if self.party:
            bits.append(f"{party_word} {self.party}")
        if self.since:
            bits.append(f"since {self.since}")
        if self.until:
            bits.append(f"until {self.until}")
        return ", ".join(bits)

    def args(self, **more) -> dict:
        """The tool arguments that reproduce this listing."""
        out: dict = {}
        if self.include_read:
            out["include_read"] = True
        for k in ("query", "since", "until", "order"):
            v = getattr(self, k)
            if v:
                out[k] = v
        out.update(self.extra)
        out.update(more)
        return out
