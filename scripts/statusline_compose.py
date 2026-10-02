#!/usr/bin/env python3
"""Pure-Python statusLine for Claude Code.

Reads Claude Code's status JSON on stdin and prints one line of the form:

    proj | ctx: 23% used | Opus 5.5 | 5h 19% · 7d 90% 🔴 | 📬 2

The usage segment is Claude Code's own ``rate_limits`` block (see
``scripts/statusline_usage.py``); the mail segment is this session's
unread mailbox count and waiting ack notes, shown only when there are
some. Set
``"refreshInterval"`` on the ``statusLine`` setting so mail that arrives
while the session is idle shows up without a prompt — the status line
costs no tokens, asking the model does.

Exit code is always 0; on any error, prints a minimal line so the
statusLine never breaks the UI.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Allow running from a checkout without installing the package.
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.statusline_usage import (  # noqa: E402
    _effective_format,
    add_legacy_flags,
    default_format,
    read_payload,
    usage_segment,
)


def compose(payload: dict, *, usage: str = "", mail: str = "") -> str:
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
    if usage:
        parts.append(usage)
    if mail:
        parts.append(mail)
    return " | ".join(parts)


def _mail(payload: dict, args) -> str:
    if args.no_mail:
        return ""
    from claude_hooks.statusline import mail_counts, mail_segment
    unread, acks = mail_counts(payload, ttl=args.mail_ttl) or (0, 0)
    return mail_segment(unread, acks=acks,
                        fmt=_effective_format(args.format))


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--format", choices=("emoji", "plain", "ascii"),
        default=default_format(),
        help="glyph style (default: emoji on Linux/macOS, ascii on "
             "Windows; override with CLAUDE_HOOKS_STATUSLINE_FORMAT)",
    )
    ap.add_argument("--no-mail", action="store_true",
                    help="omit the unread-mail segment")
    ap.add_argument("--mail-ttl", type=float, default=20.0,
                    help="seconds an unread count is reused (default 20)")
    ap.add_argument("--stale-seconds", type=int, default=None,
                    help=argparse.SUPPRESS)
    add_legacy_flags(ap)
    args = ap.parse_args(argv)

    payload = read_payload()
    try:
        usage = usage_segment(payload, fmt=args.format)
    except Exception:
        usage = ""
    try:
        mail = _mail(payload, args)
    except Exception:
        mail = ""
    try:
        sys.stdout.write(compose(payload, usage=usage, mail=mail))
    except Exception:
        sys.stdout.write("?")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
