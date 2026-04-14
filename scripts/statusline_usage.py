#!/usr/bin/env python3
"""
Compact statusline segment showing the live weekly rate-limit %.

Reads the claude-hooks proxy's rolling state file
(``~/.claude/claude-hooks-proxy/ratelimit-state.json`` by default) and
prints one short line suitable for embedding in a custom
``statusLine`` command.

Output shapes (no trailing newline):

- ``5h 42%``                        — only 5h window present
- ``5h 42% · 7d 18%``               — both windows present
- ``5h 65% ⚠``                      — ≥ 50% on the binding window
- ``5h 85% 🔴``                      — ≥ 80%
- ``(empty string)``                — no state file, stale, or unreadable

Exit codes are always 0 — the script must never break the statusline
callers. Unknown errors print an empty string.

Usage:

    python3 scripts/statusline_usage.py
    python3 scripts/statusline_usage.py --format plain
    python3 scripts/statusline_usage.py --state-file /custom/path.json
    python3 scripts/statusline_usage.py --stale-seconds 600
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from pathlib import Path
from typing import Optional


DEFAULT_STATE_PATH = Path.home() / ".claude" / "claude-hooks-proxy" / "ratelimit-state.json"
DEFAULT_STALE_SECONDS = 600   # 10 min — state older than this is treated as absent


def _parse_ts(raw: str) -> Optional[_dt.datetime]:
    if not raw:
        return None
    r = raw
    if r.endswith("Z"):
        r = r[:-1] + "+00:00"
    try:
        ts = _dt.datetime.fromisoformat(r)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=_dt.timezone.utc)
    return ts.astimezone(_dt.timezone.utc).replace(tzinfo=None)


def format_segment(
    state: dict,
    *,
    fmt: str = "emoji",
    stale_seconds: int = DEFAULT_STALE_SECONDS,
    now: Optional[_dt.datetime] = None,
) -> str:
    """Render one state dict into a compact segment. Returns "" on
    stale / empty / broken inputs.
    """
    if not state:
        return ""
    last = _parse_ts(state.get("last_updated") or "")
    if last is None:
        return ""
    now = now or _dt.datetime.utcnow()
    age = (now - last).total_seconds()
    if age > stale_seconds:
        return ""

    five = state.get("five_hour_utilization")
    seven = state.get("seven_day_utilization")
    claim = state.get("representative_claim") or "five_hour"

    def pct(v):
        return f"{v * 100:.0f}%"

    parts: list[str] = []
    if isinstance(five, (int, float)):
        parts.append(f"5h {pct(five)}")
    if isinstance(seven, (int, float)):
        parts.append(f"7d {pct(seven)}")
    if not parts:
        return ""
    base = " · ".join(parts)

    # Pick the binding window for the warning glyph.
    binding = five if claim == "five_hour" else seven
    glyph = ""
    if fmt != "plain" and isinstance(binding, (int, float)):
        if binding >= 0.80:
            glyph = " 🔴" if fmt == "emoji" else " !!"
        elif binding >= 0.50:
            glyph = " ⚠" if fmt == "emoji" else " !"

    return base + glyph


def read_state(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--state-file", type=Path, default=DEFAULT_STATE_PATH,
        help=f"path to ratelimit-state.json (default: {DEFAULT_STATE_PATH})",
    )
    ap.add_argument(
        "--format", choices=("emoji", "plain", "ascii"), default="emoji",
        help="glyph style for the warning indicator (default: emoji)",
    )
    ap.add_argument(
        "--stale-seconds", type=int, default=DEFAULT_STALE_SECONDS,
        help=f"treat state older than N seconds as absent "
             f"(default: {DEFAULT_STALE_SECONDS})",
    )
    try:
        args = ap.parse_args(argv)
        state = read_state(args.state_file)
        segment = format_segment(
            state, fmt=args.format, stale_seconds=args.stale_seconds,
        )
        if segment:
            sys.stdout.write(segment)
    except SystemExit:
        raise
    except Exception:
        # Last-ditch safety: never crash a statusline caller.
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
