"""Text views of tasks, shared by the MCP tools, the CLI and the hooks.

One-line rows read at a glance (``bm-42 [active H] R9.gepo · title · 5d``);
the full view is the task file itself, which is already written to be
read.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable, Optional

from claude_hooks.tasks.model import parse_stamp, utcnow


def ago(stamp: str, now: Optional[datetime] = None) -> str:
    dt = parse_stamp(stamp or "")
    if dt is None:
        return ""
    s = max(0, int(((now or utcnow()) - dt).total_seconds()))
    if s < 3600:
        return f"{max(1, s // 60)}m"
    if s < 86400:
        return f"{s // 3600}h"
    if s < 86400 * 60:
        return f"{s // 86400}d"
    return f"{s // (86400 * 30)}mo"


def row_line(r: dict, *, project: bool = False,
             now: Optional[datetime] = None) -> str:
    flags = r.get("status", "")
    if r.get("priority") and r["priority"] != "M":
        flags += f" {r['priority']}"
    head = f"{r['id']} [{flags}]"
    if project and r.get("project"):
        head = f"{r['project']}:{head}"
    area = f"{r['area']} · " if r.get("area") else ""
    tail = []
    if r.get("open_depends"):
        tail.append("after " + ", ".join(r["open_depends"]))
    when = ago(r.get("updated") or "", now)
    if when:
        tail.append(when)
    return f"{head} {area}{r.get('title', '')}" + (
        " · " + " · ".join(tail) if tail else "")


def rows_text(rows: Iterable[dict], **kw) -> str:
    return "\n".join(row_line(r, **kw) for r in rows)


def counts_line(board: dict) -> str:
    parts = []
    for k in ("active", "ready", "waiting", "blocked"):
        if board.get(k):
            parts.append(f"{len(board[k])} {k}")
    return " · ".join(parts) or "nothing open"


def board_text(board: dict, project: str, *, per_section: int = 10,
               closed: int = 5) -> str:
    out = [f"Tasks — {project}: {counts_line(board)}"]
    for k in ("active", "ready", "waiting", "blocked"):
        rows = board.get(k) or []
        if not rows:
            continue
        out.append(f"\n{k.capitalize()} ({len(rows)}):")
        out.extend(f"  {row_line(r)}" for r in rows[:per_section])
        if len(rows) > per_section:
            out.append(f"  … {len(rows) - per_section} more")
    done = (board.get("closed") or [])[:closed]
    if done:
        out.append("\nRecently closed:")
        out.extend(f"  {row_line(r)}" for r in done)
    return "\n".join(out)
