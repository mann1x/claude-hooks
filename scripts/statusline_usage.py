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
    python3 scripts/statusline_usage.py --remote-url http://host:38081/api/ratelimit.json
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional


DEFAULT_STATE_PATH = Path.home() / ".claude" / "claude-hooks-proxy" / "ratelimit-state.json"
DEFAULT_STALE_SECONDS = 600   # 10 min — state older than this is treated as absent
DEFAULT_REMOTE_TIMEOUT = 2.0  # seconds — keep tight; statusline runs often


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
    blocked_today: Optional[int] = None,
) -> str:
    """Render one state dict into a compact segment. Returns "" on
    stale / empty / broken inputs.

    ``blocked_today`` is an optional count of Warmups blocked today
    (passed in from the proxy's JSONL log). When > 0, appends a
    compact ``· blk=N`` segment so the savings are visible inline.
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
    if blocked_today and blocked_today > 0:
        parts.append(f"blk={blocked_today}")
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


def count_blocked_today(log_dir: Path) -> int:
    """Count ``warmup_blocked: true`` entries in today's proxy log.

    Returns 0 on any error — the statusline caller must never crash.
    """
    today = _dt.datetime.utcnow().strftime("%Y-%m-%d")
    p = log_dir / f"{today}.jsonl"
    if not p.exists():
        return 0
    n = 0
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get("warmup_blocked"):
                    n += 1
    except OSError:
        pass
    return n


def read_state(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def read_state_remote(url: str, timeout: float = DEFAULT_REMOTE_TIMEOUT) -> dict:
    """Fetch state JSON from the proxy dashboard's /api/ratelimit.json.

    The endpoint wraps the state under ``{"state": {...}, "burn": ...}`` —
    we unwrap to match the local-file shape. Returns {} on any failure;
    statusline callers must never see an exception.
    """
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    state = data.get("state")
    if isinstance(state, dict):
        return state
    return data


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
    ap.add_argument(
        "--show-blocked", action="store_true",
        help="append ' · blk=N' when today's proxy log has N Warmup-"
             "blocked events. Reads the same directory as --state-file. "
             "Disabled automatically when --remote-url is used.",
    )
    ap.add_argument(
        "--remote-url", default=None,
        help="fetch state from a proxy dashboard endpoint instead of a "
             "local file (e.g. http://solidpc:38081/api/ratelimit.json)",
    )
    ap.add_argument(
        "--remote-timeout", type=float, default=DEFAULT_REMOTE_TIMEOUT,
        help=f"timeout in seconds for --remote-url "
             f"(default: {DEFAULT_REMOTE_TIMEOUT})",
    )
    try:
        args = ap.parse_args(argv)
        if args.remote_url:
            state = read_state_remote(args.remote_url, timeout=args.remote_timeout)
            blocked = None
        else:
            state = read_state(args.state_file)
            blocked = None
            if args.show_blocked:
                blocked = count_blocked_today(args.state_file.parent)
        segment = format_segment(
            state, fmt=args.format, stale_seconds=args.stale_seconds,
            blocked_today=blocked,
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
