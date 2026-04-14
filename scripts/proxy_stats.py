#!/usr/bin/env python3
"""
Ad-hoc summaries of claude-hooks proxy traffic.

Walks the proxy's daily JSONL files (default
``~/.claude/claude-hooks-proxy/``) and prints:

- totals for the requested window
- per-day breakdown with Warmup-blocked savings
- per-model request / token counts
- current rate-limit state from ``ratelimit-state.json``

Stdlib only. ``--json`` for structured output. Never reads network.

Usage:

    scripts/proxy_stats.py                 # last 7 days, default log dir
    scripts/proxy_stats.py --days 1        # today only
    scripts/proxy_stats.py --since 2026-04-10
    scripts/proxy_stats.py --log-dir /custom/path
    scripts/proxy_stats.py --json
    scripts/proxy_stats.py --by-model
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional


DEFAULT_LOG_DIR = Path.home() / ".claude" / "claude-hooks-proxy"


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
    return ts.astimezone(_dt.timezone.utc)


def _parse_date(s: str) -> _dt.datetime:
    """Accept ``YYYY-MM-DD`` or ``YYYYMMDD`` — return UTC midnight."""
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return _dt.datetime.strptime(s, fmt).replace(
                tzinfo=_dt.timezone.utc
            )
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"bad date: {s!r}")


# --------------------------------------------------------------- #
@dataclass
class DayStats:
    date: str
    requests: int = 0
    warmups_blocked: int = 0
    warmups_passed: int = 0
    synthetic: int = 0
    status_2xx: int = 0
    status_4xx: int = 0
    status_5xx: int = 0
    bytes_in: int = 0
    bytes_out: int = 0
    duration_ms_sum: int = 0
    by_model: dict = field(default_factory=lambda: defaultdict(int))
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0


def iter_log_records(
    log_dir: Path,
    *,
    since: Optional[_dt.datetime],
    until: Optional[_dt.datetime],
) -> Iterable[dict]:
    if not log_dir.exists():
        return
    for p in sorted(log_dir.glob("*.jsonl")):
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
                    if not isinstance(rec, dict):
                        continue
                    ts = _parse_ts(rec.get("ts") or "")
                    if ts is None:
                        continue
                    if since is not None and ts < since:
                        continue
                    if until is not None and ts >= until:
                        continue
                    yield rec
        except OSError:
            continue


def aggregate(records: Iterable[dict]) -> dict[str, DayStats]:
    out: dict[str, DayStats] = {}
    for rec in records:
        ts = _parse_ts(rec.get("ts") or "")
        if ts is None:
            continue
        day = ts.strftime("%Y-%m-%d")
        s = out.setdefault(day, DayStats(date=day))
        s.requests += 1
        status = int(rec.get("status") or 0)
        if 200 <= status < 300:
            s.status_2xx += 1
        elif 400 <= status < 500:
            s.status_4xx += 1
        elif 500 <= status < 600:
            s.status_5xx += 1
        if rec.get("warmup_blocked"):
            s.warmups_blocked += 1
        elif rec.get("is_warmup"):
            s.warmups_passed += 1
        if rec.get("synthetic"):
            s.synthetic += 1
        s.bytes_in += int(rec.get("req_bytes") or 0)
        s.bytes_out += int(rec.get("resp_bytes") or 0)
        s.duration_ms_sum += int(rec.get("duration_ms") or 0)
        model = rec.get("model_delivered") or rec.get("model_requested") or ""
        if model:
            s.by_model[model] += 1
        usage = rec.get("usage") or {}
        s.input_tokens += int(usage.get("input_tokens") or 0)
        s.output_tokens += int(usage.get("output_tokens") or 0)
        s.cache_read_tokens += int(usage.get("cache_read_input_tokens") or 0)
        s.cache_creation_tokens += int(
            usage.get("cache_creation_input_tokens") or 0
        )
    return out


def read_rate_limit_state(log_dir: Path) -> Optional[dict]:
    p = log_dir / "ratelimit-state.json"
    if not p.exists():
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


# --------------------------------------------------------------- #
def _fmt_int(n: int) -> str:
    return f"{n:,}"


def render_text(
    days: dict[str, DayStats],
    *,
    rate_limit_state: Optional[dict],
    show_by_model: bool,
) -> str:
    lines: list[str] = []
    lines.append("Proxy traffic summary")
    lines.append("=" * 40)
    if not days:
        lines.append("(no records in the requested window)")
    else:
        header = (
            f"{'Day':<12}{'Reqs':>8}{'Blk':>6}{'Pass':>6}{'Syn':>5}"
            f"{'2xx':>6}{'4xx':>6}{'5xx':>6}"
            f"{'Input':>11}{'Output':>11}{'CacheRd':>13}"
        )
        lines.append(header)
        lines.append("-" * len(header))
        grand = DayStats(date="TOTAL")
        for day in sorted(days):
            s = days[day]
            lines.append(
                f"{s.date:<12}{s.requests:>8}{s.warmups_blocked:>6}"
                f"{s.warmups_passed:>6}{s.synthetic:>5}"
                f"{s.status_2xx:>6}{s.status_4xx:>6}{s.status_5xx:>6}"
                f"{_fmt_int(s.input_tokens):>11}"
                f"{_fmt_int(s.output_tokens):>11}"
                f"{_fmt_int(s.cache_read_tokens):>13}"
            )
            grand.requests += s.requests
            grand.warmups_blocked += s.warmups_blocked
            grand.warmups_passed += s.warmups_passed
            grand.synthetic += s.synthetic
            grand.status_2xx += s.status_2xx
            grand.status_4xx += s.status_4xx
            grand.status_5xx += s.status_5xx
            grand.input_tokens += s.input_tokens
            grand.output_tokens += s.output_tokens
            grand.cache_read_tokens += s.cache_read_tokens
        lines.append("-" * len(header))
        lines.append(
            f"{'TOTAL':<12}{grand.requests:>8}{grand.warmups_blocked:>6}"
            f"{grand.warmups_passed:>6}{grand.synthetic:>5}"
            f"{grand.status_2xx:>6}{grand.status_4xx:>6}{grand.status_5xx:>6}"
            f"{_fmt_int(grand.input_tokens):>11}"
            f"{_fmt_int(grand.output_tokens):>11}"
            f"{_fmt_int(grand.cache_read_tokens):>13}"
        )

    if show_by_model:
        lines.append("")
        lines.append("Per-model request count")
        lines.append("-" * 40)
        tot = defaultdict(int)
        for s in days.values():
            for m, c in s.by_model.items():
                tot[m] += c
        if not tot:
            lines.append("(no model-tagged requests)")
        else:
            for m, c in sorted(tot.items(), key=lambda kv: -kv[1]):
                lines.append(f"  {c:>6}  {m}")

    if rate_limit_state:
        lines.append("")
        lines.append("Current rate-limit state (from the proxy)")
        lines.append("-" * 40)
        cl = rate_limit_state.get("representative_claim") or "?"
        lu = rate_limit_state.get("last_updated") or "?"
        lines.append(f"  binding window: {cl}")
        lines.append(f"  last update:    {lu}")
        five = rate_limit_state.get("five_hour_utilization")
        seven = rate_limit_state.get("seven_day_utilization")
        if isinstance(five, (int, float)):
            lines.append(f"  5h utilisation: {five*100:5.2f}%  "
                         f"(remaining {max(0.0,(1-five)*100):5.2f}%)")
        if isinstance(seven, (int, float)):
            lines.append(f"  7d utilisation: {seven*100:5.2f}%  "
                         f"(remaining {max(0.0,(1-seven)*100):5.2f}%)")
    return "\n".join(lines)


def render_json(days: dict[str, DayStats],
                rate_limit_state: Optional[dict]) -> str:
    out = {
        "days": [
            {
                "date": s.date,
                "requests": s.requests,
                "warmups_blocked": s.warmups_blocked,
                "warmups_passed": s.warmups_passed,
                "synthetic_rate_limits": s.synthetic,
                "status_2xx": s.status_2xx,
                "status_4xx": s.status_4xx,
                "status_5xx": s.status_5xx,
                "bytes_in": s.bytes_in,
                "bytes_out": s.bytes_out,
                "duration_ms_sum": s.duration_ms_sum,
                "input_tokens": s.input_tokens,
                "output_tokens": s.output_tokens,
                "cache_read_tokens": s.cache_read_tokens,
                "cache_creation_tokens": s.cache_creation_tokens,
                "by_model": dict(s.by_model),
            }
            for s in sorted(days.values(), key=lambda x: x.date)
        ],
        "rate_limit_state": rate_limit_state,
    }
    return json.dumps(out, indent=2)


# --------------------------------------------------------------- #
def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--log-dir", type=Path, default=DEFAULT_LOG_DIR,
        help=f"proxy log directory (default: {DEFAULT_LOG_DIR})",
    )
    ap.add_argument(
        "--days", type=int, default=7, metavar="N",
        help="include the last N days (default: 7). Overridden by --since.",
    )
    ap.add_argument(
        "--since", type=_parse_date, default=None,
        help="start date (inclusive), YYYY-MM-DD or YYYYMMDD.",
    )
    ap.add_argument(
        "--until", type=_parse_date, default=None,
        help="end date (exclusive), YYYY-MM-DD or YYYYMMDD.",
    )
    ap.add_argument(
        "--by-model", action="store_true",
        help="include per-model request counts.",
    )
    ap.add_argument("--json", action="store_true",
                    help="emit JSON instead of a table.")
    args = ap.parse_args(argv)

    now = _dt.datetime.now(_dt.timezone.utc)
    if args.since is None:
        since = now - _dt.timedelta(days=args.days)
    else:
        since = args.since
    until = args.until  # may be None (read to now)

    records = list(iter_log_records(args.log_dir, since=since, until=until))
    days = aggregate(records)
    rl_state = read_rate_limit_state(args.log_dir)

    if args.json:
        print(render_json(days, rl_state))
    else:
        print(render_text(days, rate_limit_state=rl_state,
                          show_by_model=args.by_model))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
