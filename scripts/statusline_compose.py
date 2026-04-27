#!/usr/bin/env python3
"""Pure-Python statusLine for Claude Code.

Reads Claude Code's status JSON on stdin and prints one line of the form:

    proj | ctx: 23% used | claude-sonnet-4-6 | 7d 2%

Useful where the bash + jq combo isn't available (e.g. Windows / msys2).
The proxy-utilization segment comes from a remote dashboard endpoint
(``--proxy-url``); when omitted, falls back to a local state file.

Exit code is always 0; on any error, prints a minimal line so the
statusLine never breaks the UI.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Allow running from a checkout without installing the package.
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.statusline_usage import (  # noqa: E402
    DEFAULT_REMOTE_TIMEOUT,
    DEFAULT_STALE_SECONDS,
    DEFAULT_STATE_PATH,
    default_format,
    format_segment,
    read_state,
    read_state_remote,
)


def compose(payload: dict, *, proxy_segment: str) -> str:
    cwd = (payload.get("workspace") or {}).get("current_dir") or payload.get("cwd") or ""
    model = (payload.get("model") or {}).get("display_name") or ""
    ctx = (payload.get("context_window") or {}).get("used_percentage")

    parts: list[str] = []
    proj = os.path.basename(cwd.rstrip("/\\")) or "/"
    parts.append(proj)
    if isinstance(ctx, (int, float)):
        parts.append(f"ctx: {int(ctx)}% used")
    if model:
        parts.append(model)
    if proxy_segment:
        parts.append(proxy_segment)
    return " | ".join(parts)


def _resolve_proxy_segment(args) -> str:
    if args.proxy_url:
        state = read_state_remote(args.proxy_url, timeout=args.proxy_timeout)
    else:
        state = read_state(args.state_file)
    return format_segment(state, fmt=args.format, stale_seconds=args.stale_seconds)


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--proxy-url", default=None,
                    help="dashboard endpoint, e.g. http://solidpc:38081/api/ratelimit.json")
    ap.add_argument("--proxy-timeout", type=float, default=DEFAULT_REMOTE_TIMEOUT)
    ap.add_argument("--state-file", type=Path, default=DEFAULT_STATE_PATH)
    ap.add_argument("--stale-seconds", type=int, default=DEFAULT_STALE_SECONDS)
    ap.add_argument(
        "--format", choices=("emoji", "plain", "ascii"),
        default=default_format(),
        help="glyph style (default: emoji on Linux/macOS, ascii on "
             "Windows; override with CLAUDE_HOOKS_STATUSLINE_FORMAT)",
    )
    args = ap.parse_args(argv)

    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
    except (OSError, json.JSONDecodeError):
        payload = {}

    try:
        seg = _resolve_proxy_segment(args)
    except Exception:
        seg = ""

    try:
        sys.stdout.write(compose(payload, proxy_segment=seg))
    except Exception:
        sys.stdout.write("?")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
