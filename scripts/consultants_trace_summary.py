#!/usr/bin/env python3
"""Print a per-role waterfall summary from a consultants trace JSONL.

Usage::

    python scripts/consultants_trace_summary.py <sid>
    python scripts/consultants_trace_summary.py path/to/trace.jsonl
    python scripts/consultants_trace_summary.py --latest

Reads ``~/.claude/consultants-traces/<sid>.jsonl`` and groups events
by role, then by event type, printing:

    csl-...  total_wall=247.3s
      planner       12.4s   (1 LLM call,  1841 prompt tok,  412 completion)
      researcher  189.2s   (4 LLM calls, 8 tool calls)
        LLM calls (sorted by duration):
          iter 1  qwen3.5:cloud   42.1s  prompt=2415 completion=812 tool_calls=2
          ...
        Tool calls:
          read_file       0.3s   chars=4823
          ...
      critic        18.7s
      synthesizer   26.9s

The numbers come from the JSONL events written by
``consultants/engine/trace.py`` when ``CONSULTANTS_TRACE=1``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

DEFAULT_TRACE_DIR = Path(os.path.expanduser("~/.claude/consultants-traces"))


def _resolve_path(arg: str) -> Path:
    p = Path(arg)
    if p.exists():
        return p
    # Treat as sid
    candidate = DEFAULT_TRACE_DIR / f"{arg}.jsonl"
    if candidate.exists():
        return candidate
    raise SystemExit(f"trace not found: {arg} (also tried {candidate})")


def _latest() -> Path:
    if not DEFAULT_TRACE_DIR.exists():
        raise SystemExit(f"no trace dir at {DEFAULT_TRACE_DIR}")
    files = sorted(DEFAULT_TRACE_DIR.glob("*.jsonl"),
                   key=lambda p: p.stat().st_mtime)
    if not files:
        raise SystemExit(f"no traces found in {DEFAULT_TRACE_DIR}")
    return files[-1]


def _load(path: Path) -> list[dict[str, Any]]:
    events: list[dict] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def _fmt_dur_ms(ms: int) -> str:
    if ms < 1000:
        return f"{ms}ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f}s"
    return f"{ms / 60_000:.1f}m"


def summarize(events: list[dict]) -> None:
    if not events:
        print("(empty trace)")
        return

    sid = events[0].get("sid", "?")
    t_first = events[0]["ts"]
    t_last = events[-1]["ts"]
    total_s = t_last - t_first

    # Bucket by role.
    by_role: dict[str, dict[str, list[dict]]] = defaultdict(
        lambda: {"node": [], "llm": [], "tool": []})
    for e in events:
        ev = e.get("event")
        role = e.get("role") or "unknown"
        if ev == "node_exit":
            by_role[role]["node"].append(e)
        elif ev == "llm_call":
            by_role[role]["llm"].append(e)
        elif ev == "tool_call":
            by_role[role]["tool"].append(e)

    print(f"sid: {sid}")
    print(f"total wall: {total_s:.1f}s   "
          f"first_event=+0.0s  last_event=+{total_s:.1f}s")
    print()

    # Order: planner, researcher, critic, synthesizer, anything else.
    order = ["planner", "researcher", "critic", "synthesizer"]
    seen = set()
    role_keys = [r for r in order if r in by_role] + [
        r for r in by_role if r not in order]

    grand_node_ms = 0
    grand_llm_ms = 0
    grand_tool_ms = 0
    grand_prompt = 0
    grand_completion = 0

    for role in role_keys:
        if role in seen:
            continue
        seen.add(role)
        node_evts = by_role[role]["node"]
        llm_evts = by_role[role]["llm"]
        tool_evts = by_role[role]["tool"]

        node_ms = sum(int(e.get("duration_ms") or 0) for e in node_evts)
        llm_ms = sum(int(e.get("duration_ms") or 0) for e in llm_evts)
        tool_ms = sum(int(e.get("duration_ms") or 0) for e in tool_evts)
        prompt_tok = sum(int(e.get("prompt_tokens") or 0) for e in llm_evts)
        completion_tok = sum(
            int(e.get("completion_tokens") or 0) for e in llm_evts)

        grand_node_ms += node_ms
        grand_llm_ms += llm_ms
        grand_tool_ms += tool_ms
        grand_prompt += prompt_tok
        grand_completion += completion_tok

        n_node = len(node_evts)
        n_llm = len(llm_evts)
        n_tool = len(tool_evts)
        node_label = (f"{n_node}x" if n_node != 1 else "")
        print(f"  {role:<13} node={_fmt_dur_ms(node_ms):<7} "
              f"({node_label}entries) "
              f"llm={n_llm}x={_fmt_dur_ms(llm_ms):<7} "
              f"tool={n_tool}x={_fmt_dur_ms(tool_ms):<7} "
              f"tok=p{prompt_tok}/c{completion_tok}")

        # Top LLM calls
        if llm_evts:
            for e in sorted(llm_evts,
                            key=lambda x: -int(x.get("duration_ms") or 0)):
                idx = e.get("iter_index") or "-"
                model = (e.get("model") or "?").split("/")[-1]
                dur = _fmt_dur_ms(int(e.get("duration_ms") or 0))
                p = e.get("prompt_tokens") or 0
                c = e.get("completion_tokens") or 0
                tc = e.get("tool_calls") or 0
                err = e.get("error")
                tail = f"  [ERROR: {err}]" if err else ""
                print(f"      LLM iter={idx} model={model:<25} "
                      f"{dur:>7}  p={p:<6} c={c:<6} "
                      f"tool_calls={tc}{tail}")
        # Top tool calls
        if tool_evts:
            tool_summary: dict[str, dict] = defaultdict(
                lambda: {"count": 0, "ms": 0, "chars": 0})
            for e in tool_evts:
                t = e.get("tool") or "?"
                ts = tool_summary[t]
                ts["count"] += 1
                ts["ms"] += int(e.get("duration_ms") or 0)
                ts["chars"] += int(e.get("output_chars") or 0)
            for t, ts in sorted(tool_summary.items(),
                                key=lambda kv: -kv[1]["ms"]):
                print(f"      tool {t:<18} x{ts['count']:<3} "
                      f"{_fmt_dur_ms(ts['ms']):>7} "
                      f"chars={ts['chars']}")
        print()

    print("totals")
    print(f"  wall                  {total_s:.1f}s")
    print(f"  sum of node spans     {_fmt_dur_ms(grand_node_ms)}")
    print(f"  sum of LLM call time  {_fmt_dur_ms(grand_llm_ms)}  "
          f"({grand_llm_ms / 10 / max(total_s, 0.001):.1f}% of wall)")
    print(f"  sum of tool call time {_fmt_dur_ms(grand_tool_ms)}")
    print(f"  total prompt tokens   {grand_prompt}")
    print(f"  total completion tok  {grand_completion}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("trace", nargs="?",
                    help="sid or path to JSONL")
    ap.add_argument("--latest", action="store_true",
                    help="read the most recent trace in the default dir")
    args = ap.parse_args()

    if args.latest:
        path = _latest()
    elif args.trace:
        path = _resolve_path(args.trace)
    else:
        ap.print_help()
        return 2

    print(f"reading: {path}")
    print()
    events = _load(path)
    summarize(events)
    return 0


if __name__ == "__main__":
    sys.exit(main())
