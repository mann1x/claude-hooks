#!/usr/bin/env python3
"""
Weekly token-usage breakdown for Claude Code.

Walks every transcript under ``~/.claude/projects/`` (or ``--projects-dir``),
pulls the ``message.usage`` block from every ``assistant`` entry, and
aggregates tokens by local day across the current billing week.

Claude Code's weekly rate limit resets at a fixed wall-clock time — by
default Friday 10:00 CEST (08:00 UTC). The script computes the current
week's window from the most recent reset up to now, then groups each
assistant message's usage into the local-timezone day it was produced.

Outputs a table: one row per day, columns for each token category
(input / output / cache-creation / cache-read), plus a per-day total
and a grand total. An optional ``--by-model`` flag pivots on model so
you can see Opus vs Sonnet vs Haiku separately (they have different
rate-limit weights).

Stdlib only — no dependencies, runs under any Python >= 3.9 that
Claude Code already depends on.

Usage
-----

    # Current week, default Friday 10:00 CEST reset, local tz
    python3 scripts/weekly_token_usage.py

    # JSON output for scripting
    python3 scripts/weekly_token_usage.py --json

    # Previous week (shift the window back one full week)
    python3 scripts/weekly_token_usage.py --week-offset -1

    # Different reset (US Pacific Tuesday midnight, for example)
    python3 scripts/weekly_token_usage.py --reset-tz UTC \\
        --reset-weekday tue --reset-hour 8

    # Break out Opus / Sonnet / Haiku separately
    python3 scripts/weekly_token_usage.py --by-model

    # Map day-by-day contribution onto Anthropic's reported weekly %
    # (read off the Claude Code UI — nothing local reports it)
    python3 scripts/weekly_token_usage.py --current-usage-pct 65

Notes
-----

- Claude Code's ``/cost`` slash command is session-scoped only and
  can't be queried non-interactively for a weekly total (``claude -p
  /cost`` prints "You are currently using your subscription"). The
  ``/status`` and ``/stats`` skills don't exist. The UI's "you are at
  X % of your weekly limit" number is computed server-side and is
  **not** written to any file under ``~/.claude/``. The only way to
  use it is to read it off the UI and pass ``--current-usage-pct X``.
- Anthropic does not publish the absolute token count of the weekly
  subscription limit for any plan. Only the reported percentage is
  ground truth — everything else is a ratio of observed tokens.
- This script reads transcripts directly, which is also what
  `ccusage <https://github.com/ryoppippi/ccusage>`_ does. For a USD
  cross-reference with model breakdown::

      npx -y ccusage@latest daily -z Europe/Berlin --since 20260410 --breakdown

  ccusage groups by calendar day only, so it can't honour a Friday
  10:00 CEST reset boundary — use this script for that.
- Tool-call count is the number of ``tool_use`` content blocks inside
  assistant messages, so it includes nested-tool chains.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

# ------------------------------------------------------------------ #
# Timezone helpers (stdlib zoneinfo on 3.9+, no third-party fallback)
# ------------------------------------------------------------------ #
try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore


DEFAULT_PROJECTS_DIR = Path.home() / ".claude" / "projects"

#: Claude Code's weekly reset is Friday 10:00 CEST by default
#: (10:00 UTC+2). Override via CLI flags if your plan resets elsewhere.
DEFAULT_RESET_WEEKDAY = 4      # 0 = Monday, 4 = Friday
DEFAULT_RESET_HOUR = 10        # 10:00
DEFAULT_RESET_MINUTE = 0
DEFAULT_RESET_TZ = "Europe/Berlin"  # CEST (summer) / CET (winter)

WEEKDAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _zone(name: str):
    if ZoneInfo is None:
        raise RuntimeError(
            "zoneinfo unavailable — requires Python 3.9+ or tzdata backport"
        )
    return ZoneInfo(name)


def _parse_weekday(s: str) -> int:
    s = s.strip().lower()[:3]
    if s in WEEKDAY_NAMES:
        return WEEKDAY_NAMES.index(s)
    try:
        n = int(s)
        if 0 <= n <= 6:
            return n
    except ValueError:
        pass
    raise argparse.ArgumentTypeError(
        f"weekday must be one of {WEEKDAY_NAMES} or 0-6 (got {s!r})"
    )


def most_recent_reset(
    now: datetime,
    *,
    weekday: int,
    hour: int,
    minute: int,
    tz_name: str,
    offset_weeks: int = 0,
) -> datetime:
    """Return the UTC timestamp of the most recent weekly reset.

    ``weekday`` uses Python's ``datetime.weekday()`` (Monday=0). ``tz_name``
    must be an IANA name understood by :mod:`zoneinfo`. ``offset_weeks``
    shifts the result by N whole weeks (negative = past, positive = future).
    """
    tz = _zone(tz_name)
    local_now = now.astimezone(tz)
    # Walk back from now in the local zone until we hit the target weekday
    # at the target hour/minute.
    today = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    delta_days = (local_now.weekday() - weekday) % 7
    candidate = today - timedelta(days=delta_days)
    if candidate > local_now:
        # Target weekday/time is later *today* — fall back to last week.
        candidate -= timedelta(days=7)
    candidate += timedelta(weeks=offset_weeks)
    return candidate.astimezone(timezone.utc)


# ------------------------------------------------------------------ #
# Transcript iteration
# ------------------------------------------------------------------ #
@dataclass
class UsageRecord:
    timestamp: datetime              # UTC
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    tool_call_count: int = 0         # number of tool_use blocks in this turn
    is_sidechain: bool = False       # True for subagent / Task-tool turns

    @property
    def total(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )


def _parse_ts(raw: str) -> Optional[datetime]:
    if not raw:
        return None
    # Python's fromisoformat accepts +00:00 but ``Z`` only on 3.11+.
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iter_usage_records(
    projects_dir: Path,
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> Iterable[UsageRecord]:
    """Yield one ``UsageRecord`` per assistant turn whose timestamp falls in
    ``[since, until)``. Silent about malformed lines and missing files.

    Claude Code replays assistant messages into every transcript that
    forks or resumes from the originating session — the same turn can
    appear in 10-30 files. We dedup using the same key ccusage uses:
    ``message.id + model + requestId`` (with a text-hash fallback when
    a transcript predates those fields). Missing dedup would inflate
    token counts by 2-3×.
    """
    if not projects_dir.exists():
        return
    seen_keys: set[str] = set()
    for path in projects_dir.rglob("*.jsonl"):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if entry.get("type") != "assistant":
                        continue
                    ts = _parse_ts(entry.get("timestamp") or "")
                    if ts is None:
                        continue
                    if since is not None and ts < since:
                        continue
                    if until is not None and ts >= until:
                        continue
                    msg = entry.get("message") or {}
                    usage = msg.get("usage") or {}
                    if not usage:
                        continue
                    # Dedup — see docstring above.
                    mid = msg.get("id") or ""
                    model = msg.get("model") or ""
                    rid = entry.get("requestId") or ""
                    if mid and rid:
                        dedup_key = f"{mid}|{model}|{rid}"
                    elif mid:
                        dedup_key = f"id:{mid}|{model}"
                    else:
                        # Fallback: timestamp + token counts (coarse but
                        # stable enough for transcripts without IDs).
                        dedup_key = (
                            f"{ts.isoformat()}|{model}|"
                            f"{usage.get('input_tokens')}|"
                            f"{usage.get('output_tokens')}"
                        )
                    if dedup_key in seen_keys:
                        continue
                    seen_keys.add(dedup_key)
                    # Count tool_use blocks inside this assistant message.
                    content = msg.get("content") or []
                    tool_count = 0
                    if isinstance(content, list):
                        for block in content:
                            if (
                                isinstance(block, dict)
                                and block.get("type") == "tool_use"
                            ):
                                tool_count += 1
                    yield UsageRecord(
                        timestamp=ts,
                        model=msg.get("model", "") or "",
                        input_tokens=int(usage.get("input_tokens", 0) or 0),
                        output_tokens=int(usage.get("output_tokens", 0) or 0),
                        cache_creation_input_tokens=int(
                            usage.get("cache_creation_input_tokens", 0) or 0
                        ),
                        cache_read_input_tokens=int(
                            usage.get("cache_read_input_tokens", 0) or 0
                        ),
                        tool_call_count=tool_count,
                        is_sidechain=bool(entry.get("isSidechain", False)),
                    )
        except OSError:
            continue


# ------------------------------------------------------------------ #
# Proxy JSONL log — P4 hand-off (read warmup-blocked savings)
# ------------------------------------------------------------------ #
@dataclass
class ProxyStats:
    warmups_blocked: int = 0
    warmups_passed_through: int = 0
    synthetic_rate_limits: int = 0
    total_requests: int = 0


def read_proxy_log(
    log_dir: Path,
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> ProxyStats:
    """Walk the proxy's daily JSONL files and tally warmup / synthetic
    stats within the given window. Returns zeros on missing dir /
    broken files — the script must never crash for lack of proxy data.
    """
    stats = ProxyStats()
    if not log_dir.exists():
        return stats
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
                    stats.total_requests += 1
                    if rec.get("warmup_blocked"):
                        stats.warmups_blocked += 1
                    elif rec.get("is_warmup"):
                        stats.warmups_passed_through += 1
                    if rec.get("synthetic"):
                        stats.synthetic_rate_limits += 1
        except OSError:
            continue
    return stats


# ------------------------------------------------------------------ #
# Aggregation
# ------------------------------------------------------------------ #
@dataclass
class DayBucket:
    date_str: str                   # YYYY-MM-DD in the display tz
    weekday_label: str              # Mon / Tue / ...
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    by_model: dict = field(default_factory=dict)
    message_count: int = 0
    tool_call_count: int = 0
    sidechain_input_tokens: int = 0
    sidechain_total_tokens: int = 0
    sidechain_message_count: int = 0

    @property
    def total(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )


def build_weekly_buckets(
    records: Iterable[UsageRecord],
    *,
    week_start_utc: datetime,
    week_end_utc: datetime,
    display_tz_name: str,
) -> list[DayBucket]:
    """Group records into one bucket per local-day within the week window."""
    display_tz = _zone(display_tz_name)
    start_local = week_start_utc.astimezone(display_tz)
    # Week has 7 days; first bucket is the start-day in the display tz.
    buckets: list[DayBucket] = []
    by_date: dict[str, DayBucket] = {}
    for i in range(7):
        day = start_local + timedelta(days=i)
        date_str = day.strftime("%Y-%m-%d")
        wd = WEEKDAY_LABELS[day.weekday()]
        b = DayBucket(date_str=date_str, weekday_label=wd)
        buckets.append(b)
        by_date[date_str] = b

    for r in records:
        local = r.timestamp.astimezone(display_tz)
        date_str = local.strftime("%Y-%m-%d")
        b = by_date.get(date_str)
        if b is None:
            # Outside the 7-day display window (shouldn't happen if
            # iter_usage_records is bounded to [since, until)).
            continue
        b.input_tokens += r.input_tokens
        b.output_tokens += r.output_tokens
        b.cache_creation_input_tokens += r.cache_creation_input_tokens
        b.cache_read_input_tokens += r.cache_read_input_tokens
        b.message_count += 1
        b.tool_call_count += r.tool_call_count
        if r.is_sidechain:
            b.sidechain_input_tokens += r.input_tokens
            b.sidechain_total_tokens += r.total
            b.sidechain_message_count += 1
        if r.model:
            mm = b.by_model.setdefault(r.model, {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "message_count": 0,
                "tool_call_count": 0,
            })
            mm["input_tokens"] += r.input_tokens
            mm["output_tokens"] += r.output_tokens
            mm["cache_creation_input_tokens"] += r.cache_creation_input_tokens
            mm["cache_read_input_tokens"] += r.cache_read_input_tokens
            mm["message_count"] += 1
            mm["tool_call_count"] += r.tool_call_count
    return buckets


# ------------------------------------------------------------------ #
# Rendering
# ------------------------------------------------------------------ #
def _fmt_int(n: int) -> str:
    return f"{n:,}"


def render_text(
    buckets: list[DayBucket],
    *,
    week_start_local: datetime,
    week_end_local: datetime,
    display_tz_name: str,
    show_by_model: bool,
    show_sidechain: bool = False,
    current_usage_pct: Optional[float] = None,
) -> str:
    lines: list[str] = []
    lines.append(
        f"Claude Code weekly usage — {display_tz_name}"
    )
    lines.append(
        f"window: {week_start_local.strftime('%Y-%m-%d %H:%M %Z')}"
        f"  →  {week_end_local.strftime('%Y-%m-%d %H:%M %Z')}"
    )
    lines.append("")

    week_total = sum(b.total for b in buckets)

    # Two percentage views:
    #   %Week   — each day's share of the week's own observed total (always
    #             computable, sums to 100).
    #   %Limit  — when the user passes --current-usage-pct, each day's
    #             share of the weekly *limit*, computed as
    #             (day.total / week_total) * current_usage_pct. This maps
    #             the relative split onto the one authoritative number
    #             Anthropic exposes — the percentage reported in the CLI.
    header_cols = (
        f"{'Day':<14}{'Input':>12}{'Output':>12}"
        f"{'CacheCr':>12}{'CacheRd':>14}{'Total':>14}"
        f"{'%Week':>8}"
    )
    if current_usage_pct is not None:
        header_cols += f"{'%Limit':>8}"
    header_cols += f"{'Msgs':>8}{'Tools':>8}"
    if show_sidechain:
        header_cols += f"{'Sub%Inp':>8}{'Subs':>6}"
    sep = "-" * len(header_cols)
    lines.append(header_cols)
    lines.append(sep)

    def _pct_of_week(n: int) -> str:
        if not week_total:
            return "   —"
        return f"{100.0 * n / week_total:7.2f}"

    def _pct_of_limit(n: int) -> str:
        if not week_total or current_usage_pct is None:
            return "   —"
        return f"{(100.0 * n / week_total) * (current_usage_pct / 100.0):7.2f}"

    tot = DayBucket(date_str="TOTAL", weekday_label="")
    for b in buckets:
        row = (
            f"{b.weekday_label} {b.date_str:<9}"
            f"{_fmt_int(b.input_tokens):>12}"
            f"{_fmt_int(b.output_tokens):>12}"
            f"{_fmt_int(b.cache_creation_input_tokens):>12}"
            f"{_fmt_int(b.cache_read_input_tokens):>14}"
            f"{_fmt_int(b.total):>14}"
            f"{_pct_of_week(b.total):>8}"
        )
        if current_usage_pct is not None:
            row += f"{_pct_of_limit(b.total):>8}"
        row += f"{b.message_count:>8}{b.tool_call_count:>8}"
        if show_sidechain:
            sub_pct = (
                f"{100.0 * b.sidechain_input_tokens / b.input_tokens:7.1f}"
                if b.input_tokens else "   —"
            )
            row += f"{sub_pct:>8}{b.sidechain_message_count:>6}"
        lines.append(row)
        tot.input_tokens += b.input_tokens
        tot.output_tokens += b.output_tokens
        tot.cache_creation_input_tokens += b.cache_creation_input_tokens
        tot.cache_read_input_tokens += b.cache_read_input_tokens
        tot.message_count += b.message_count
        tot.tool_call_count += b.tool_call_count
    lines.append(sep)
    tot_row = (
        f"{'TOTAL':<14}"
        f"{_fmt_int(tot.input_tokens):>12}"
        f"{_fmt_int(tot.output_tokens):>12}"
        f"{_fmt_int(tot.cache_creation_input_tokens):>12}"
        f"{_fmt_int(tot.cache_read_input_tokens):>14}"
        f"{_fmt_int(tot.total):>14}"
        f"{_pct_of_week(tot.total):>8}"
    )
    if current_usage_pct is not None:
        tot_row += f"{current_usage_pct:7.2f}"
    tot_row += f"{tot.message_count:>8}{tot.tool_call_count:>8}"
    if show_sidechain:
        tot_sub = sum(b.sidechain_input_tokens for b in buckets)
        tot_in = sum(b.input_tokens for b in buckets)
        tot_sub_msgs = sum(b.sidechain_message_count for b in buckets)
        sub_pct = f"{100.0 * tot_sub / tot_in:7.1f}" if tot_in else "   —"
        tot_row += f"{sub_pct:>8}{tot_sub_msgs:>6}"
    lines.append(tot_row)
    if current_usage_pct is not None:
        remaining = max(0.0, 100.0 - current_usage_pct)
        lines.append("")
        lines.append(
            f"Weekly limit (reported by Claude Code): used {current_usage_pct:.2f}%"
            f"  |  remaining {remaining:.2f}%"
        )
        lines.append(
            "  '%Limit' = each day's share of the total limit, derived as"
            " (day_tokens / week_tokens) × reported_pct."
        )

    if show_by_model:
        lines.append("")
        lines.append("Per-model breakdown")
        lines.append("-" * 40)
        for b in buckets:
            if not b.by_model:
                continue
            lines.append(f"{b.weekday_label} {b.date_str}:")
            for model, m in sorted(
                b.by_model.items(),
                key=lambda kv: -sum([
                    kv[1]["input_tokens"], kv[1]["output_tokens"],
                    kv[1]["cache_creation_input_tokens"],
                    kv[1]["cache_read_input_tokens"],
                ]),
            ):
                model_total = sum([
                    m["input_tokens"], m["output_tokens"],
                    m["cache_creation_input_tokens"],
                    m["cache_read_input_tokens"],
                ])
                lines.append(
                    f"  {model:<38} total={_fmt_int(model_total):>14}"
                    f"  msgs={m['message_count']}"
                )

    lines.append("")
    lines.append(
        "note: 'Total' sums all four categories. Rate-limit accounting "
        "for Claude plans typically weights cache-read lower than "
        "input/output/cache-create — see your plan's docs for exact "
        "weights."
    )
    return "\n".join(lines)


def render_json(
    buckets: list[DayBucket],
    *,
    week_start_utc: datetime,
    week_end_utc: datetime,
    display_tz_name: str,
    current_usage_pct: Optional[float] = None,
    proxy_stats: Optional["ProxyStats"] = None,
) -> str:
    week_total = sum(b.total for b in buckets)

    def _pct_of_week(n: int) -> Optional[float]:
        if not week_total:
            return None
        return round(100.0 * n / week_total, 4)

    def _pct_of_limit(n: int) -> Optional[float]:
        if not week_total or current_usage_pct is None:
            return None
        return round(
            (100.0 * n / week_total) * (current_usage_pct / 100.0), 4
        )

    payload = {
        "window": {
            "start_utc": week_start_utc.isoformat(),
            "end_utc": week_end_utc.isoformat(),
            "display_tz": display_tz_name,
        },
        "current_usage_pct": current_usage_pct,
        "days": [
            {
                "date": b.date_str,
                "weekday": b.weekday_label,
                "input_tokens": b.input_tokens,
                "output_tokens": b.output_tokens,
                "cache_creation_input_tokens": b.cache_creation_input_tokens,
                "cache_read_input_tokens": b.cache_read_input_tokens,
                "total": b.total,
                "percentage_of_week": _pct_of_week(b.total),
                "percentage_of_limit": _pct_of_limit(b.total),
                "message_count": b.message_count,
                "tool_call_count": b.tool_call_count,
                "sidechain_input_tokens": b.sidechain_input_tokens,
                "sidechain_total_tokens": b.sidechain_total_tokens,
                "sidechain_message_count": b.sidechain_message_count,
                "by_model": b.by_model,
            }
            for b in buckets
        ],
        "totals": {
            "input_tokens": sum(b.input_tokens for b in buckets),
            "output_tokens": sum(b.output_tokens for b in buckets),
            "cache_creation_input_tokens":
                sum(b.cache_creation_input_tokens for b in buckets),
            "cache_read_input_tokens":
                sum(b.cache_read_input_tokens for b in buckets),
            "total": sum(b.total for b in buckets),
            "message_count": sum(b.message_count for b in buckets),
            "tool_call_count": sum(b.tool_call_count for b in buckets),
        },
    }
    if proxy_stats is not None:
        payload["proxy"] = {
            "total_requests": proxy_stats.total_requests,
            "warmups_blocked": proxy_stats.warmups_blocked,
            "warmups_passed_through": proxy_stats.warmups_passed_through,
            "synthetic_rate_limits": proxy_stats.synthetic_rate_limits,
        }
    return json.dumps(payload, indent=2)


# ------------------------------------------------------------------ #
# CLI
# ------------------------------------------------------------------ #
def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--projects-dir", type=Path, default=DEFAULT_PROJECTS_DIR,
        help=f"Claude Code projects dir (default: {DEFAULT_PROJECTS_DIR})",
    )
    ap.add_argument(
        "--reset-weekday", type=_parse_weekday, default=DEFAULT_RESET_WEEKDAY,
        help="day of week when the weekly limit resets "
             f"(default: {WEEKDAY_NAMES[DEFAULT_RESET_WEEKDAY]})",
    )
    ap.add_argument(
        "--reset-hour", type=int, default=DEFAULT_RESET_HOUR,
        help=f"hour of day of reset, 0-23 (default: {DEFAULT_RESET_HOUR:02d})",
    )
    ap.add_argument(
        "--reset-minute", type=int, default=DEFAULT_RESET_MINUTE,
        help=f"minute of hour of reset (default: {DEFAULT_RESET_MINUTE:02d})",
    )
    ap.add_argument(
        "--reset-tz", default=DEFAULT_RESET_TZ,
        help=f"IANA timezone name for the reset clock "
             f"(default: {DEFAULT_RESET_TZ})",
    )
    ap.add_argument(
        "--display-tz", default=None,
        help="timezone for day-bucketing (default: same as --reset-tz)",
    )
    ap.add_argument(
        "--week-offset", type=int, default=0,
        help="shift the week window by N (negative=past, 0=current)",
    )
    ap.add_argument(
        "--by-model", action="store_true",
        help="include a per-model breakdown per day in the text output",
    )
    ap.add_argument(
        "--show-sidechain", action="store_true",
        help="add a Sidechain-% column that reports how much of each "
             "day's input came from subagent (Task-tool / sidechain) "
             "turns. Claude Code pre-warms every registered agent at "
             "session start, so heavy plugin use spikes this number.",
    )
    ap.add_argument(
        "--json", action="store_true",
        help="emit JSON instead of a human-readable table",
    )
    ap.add_argument(
        "--current-usage-pct", type=float, default=None, metavar="PCT",
        help="Anthropic's reported weekly-limit percentage (0-100). When "
             "set, adds a %%Limit column showing each day's share of the "
             "limit. Normally server-side only — but if you run the "
             "claude-hooks proxy (docs/proxy.md) it auto-populates this "
             "from the rate-limit headers and this flag becomes optional.",
    )
    ap.add_argument(
        "--proxy-state", type=Path, default=None, metavar="PATH",
        help="path to the proxy's ratelimit-state.json. Default looks "
             "under ~/.claude/claude-hooks-proxy/. Only used when "
             "--current-usage-pct is not provided.",
    )
    ap.add_argument(
        "--proxy-log-dir", type=Path, default=None, metavar="PATH",
        help="path to the proxy's JSONL log directory (for warmup-"
             "blocked / synthetic-rate-limit stats). Default looks "
             "under ~/.claude/claude-hooks-proxy/. Stats are printed "
             "as a footer line when any are present in the week.",
    )
    args = ap.parse_args(argv)

    display_tz_name = args.display_tz or args.reset_tz

    # P1 hand-off: if the user didn't pass --current-usage-pct, try to
    # pick it up from the proxy's rolling state file.
    current_usage_pct = args.current_usage_pct
    proxy_state_info: Optional[dict] = None
    if current_usage_pct is None:
        state_path = (
            args.proxy_state
            or Path.home() / ".claude" / "claude-hooks-proxy" / "ratelimit-state.json"
        )
        if state_path.exists():
            try:
                with open(state_path, "r", encoding="utf-8") as f:
                    proxy_state_info = json.load(f)
                claim = (proxy_state_info or {}).get("representative_claim")
                # Prefer the binding claim; fall back to 5h.
                if claim == "seven_day":
                    util = proxy_state_info.get("seven_day_utilization")
                else:
                    util = proxy_state_info.get("five_hour_utilization")
                if isinstance(util, (int, float)):
                    current_usage_pct = float(util) * 100.0
            except (OSError, json.JSONDecodeError):
                proxy_state_info = None

    now_utc = datetime.now(timezone.utc)
    week_start_utc = most_recent_reset(
        now_utc,
        weekday=args.reset_weekday,
        hour=args.reset_hour,
        minute=args.reset_minute,
        tz_name=args.reset_tz,
        offset_weeks=args.week_offset,
    )
    week_end_utc = week_start_utc + timedelta(days=7)

    records = iter_usage_records(
        args.projects_dir,
        since=week_start_utc,
        until=week_end_utc,
    )
    buckets = build_weekly_buckets(
        records,
        week_start_utc=week_start_utc,
        week_end_utc=week_end_utc,
        display_tz_name=display_tz_name,
    )

    # Proxy-log stats (warmup-blocked savings, synthetic RL detection).
    proxy_log_dir = (
        args.proxy_log_dir
        or Path.home() / ".claude" / "claude-hooks-proxy"
    )
    proxy_stats = read_proxy_log(
        proxy_log_dir, since=week_start_utc, until=week_end_utc,
    )

    if args.json:
        print(render_json(
            buckets,
            week_start_utc=week_start_utc,
            week_end_utc=week_end_utc,
            display_tz_name=display_tz_name,
            current_usage_pct=current_usage_pct,
            proxy_stats=proxy_stats,
        ))
    else:
        display_tz = _zone(display_tz_name)
        out = render_text(
            buckets,
            week_start_local=week_start_utc.astimezone(display_tz),
            week_end_local=week_end_utc.astimezone(display_tz),
            display_tz_name=display_tz_name,
            show_by_model=args.by_model,
            show_sidechain=args.show_sidechain,
            current_usage_pct=current_usage_pct,
        )
        if proxy_state_info and args.current_usage_pct is None:
            out += (
                f"\n\nLimit % auto-populated from claude-hooks proxy "
                f"(claim={proxy_state_info.get('representative_claim','?')}"
                f", updated={proxy_state_info.get('last_updated','?')})."
            )
        if proxy_stats.total_requests > 0:
            out += (
                f"\n\nProxy this week: {proxy_stats.total_requests} requests, "
                f"{proxy_stats.warmups_blocked} Warmup(s) BLOCKED, "
                f"{proxy_stats.warmups_passed_through} Warmup(s) passed, "
                f"{proxy_stats.synthetic_rate_limits} synthetic rate-limit(s)."
            )
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
