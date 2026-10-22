"""What a claim is and when two claims cannot be held together.

No I/O here: the store reads every live claim, hands them to
:func:`decide`, and writes back what it returns, so the whole policy is
these few functions and their tests.

**Resources** are names in a hierarchy, ``/``-separated: ``solidpc``,
``solidpc/gpu0``, ``bs2/gpu1``, ``eleven2go/ollama``, ``pod/runpod-a100``,
``ollama-cloud``. The first segment is the *root* — for a machine, the
host. Nothing has to be declared first; a definition only adds a
capacity and a description.

**Modes**

- ``shared`` — use alongside others. Several shared claims on one
  resource coexist, up to the resource's ``capacity`` in units when it
  has one (``ollama-cloud`` with capacity 3: three one-unit holders).
- ``exclusive`` — sole use of the resource *and everything under it*: an
  exclusive ``solidpc`` excludes ``solidpc/gpu0`` too, while an exclusive
  ``solidpc/gpu0`` leaves ``solidpc/gpu1`` and a shared ``solidpc`` free.
- ``quiet`` — sole use of the resource's whole root: nobody else may hold
  anything on that host, so a performance measurement on ``solidpc/gpu0``
  is not disturbed by a build on the CPU or a job on the other GPU. A
  quiet host is also what background work checks before starting
  (:func:`claude_hooks.resources.api.quiet_holder`).

**Claims** hold one or more items and are granted all at once or not at
all, so "gpu0 exclusive plus the host quiet" can never end half-held, and
two sessions each holding half of what the other wants cannot deadlock.

**Order.** Waiting claims are served first come, first served: a claim
is granted only when it conflicts neither with what is held nor with any
*earlier* claim still waiting. A stream of small shared claims therefore
cannot starve an exclusive one queued before them.

A claim never conflicts with itself; two claims of the same session do
conflict (one job per resource — opencoti bug-4004, two gates of one
session overlapping on the same GPU, both void).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

SHARED, EXCLUSIVE, QUIET = "shared", "exclusive", "quiet"
MODES = (SHARED, EXCLUSIVE, QUIET)
_MODE_ALIASES = {"s": SHARED, "sh": SHARED, "shared": SHARED,
                 "x": EXCLUSIVE, "ex": EXCLUSIVE, "excl": EXCLUSIVE,
                 "exclusive": EXCLUSIVE,
                 "q": QUIET, "quiet": QUIET}
#: How strong each mode is, for "already held" checks.
_STRENGTH = {SHARED: 0, EXCLUSIVE: 1, QUIET: 2}

_SEGMENT = r"[a-z0-9][a-z0-9._-]{0,62}"
_NAME_RX = re.compile(rf"^{_SEGMENT}(?:/{_SEGMENT}){{0,7}}$")


class SpecError(ValueError):
    pass


def normalize(name: str) -> str:
    """Lower-case, ``/``-separated, no empty segments. Raises
    :class:`SpecError` for anything else."""
    n = "/".join(p for p in str(name or "").strip().lower()
                 .replace("\\", "/").split("/") if p)
    if not _NAME_RX.match(n):
        raise SpecError(
            f"bad resource name {name!r}: segments of a-z 0-9 . _ -, "
            f"separated by /, e.g. solidpc/gpu0")
    return n


def root(name: str) -> str:
    return name.split("/", 1)[0]


def is_ancestor(a: str, b: str) -> bool:
    """``a`` strictly contains ``b`` (``solidpc`` ⊃ ``solidpc/gpu0``)."""
    return b.startswith(a + "/")


def parse_mode(text: str) -> str:
    mode = _MODE_ALIASES.get(str(text or "").strip().lower())
    if mode is None:
        raise SpecError(f"bad mode {text!r}: shared, exclusive or quiet")
    return mode


@dataclass(frozen=True)
class Item:
    resource: str
    mode: str = EXCLUSIVE
    units: int = 1

    def as_list(self) -> list:
        return [self.resource, self.mode, self.units]

    def __str__(self) -> str:
        unit = f":{self.units}" if self.units != 1 else ""
        return f"{self.resource}:{self.mode}{unit}"


def parse_item(spec: str, default_mode: str = EXCLUSIVE) -> Item:
    """``RES[:MODE[:UNITS]]`` — ``solidpc/gpu0``, ``solidpc:quiet``,
    ``ollama-cloud:shared:2``."""
    parts = str(spec or "").strip().split(":")
    if not parts or not parts[0]:
        raise SpecError(f"empty resource spec {spec!r}")
    if len(parts) > 3:
        raise SpecError(f"bad resource spec {spec!r}: RES[:MODE[:UNITS]]")
    mode = parse_mode(parts[1]) if len(parts) > 1 and parts[1] else default_mode
    units = 1
    if len(parts) == 3:
        try:
            units = int(parts[2])
        except ValueError:
            raise SpecError(f"bad units in {spec!r}") from None
        if units < 1:
            raise SpecError(f"units must be >= 1 in {spec!r}")
    return Item(normalize(parts[0]), mode, units)


def items_from(raw: Iterable) -> tuple[Item, ...]:
    """Items from their stored ``[[res, mode, units], …]`` form."""
    out = []
    for r in raw or ():
        res, mode, units = (list(r) + [EXCLUSIVE, 1])[:3]
        out.append(Item(normalize(res), parse_mode(mode), int(units or 1)))
    return tuple(out)


@dataclass
class Claim:
    """One request: its items, who made it and where it stands."""
    id: str
    seq: int
    items: tuple[Item, ...]
    state: str = "waiting"            # waiting | granted
    session_id: str = ""
    alias: str = ""
    host: str = ""
    pid: Optional[int] = None
    pid_started: Optional[float] = None
    reason: str = ""
    cwd: str = ""
    created_at: str = ""
    granted_at: str = ""
    renewed_at: str = ""
    expires_at: str = ""
    lease_s: int = 0
    extra: dict = field(default_factory=dict)

    @property
    def owner(self) -> str:
        """``alias@host``, plus the session's short id: two sessions in
        one directory share an alias."""
        who = f"{self.alias or '?'}@{self.host or '?'}"
        return f"{who}#{self.session_id[:6]}" if self.session_id else who

    def label(self) -> str:
        return ", ".join(str(i) for i in self.items)


def items_conflict(a: Item, b: Item) -> bool:
    """Can ``a`` and ``b``, held by two different claims, not coexist?
    Capacity is separate (:func:`blocker`)."""
    if a.mode == QUIET or b.mode == QUIET:
        return root(a.resource) == root(b.resource)
    if a.resource == b.resource:
        return not (a.mode == SHARED and b.mode == SHARED)
    if is_ancestor(a.resource, b.resource):
        return a.mode == EXCLUSIVE
    if is_ancestor(b.resource, a.resource):
        return b.mode == EXCLUSIVE
    return False


@dataclass(frozen=True)
class Block:
    """Why a claim cannot be granted now."""
    item: Item
    by: Optional[Claim]          # the conflicting claim, None for capacity
    kind: str                    # "conflict" | "capacity" | "queue"
    detail: str = ""

    def describe(self) -> str:
        if self.kind == "capacity":
            return f"{self.item.resource} is at capacity ({self.detail})"
        who = self.by.owner if self.by else "?"
        why = (f" — {self.by.reason}" if self.by and self.by.reason else "")
        verb = "queued before you by" if self.kind == "queue" else "held by"
        return (f"{self.item} conflicts with {self.by.label() if self.by else '?'} "
                f"{verb} {who}{why}")


def blocker(claim: Claim, others: Sequence[Claim],
            capacity: dict[str, int], *, kind: str = "conflict"
            ) -> Optional[Block]:
    """The first reason ``claim`` cannot be held next to ``others``."""
    for it in claim.items:
        for other in others:
            if other.id == claim.id:
                continue
            for ot in other.items:
                if items_conflict(it, ot):
                    return Block(it, other, kind)
        cap = capacity.get(it.resource)
        if it.mode == SHARED and cap is not None:
            load = sum(ot.units for other in others if other.id != claim.id
                       for ot in other.items
                       if ot.resource == it.resource and ot.mode == SHARED)
            if load + it.units > cap:
                return Block(it, None, "capacity",
                             f"{load}/{cap} units in use or queued ahead, "
                             f"{it.units} asked")
    return None


def decide(granted: Sequence[Claim], waiting: Sequence[Claim],
           capacity: dict[str, int]) -> list[Claim]:
    """The waiting claims to grant now, in ``seq`` order.

    Strict FIFO among conflicting claims: a claim waits behind every
    earlier waiting claim it conflicts with, even when that one is itself
    blocked, which is what keeps an exclusive request from starving.
    """
    held = list(granted)
    ahead: list[Claim] = []
    out: list[Claim] = []
    for w in sorted(waiting, key=lambda c: c.seq):
        if (blocker(w, held, capacity) is None
                and blocker(w, held + ahead, capacity) is None):
            out.append(w)
            held.append(w)
        else:
            ahead.append(w)
    return out


def why_waiting(claim: Claim, granted: Sequence[Claim],
                waiting: Sequence[Claim], capacity: dict[str, int]
                ) -> Optional[Block]:
    """What a waiting claim is waiting for: a holder first, else an
    earlier claim in the queue."""
    b = blocker(claim, granted, capacity)
    if b is not None:
        return b
    earlier = [w for w in waiting if w.seq < claim.seq]
    return blocker(claim, list(granted) + earlier, capacity, kind="queue")


def covers(held: Claim, wanted: Sequence[Item]) -> bool:
    """Does ``held`` already give everything ``wanted`` asks for (same
    resources, at least as strong a mode)?"""
    have = {i.resource: i for i in held.items}
    quiet_roots = {root(i.resource) for i in held.items if i.mode == QUIET}
    for w in wanted:
        if root(w.resource) in quiet_roots:
            continue                # the whole host is already ours
        h = have.get(w.resource)
        if h is None or _STRENGTH[h.mode] < _STRENGTH[w.mode]:
            return False
        if h.mode == SHARED and w.mode == SHARED and h.units < w.units:
            return False
    return True
