#!/usr/bin/env python3
"""
Compact statusline segment showing the live 5h / 7d usage limits.

Reads the JSON Claude Code passes a ``statusLine`` command on stdin and
renders its ``rate_limits`` block. No proxy is involved: Claude Code
gets the same numbers from Anthropic's response headers and hands them
to the status line itself, fresh on every refresh. (Until v1.18 this
read the claude-hooks proxy's ``ratelimit-state.json``; that path went
blank whenever a session's traffic did not pass through the proxy.)

Output shapes (no trailing newline):

- ``5h 42%``                        — only 5h window present
- ``5h 42% · 7d 18%``               — both windows present
- ``5h 65% ⚠``                      — ≥ 50% on the binding window
- ``5h 85% 🔴``                      — ≥ 80%
- ``5h 20% · 7d 23% ⏰``             — Anthropic shoulder hours (US business)
- ``5h 20% · 7d 23% 🔥``             — Anthropic peak-of-peak hours (mid-afternoon ET)
- ``5h 65% 🔥 ⚠``                    — peak + utilization warning stack
- ``(empty string)``                — no ``rate_limits`` in the payload
  (API-key sessions, or before the first response)

Exit codes are always 0 — the script must never break the statusline
callers. Unknown errors print an empty string.

Usage (``scripts/statusline_compose.py`` builds the whole line):

    echo "$STATUS_JSON" | python3 scripts/statusline_usage.py
    echo "$STATUS_JSON" | python3 scripts/statusline_usage.py --format plain
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
from pathlib import Path
from typing import Optional, Tuple

# Allow running from a checkout without installing the package.
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

DEFAULT_STALE_SECONDS = 600   # 10 min — state older than this is treated as absent

#: Flags of the proxy-backed script (<= v1.17). Accepted and ignored, so
#: a statusLine command written for it keeps working.
LEGACY_VALUE_FLAGS = ("--state-file", "--remote-url", "--remote-timeout",
                      "--proxy-url", "--proxy-timeout")
LEGACY_SWITCH_FLAGS = ("--show-blocked",)

# Default Anthropic peak-hour windows (UTC, end-exclusive).
#
# Shoulder ⏰ : 13:00-22:00 UTC = 09:00-18:00 ET — North-American
# business hours, where edge load is consistently elevated.
# Peak-of-peak 🔥 : 17:00-21:00 UTC = 13:00-17:00 ET — mid-afternoon
# ET, the window where 502 storms most often clustered in our logs.
# Peak-of-peak applies to weekdays only; shoulder applies any day.
#
# Override per-installation:
#   CLAUDE_HOOKS_STATUSLINE_PEAK_HOURS_UTC="HH-HH"      (shoulder)
#   CLAUDE_HOOKS_STATUSLINE_PEAKPEAK_HOURS_UTC="HH-HH"  (peak-of-peak)
_DEFAULT_SHOULDER_HOURS = (13, 22)
_DEFAULT_PEAK_HOURS = (17, 21)


def _parse_hour_range(raw: Optional[str], default: Tuple[int, int]) -> Tuple[int, int]:
    """Parse ``"HH-HH"`` (UTC, end-exclusive). Returns ``default`` on
    any malformed input — statusline must never crash."""
    if not raw:
        return default
    try:
        a_s, b_s = raw.split("-", 1)
        a, b = int(a_s), int(b_s)
    except (ValueError, AttributeError):
        return default
    if 0 <= a < 24 and 0 < b <= 24 and a < b:
        return (a, b)
    return default


_VALID_FORMATS = frozenset({"emoji", "ascii", "plain"})

_TRUTHY_ENV = frozenset({"1", "true", "yes", "on"})


def _is_windows_console() -> bool:
    """Detect Windows-like consoles where emoji glyphs commonly tofu.

    Covers more than ``sys.platform == "win32"`` because the statusline
    is often launched from environments that hide native Windows behind
    a POSIX-shaped Python:

    - native Windows Python (``sys.platform == "win32"``, ``os.name == "nt"``)
    - Cygwin / msys2 Python (``sys.platform`` in ``"cygwin"`` / ``"msys"``)
    - Git Bash on Windows (Python sees POSIX but ``MSYSTEM`` is set)
    - any shell that exports ``OS=Windows_NT``

    WSL is intentionally NOT included — its Python runs on real Linux
    with a real Linux font stack and emoji renders fine in WSL terminals.
    """
    if sys.platform in ("win32", "cygwin", "msys"):
        return True
    if os.name == "nt":
        return True
    if os.environ.get("MSYSTEM"):
        return True
    if (os.environ.get("OS") or "").lower() == "windows_nt":
        return True
    return False


def _force_emoji_on_windows() -> bool:
    """Escape hatch — Windows Terminal + Cascadia Code renders emoji
    fine, so users on those setups can keep the rich glyphs by
    exporting ``CLAUDE_HOOKS_STATUSLINE_FORCE_EMOJI=1``.
    """
    return (os.environ.get("CLAUDE_HOOKS_STATUSLINE_FORCE_EMOJI") or "").strip().lower() in _TRUTHY_ENV


def _effective_format(fmt: str) -> str:
    """Honour caller's choice, but downgrade ``emoji`` → ``ascii`` on
    Windows-like consoles where the glyphs render as tofu boxes.

    This is a runtime safety net: even if the statusline command was
    wired with a hardcoded ``--format emoji`` (e.g. from older docs),
    the user still gets a readable line on cmd.exe / PowerShell /
    Git Bash. Set ``CLAUDE_HOOKS_STATUSLINE_FORCE_EMOJI=1`` to opt out.
    """
    if fmt == "emoji" and _is_windows_console() and not _force_emoji_on_windows():
        return "ascii"
    return fmt


def default_format() -> str:
    """Pick the safe default glyph style for the current host.

    Windows cmd.exe / legacy PowerShell consoles ship without emoji-
    capable fonts, so the warning / peak glyphs (``⏰`` ``🔥`` ``⚠``
    ``🔴``) render as tofu boxes. Default to ``"ascii"`` there to keep
    the statusline readable. Linux / macOS terminals (and Windows
    Terminal with Cascadia Code) handle emoji fine — keep the rich
    glyphs as default.

    Override with ``CLAUDE_HOOKS_STATUSLINE_FORMAT={emoji,ascii,plain}``
    for a Windows host that DOES render emoji (e.g. Windows Terminal),
    or to force ascii / plain on Linux for accessibility.
    """
    env = (os.environ.get("CLAUDE_HOOKS_STATUSLINE_FORMAT") or "").strip().lower()
    if env in _VALID_FORMATS:
        return env
    return "ascii" if _is_windows_console() else "emoji"


def peak_marker(
    now: Optional[_dt.datetime] = None,
    fmt: str = "emoji",
) -> str:
    """Return a peak-hour marker, or ``""`` outside peak windows.

    Two tiers (UTC, end-exclusive):

    - shoulder (any day, default 13:00-22:00) — ``⏰`` / ``[busy]``
    - peak-of-peak (weekdays only, default 17:00-21:00) — ``🔥`` / ``[peak]``

    ``fmt="plain"`` always returns ``""`` to match the existing
    utilization-glyph convention. ``fmt="ascii"`` returns bracketed
    text; ``fmt="emoji"`` returns the unicode glyph.
    """
    fmt = _effective_format(fmt)
    now = now or _dt.datetime.utcnow()
    shoulder = _parse_hour_range(
        os.environ.get("CLAUDE_HOOKS_STATUSLINE_PEAK_HOURS_UTC"),
        _DEFAULT_SHOULDER_HOURS,
    )
    peak = _parse_hour_range(
        os.environ.get("CLAUDE_HOOKS_STATUSLINE_PEAKPEAK_HOURS_UTC"),
        _DEFAULT_PEAK_HOURS,
    )
    h = now.hour
    is_weekday = now.weekday() < 5
    if is_weekday and peak[0] <= h < peak[1]:
        if fmt == "emoji":
            return "🔥"
        if fmt == "ascii":
            return "[peak]"
        return ""
    if shoulder[0] <= h < shoulder[1]:
        if fmt == "emoji":
            return "⏰"
        if fmt == "ascii":
            return "[busy]"
        return ""
    return ""


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

    ``state`` has the shape :func:`claude_hooks.statusline.native_state`
    builds: 0-1 utilizations, ``representative_claim``, ``last_updated``.
    """
    fmt = _effective_format(fmt)
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

    # Peak-hour marker comes BEFORE the utilization warning so the
    # contextual ("we're in the busy window") sign reads first and
    # the dynamic ("you're at 65%") sign reads second when both fire.
    peak = peak_marker(now=now, fmt=fmt)
    peak_part = (" " + peak) if peak else ""

    return base + peak_part + glyph


def add_legacy_flags(ap: argparse.ArgumentParser) -> None:
    """Swallow the proxy-era flags without advertising them."""
    for flag in LEGACY_VALUE_FLAGS:
        ap.add_argument(flag, default=None, help=argparse.SUPPRESS)
    for flag in LEGACY_SWITCH_FLAGS:
        ap.add_argument(flag, action="store_true", help=argparse.SUPPRESS)


def read_payload(stream=None) -> dict:
    """Claude Code's status JSON from stdin, or {} — never an exception."""
    try:
        raw = (stream or sys.stdin).read()
        data = json.loads(raw) if raw.strip() else {}
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def usage_segment(payload: dict, *, fmt: str,
                  now: Optional[_dt.datetime] = None) -> str:
    from claude_hooks.statusline import native_state
    return format_segment(native_state(payload, now=now), fmt=fmt, now=now)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument(
        "--format", choices=("emoji", "plain", "ascii"),
        default=default_format(),
        help="glyph style for the warning indicator "
             "(default: emoji on Linux/macOS, ascii on Windows; "
             "override with CLAUDE_HOOKS_STATUSLINE_FORMAT)",
    )
    ap.add_argument("--stale-seconds", type=int, default=None,
                    help=argparse.SUPPRESS)
    add_legacy_flags(ap)
    try:
        args = ap.parse_args(argv)
        segment = usage_segment(read_payload(), fmt=args.format)
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
