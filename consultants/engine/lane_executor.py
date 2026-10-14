"""Give the shared tool executor a lane identity.

One executor serves every lane of a council: its signature is
``(name, raw_args, cwd) -> str`` and it cannot tell which lane is
calling. Anything that must be per lane — the read cache's invalidation
by a lane-isolated effect today, the shell sandbox's lane views later —
needs that identity, so the graph's node wrappers bind it here.

The bare signature stays for every other caller: ``LaneBoundExecutor``
is itself a ``(name, raw_args, cwd) -> str`` callable, and it forwards
the lane only to an executor that declares it accepts one
(``accepts_lane = True``). A plain function — a test fake, the coder's
sandbox executor — is called exactly as before.
"""
from __future__ import annotations

from typing import Any, Callable, Optional


class LaneBoundExecutor:
    """``inner`` with ``lane`` (and the session id) attached."""

    accepts_lane = False  # it consumes the lane; it does not take one

    def __init__(self, inner: Callable[..., str], lane: str, sid: str = ""):
        self.inner = inner
        self.lane = lane
        self.sid = sid

    def __call__(self, name: str, raw_args: str, cwd: str, **kw: Any) -> str:
        if getattr(self.inner, "accepts_lane", False):
            kw.setdefault("lane", self.lane)
        return self.inner(name, raw_args, cwd, **kw)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"LaneBoundExecutor(lane={self.lane!r}, sid={self.sid!r})"


def researcher_lane_id(state: dict) -> str:
    """``researcher:<lane_idx>``, or ``researcher:solo`` on the
    single-researcher path (base tiers, critic reroutes)."""
    idx = state.get("lane_idx")
    return f"researcher:{idx}" if idx is not None else "researcher:solo"


def tool_executor_lane_id(state: dict) -> str:
    """One id per tool-plan item: the researcher lane it serves plus the
    item's position, so two items of one plan are distinct lanes."""
    item = state.get("tool_plan_item")
    parent: Optional[Any] = state.get("parent_lane_idx")
    pos: Optional[Any] = None
    if item is not None:
        parent = getattr(item, "parent_lane_idx", parent)
        pos = getattr(item, "lane_idx", None)
    return (f"tool_executor:{'solo' if parent is None else parent}"
            f":{'0' if pos is None else pos}")


def bind_lane(executor: Optional[Callable[..., str]], lane: str,
              sid: str = "") -> Optional[Callable[..., str]]:
    """Wrap ``executor`` for ``lane``; ``None`` stays ``None``."""
    if executor is None:
        return None
    return LaneBoundExecutor(executor, lane, sid)


__all__ = [
    "LaneBoundExecutor", "bind_lane",
    "researcher_lane_id", "tool_executor_lane_id",
]
