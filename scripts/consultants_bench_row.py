#!/usr/bin/env python3
"""Render one benchmark query's KPIs from its on-disk artifacts.

Reads ``<dir>/<slug>.metadata.json`` (token totals + retries) and
``<dir>/<slug>.transcript.db`` (per-role wall + per-LLM-call shape)
and emits structured output the benchmark runner injects into
``results.md``.

In v1.0 this script read ``<dir>/<slug>.trace.jsonl``; v1.1 replaced
the JSONL stream with the per-session SQLite ``transcript.db`` (see
``consultants/engine/recorder.py``). The benchmark runner now copies
``transcript.db`` into the label dir alongside the metadata.

Modes
-----

``--mode summary-row``
    One line of pipe-delimited fields suited for a markdown table::

        smoke | medium | completed | 212s | 11823 | 4892 | 8 | 11 | csl-...

    Columns: query | effort | status | wall_s | prompt_tok |
    completion_tok | llm_calls | tool_calls | sid

``--mode role-table``
    A multi-line markdown sub-table with per-role wall + tokens::

        ### smoke
        | Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
        |---|---|---|---|---|---|
        | planner     | 84.3s  | 1 | 237   | 1481  | 0 |
        | researcher  | 110.9s | 4 | 24909 | 1137  | 9 |
        | ...

This script is consumed by ``scripts/consultants_benchmark.sh`` and
also runnable manually for ad-hoc reports.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------- #

def _read_status(path: Path) -> dict[str, str]:
    """The benchmark runner writes a tiny KEY=value file per query."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _read_metadata(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _read_trace_db(path: Path) -> list[dict[str, Any]]:
    """Load events from a v1.1 transcript.db. Returns a list of
    dicts with the same keys the v1.0 JSONL reader produced
    (``event``, ``role``, ``duration_ms``, ``prompt_tokens``,
    ``completion_tokens``) so downstream aggregation logic is
    unchanged."""
    if not path.exists():
        return []
    try:
        conn = sqlite3.connect(
            f"file:{path}?mode=ro", uri=True, timeout=5.0,
        )
    except sqlite3.OperationalError:
        return []
    try:
        rows = conn.execute(
            "SELECT kind, role, duration_ms, prompt_tokens, "
            "completion_tokens FROM events ORDER BY ts"
        ).fetchall()
    except sqlite3.DatabaseError:
        return []
    finally:
        conn.close()

    events: list[dict] = []
    for kind, role, dt_ms, ptok, ctok in rows:
        events.append({
            # Map kinds to the v1.0 ``event`` field for compat.
            "event": kind,
            "role": role,
            "duration_ms": int(dt_ms or 0),
            "prompt_tokens": int(ptok or 0),
            "completion_tokens": int(ctok or 0),
        })
    return events


def _per_role(events: list[dict]) -> dict[str, dict[str, Any]]:
    """Aggregate trace events by role.

    Returns ``{role: {wall_ms, llm_count, tool_count, prompt_tok,
    completion_tok}}``. Wall is the sum of ``node_exit.duration_ms``
    so re-fired roles (e.g. researcher in fan-out) accumulate across
    invocations.
    """
    by_role: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "wall_ms": 0,
            "llm_count": 0,
            "tool_count": 0,
            "prompt_tok": 0,
            "completion_tok": 0,
        },
    )
    for ev in events:
        role = ev.get("role") or "unknown"
        kind = ev.get("event")
        if kind == "node_exit":
            by_role[role]["wall_ms"] += int(ev.get("duration_ms") or 0)
        elif kind == "llm_call":
            by_role[role]["llm_count"] += 1
            by_role[role]["prompt_tok"] += int(ev.get("prompt_tokens") or 0)
            by_role[role]["completion_tok"] += int(
                ev.get("completion_tokens") or 0)
        elif kind == "tool_call":
            by_role[role]["tool_count"] += 1
    return dict(by_role)


def _fmt_secs(ms: int) -> str:
    s = ms / 1000.0
    if s < 60:
        return f"{s:.1f}s"
    return f"{s / 60:.1f}m"


# ---------------------------------------------------------------- #
# Renderers
# ---------------------------------------------------------------- #

def render_summary_row(slug: str, dir_: Path) -> str:
    status = _read_status(dir_ / f"{slug}.status")
    metadata = _read_metadata(dir_ / f"{slug}.metadata.json")
    events = _read_trace_db(dir_ / f"{slug}.transcript.db")

    sid = status.get("sid", "-")
    effort = status.get("effort", "-")
    st = status.get("status", "-")
    wall = status.get("wall_s", "-")

    if st == "skipped":
        return f"| {slug} | – | skipped | – | – | – | – | – | – |"
    if not status:
        return f"| {slug} | – | (no run) | – | – | – | – | – | – |"

    pt = int(metadata.get("total_prompt_tokens") or 0)
    ct = int(metadata.get("total_completion_tokens") or 0)
    llm = sum(1 for e in events if e.get("event") == "llm_call")
    tools = sum(1 for e in events if e.get("event") == "tool_call")

    return (
        f"| {slug} | {effort} | {st} | {wall}s | "
        f"{pt} | {ct} | {llm} | {tools} | `{sid}` |"
    )


def render_role_table(slug: str, dir_: Path) -> str:
    events = _read_trace_db(dir_ / f"{slug}.transcript.db")
    if not events:
        return f"### {slug}\n\n(no trace)\n"

    per = _per_role(events)
    order = ["planner", "researcher", "critic", "synthesizer"]
    seen = set()
    rows: list[str] = []
    rows.append(f"### {slug}")
    rows.append("")
    rows.append(
        "| Role | Wall | LLM calls | Prompt tok | "
        "Completion tok | Tool calls |"
    )
    rows.append("|---|---|---|---|---|---|")
    for role in order + [r for r in per if r not in order]:
        if role in seen or role not in per:
            continue
        seen.add(role)
        v = per[role]
        rows.append(
            f"| {role} | {_fmt_secs(v['wall_ms'])} | "
            f"{v['llm_count']} | {v['prompt_tok']} | "
            f"{v['completion_tok']} | {v['tool_count']} |"
        )
    rows.append("")
    return "\n".join(rows)


# ---------------------------------------------------------------- #
# CLI
# ---------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("slug",
                    help="benchmark query slug (smoke / audit-medium / "
                         "audit-high)")
    ap.add_argument("dir", help="benchmark label directory")
    ap.add_argument("--mode", choices=("summary-row", "role-table"),
                    default="summary-row")
    args = ap.parse_args()

    d = Path(args.dir)
    if not d.is_dir():
        print(f"!!! {d} is not a directory", file=sys.stderr)
        return 2

    if args.mode == "summary-row":
        print(render_summary_row(args.slug, d))
    else:
        print(render_role_table(args.slug, d))
    return 0


if __name__ == "__main__":
    sys.exit(main())
