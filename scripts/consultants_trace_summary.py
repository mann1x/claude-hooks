#!/usr/bin/env python3
"""Print a per-role waterfall summary from a consultants transcript.db.

Usage::

    python scripts/consultants_trace_summary.py <sid> [--cwd PATH]
    python scripts/consultants_trace_summary.py path/to/transcript.db
    python scripts/consultants_trace_summary.py --latest [--cwd PATH]

v1.1 reads the per-session SQLite transcript.db at
``<cwd>/.claude-hooks/consultants/<sid>/transcript.db`` (the artifact
written by ``consultants/engine/recorder.py``). The legacy v1.0 JSONL
trace at ``~/.claude/consultants-traces/<sid>.jsonl`` is no longer
written.

Output format matches the v1.0 layout so existing tooling / muscle
memory carries over:

    sid: csl-...
    total wall: 247.3s

      planner       node=12.4s   (1x entries) llm=1x=12.4s tool=0x=0ms tok=p1841/c412
      researcher    node=189.2s  (3x entries) llm=12x=170.0s tool=8x=2.1s tok=p21k/c4.8k
        LLM iter=1 model=qwen3.5:cloud           42.1s  p=2415  c=812    tool_calls=2
        ...
        tool read_file        x4    1.8s chars=12483
        ...
      synthesizer   node=26.9s   (1x entries) llm=1x=26.9s tool=0x=0ms tok=p3210/c1024

    totals
      wall                  247.3s
      sum of node spans     228.5s
      sum of LLM call time  208.6s  (84.4% of wall)
      sum of tool call time 2.1s
      total prompt tokens   28471
      total completion tok  6824
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional


_LEGACY_TRACE_DIR = Path(os.path.expanduser("~/.claude/consultants-traces"))
_DEFAULT_PROJECT_DIR = Path.cwd()
_DB_FILENAME = "transcript.db"


def _resolve_path(arg: str, cwd: Path) -> Path:
    """Resolve the user's argument to a transcript.db path.

    Order:
      1. If arg is an existing file, use it directly.
      2. If arg looks like an sid (csl-...), look under
         ``<cwd>/.claude-hooks/consultants/<arg>/transcript.db``.
      3. Legacy fallback: if the user pointed at an old JSONL, error
         out clearly with the v1.1 migration tip.
    """
    p = Path(arg)
    if p.is_file():
        if p.suffix == ".jsonl":
            raise SystemExit(
                f"v1.1 reads transcript.db (SQLite); the JSONL trace at "
                f"{p} is from v1.0 and is no longer produced. To "
                f"summarize a v1.0 trace, downgrade or convert "
                f"manually."
            )
        return p
    candidate = cwd / ".claude-hooks" / "consultants" / arg / _DB_FILENAME
    if candidate.is_file():
        return candidate
    raise SystemExit(
        f"transcript.db not found for sid={arg!r} under {cwd} "
        f"(tried {candidate}). Pass --cwd PATH or a direct .db path."
    )


def _latest(cwd: Path) -> Path:
    """Return the most recently modified transcript.db under
    ``<cwd>/.claude-hooks/consultants/``."""
    root = cwd / ".claude-hooks" / "consultants"
    if not root.is_dir():
        raise SystemExit(f"no consultants dir under {cwd} ({root})")
    candidates = list(root.glob(f"*/{_DB_FILENAME}"))
    if not candidates:
        raise SystemExit(f"no {_DB_FILENAME} files under {root}")
    candidates.sort(key=lambda p: p.stat().st_mtime)
    return candidates[-1]


def _fmt_dur_ms(ms: int) -> str:
    if ms < 1000:
        return f"{ms}ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f}s"
    return f"{ms / 60_000:.1f}m"


def _open_ro(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(
        f"file:{db_path}?mode=ro", uri=True, timeout=5.0,
    )


def summarize(db_path: Path) -> None:
    conn = _open_ro(db_path)
    try:
        meta = conn.execute(
            "SELECT sid, started_at, finished_at, status, error "
            "FROM meta"
        ).fetchone()
    except sqlite3.DatabaseError as e:
        raise SystemExit(f"not a valid transcript.db ({db_path}): {e}")
    if meta is None:
        print("(empty meta — was the consultation finalized?)")
        return
    sid, started_at, finished_at, status, err = meta
    total_s = (
        (finished_at - started_at) if (finished_at and started_at) else 0.0
    )

    print(f"sid: {sid}")
    print(f"status: {status}" + (f"  error: {err}" if err else ""))
    print(f"total wall: {total_s:.1f}s")
    print()

    # Aggregations by role.
    rows = conn.execute(
        "SELECT role, kind, duration_ms, prompt_tokens, completion_tokens, "
        "model, tool, output_chars, error, round, lane_idx "
        "FROM events ORDER BY ts"
    ).fetchall()
    conn.close()

    by_role: dict[str, dict[str, list[dict]]] = defaultdict(
        lambda: {"node_exit": [], "llm_call": [], "tool_call": []}
    )
    for (role, kind, dt_ms, ptok, ctok, model, tool, ochars,
         err_, rnd, lane) in rows:
        if kind not in ("node_exit", "llm_call", "tool_call"):
            continue  # skip node_enter; node_exit carries duration
        by_role[role or "unknown"][kind].append({
            "duration_ms": int(dt_ms or 0),
            "prompt_tokens": int(ptok or 0),
            "completion_tokens": int(ctok or 0),
            "model": model,
            "tool": tool,
            "output_chars": int(ochars or 0),
            "error": err_,
            "round": rnd,
            "lane_idx": lane,
        })

    order = ["planner", "researcher", "critic", "synthesizer"]
    role_keys = [r for r in order if r in by_role] + [
        r for r in by_role if r not in order
    ]

    grand_node_ms = 0
    grand_llm_ms = 0
    grand_tool_ms = 0
    grand_prompt = 0
    grand_completion = 0

    for role in role_keys:
        node_evts = by_role[role]["node_exit"]
        llm_evts = by_role[role]["llm_call"]
        tool_evts = by_role[role]["tool_call"]

        node_ms = sum(e["duration_ms"] for e in node_evts)
        llm_ms = sum(e["duration_ms"] for e in llm_evts)
        tool_ms = sum(e["duration_ms"] for e in tool_evts)
        prompt_tok = sum(e["prompt_tokens"] for e in llm_evts)
        completion_tok = sum(e["completion_tokens"] for e in llm_evts)

        grand_node_ms += node_ms
        grand_llm_ms += llm_ms
        grand_tool_ms += tool_ms
        grand_prompt += prompt_tok
        grand_completion += completion_tok

        n_node = len(node_evts)
        n_llm = len(llm_evts)
        n_tool = len(tool_evts)
        node_label = f"{n_node}x" if n_node != 1 else ""
        print(f"  {role:<13} node={_fmt_dur_ms(node_ms):<7} "
              f"({node_label}entries) "
              f"llm={n_llm}x={_fmt_dur_ms(llm_ms):<7} "
              f"tool={n_tool}x={_fmt_dur_ms(tool_ms):<7} "
              f"tok=p{prompt_tok}/c{completion_tok}")

        if llm_evts:
            for i, e in enumerate(
                    sorted(llm_evts, key=lambda x: -x["duration_ms"]),
                    start=1):
                lane = e.get("lane_idx")
                lane_str = f" lane={lane}" if lane is not None else ""
                model = (e.get("model") or "?").split("/")[-1]
                dur = _fmt_dur_ms(e["duration_ms"])
                p = e["prompt_tokens"]
                c = e["completion_tokens"]
                err_str = (f"  [ERROR: {e['error']}]"
                           if e.get("error") else "")
                print(f"      LLM round={e.get('round') or '-'}{lane_str} "
                      f"model={model:<25} {dur:>7}  "
                      f"p={p:<6} c={c:<6}{err_str}")
        if tool_evts:
            tool_summary: dict[str, dict] = defaultdict(
                lambda: {"count": 0, "ms": 0, "chars": 0, "errs": 0}
            )
            for e in tool_evts:
                t = e["tool"] or "?"
                ts = tool_summary[t]
                ts["count"] += 1
                ts["ms"] += e["duration_ms"]
                ts["chars"] += e["output_chars"]
                if e.get("error"):
                    ts["errs"] += 1
            for t, ts in sorted(tool_summary.items(),
                                key=lambda kv: -kv[1]["ms"]):
                err_str = f"  errs={ts['errs']}" if ts["errs"] else ""
                print(f"      tool {t:<18} x{ts['count']:<3} "
                      f"{_fmt_dur_ms(ts['ms']):>7} "
                      f"chars={ts['chars']}{err_str}")
        print()

    print("totals")
    print(f"  wall                  {total_s:.1f}s")
    print(f"  sum of node spans     {_fmt_dur_ms(grand_node_ms)}")
    pct = (grand_llm_ms / 10 / max(total_s, 0.001)) if total_s else 0.0
    print(f"  sum of LLM call time  {_fmt_dur_ms(grand_llm_ms)}  "
          f"({pct:.1f}% of wall)")
    print(f"  sum of tool call time {_fmt_dur_ms(grand_tool_ms)}")
    print(f"  total prompt tokens   {grand_prompt}")
    print(f"  total completion tok  {grand_completion}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("trace", nargs="?",
                    help="sid or path to transcript.db")
    ap.add_argument("--latest", action="store_true",
                    help="read the most recent transcript.db under "
                         "<cwd>/.claude-hooks/consultants/")
    ap.add_argument("--cwd", default=str(_DEFAULT_PROJECT_DIR),
                    help="Project root for sid resolution "
                         "(default: current directory).")
    args = ap.parse_args()

    cwd = Path(os.path.expanduser(args.cwd))

    # Politely flag the legacy dir if it's still around — folks
    # hitting this script after upgrading should know why their old
    # files aren't being read.
    if _LEGACY_TRACE_DIR.exists() and any(
            _LEGACY_TRACE_DIR.glob("*.jsonl")):
        print(
            f"NOTE: v1.0 traces still present in {_LEGACY_TRACE_DIR}; "
            f"v1.1 reads transcript.db instead. Old files are safe "
            f"to delete.\n",
            file=sys.stderr,
        )

    if args.latest:
        path = _latest(cwd)
    elif args.trace:
        path = _resolve_path(args.trace, cwd)
    else:
        ap.print_help()
        return 2

    print(f"reading: {path}")
    print()
    summarize(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
