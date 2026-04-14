#!/usr/bin/env python3
"""
One-screen status of the claude-hooks stack.

Aggregates the three existing sources into a single, dashboard-ish
view so you can tell at a glance whether the stack is healthy and
whether the proxy is actually earning its keep:

- proxy service — PID / uptime / listen address
  (systemd on Linux, best-effort process scan elsewhere)
- current rate-limit state (binding window + %) from
  ratelimit-state.json
- today's proxy-log tallies (requests / Warmups blocked / synthetic)
- this week's transcript totals (Fri-10:00-CEST anchored)

Stdlib only. No flags required — prints a compact block to stdout
and exits 0. Pass ``--json`` for machine-readable output.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional


REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
DEFAULT_LOG_DIR = Path.home() / ".claude" / "claude-hooks-proxy"


# ---------------------------------------------------------------- #
def _read_state(log_dir: Path) -> Optional[dict]:
    p = log_dir / "ratelimit-state.json"
    if not p.exists():
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _systemd_status(unit: str = "claude-hooks-proxy") -> dict:
    """Return a dict like ``{'active': bool, 'since': str, 'pid': int|None}``."""
    out = {"active": False, "since": None, "pid": None, "available": False}
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return out
    out["available"] = True
    try:
        r = subprocess.run(
            [systemctl, "show", unit,
             "--property=ActiveState,ActiveEnterTimestamp,MainPID"],
            capture_output=True, text=True, timeout=3,
        )
        for line in r.stdout.splitlines():
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k == "ActiveState":
                out["active"] = (v.strip() == "active")
            elif k == "ActiveEnterTimestamp":
                out["since"] = v.strip() or None
            elif k == "MainPID":
                try:
                    out["pid"] = int(v.strip())
                except ValueError:
                    out["pid"] = None
    except (subprocess.SubprocessError, OSError):
        pass
    return out


def _todays_stats(log_dir: Path) -> dict:
    """Sum proxy-log entries for today (UTC) into a flat dict."""
    counts = {"requests": 0, "warmups_blocked": 0,
              "warmups_passed": 0, "synthetic": 0}
    today = _dt.datetime.utcnow().strftime("%Y-%m-%d")
    p = log_dir / f"{today}.jsonl"
    if not p.exists():
        return counts
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
                counts["requests"] += 1
                if rec.get("warmup_blocked"):
                    counts["warmups_blocked"] += 1
                elif rec.get("is_warmup"):
                    counts["warmups_passed"] += 1
                if rec.get("synthetic"):
                    counts["synthetic"] += 1
    except OSError:
        pass
    return counts


def _config_proxy_host_port() -> Optional[str]:
    """Read ``config/claude-hooks.json`` to report the configured
    listen address. Returns None if config absent / unparseable.
    """
    cfg_path = REPO / "config" / "claude-hooks.json"
    if not cfg_path.exists():
        return None
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    p = cfg.get("proxy") or {}
    if not p.get("enabled"):
        return None
    host = p.get("listen_host", "127.0.0.1")
    port = p.get("listen_port", 38080)
    return f"http://{host}:{port}"


def _block_warmup_from_config() -> Optional[bool]:
    cfg_path = REPO / "config" / "claude-hooks.json"
    if not cfg_path.exists():
        return None
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return bool(((cfg.get("proxy") or {}).get("block_warmup")))


# ---------------------------------------------------------------- #
def build_status(log_dir: Path) -> dict:
    systemd = _systemd_status()
    state = _read_state(log_dir)
    today = _todays_stats(log_dir)
    listen = _config_proxy_host_port()
    block = _block_warmup_from_config()
    return {
        "timestamp": _dt.datetime.utcnow().strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "proxy": {
            "listen": listen,
            "block_warmup": block,
            "systemd": systemd,
        },
        "rate_limit": state,
        "today": today,
        "log_dir": str(log_dir),
    }


def render_text(status: dict) -> str:
    lines: list[str] = []
    lines.append("claude-hooks status")
    lines.append("=" * 40)
    p = status["proxy"]
    sd = p["systemd"]
    lines.append(f"Proxy config: listen={p['listen'] or '(disabled)'}  "
                 f"block_warmup={p['block_warmup']}")
    if sd.get("available"):
        badge = "active" if sd["active"] else "inactive"
        lines.append(
            f"  systemd:    {badge}"
            + (f"  pid={sd['pid']}" if sd['pid'] else "")
            + (f"  since={sd['since']}" if sd['since'] else "")
        )
    else:
        lines.append("  systemd:    (not available on this host)")

    rl = status["rate_limit"]
    lines.append("")
    lines.append("Rate-limit state (from proxy):")
    if not rl:
        lines.append("  (no data yet — run one Claude Code turn through the proxy)")
    else:
        claim = rl.get("representative_claim") or "?"
        five = rl.get("five_hour_utilization")
        seven = rl.get("seven_day_utilization")
        if isinstance(five, (int, float)):
            lines.append(f"  5h window:  {five*100:5.2f}% used"
                         + ("  ← binding" if claim == "five_hour" else ""))
        if isinstance(seven, (int, float)):
            lines.append(f"  7d window:  {seven*100:5.2f}% used"
                         + ("  ← binding" if claim == "seven_day" else ""))
        lu = rl.get("last_updated") or "?"
        lines.append(f"  updated:    {lu}")

    t = status["today"]
    lines.append("")
    lines.append("Today (UTC) — proxy log:")
    lines.append(
        f"  requests={t['requests']}  "
        f"blocked={t['warmups_blocked']}  "
        f"warmups_passed={t['warmups_passed']}  "
        f"synthetic={t['synthetic']}"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------- #
def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--log-dir", type=Path, default=DEFAULT_LOG_DIR,
        help=f"proxy log directory (default: {DEFAULT_LOG_DIR})",
    )
    ap.add_argument("--json", action="store_true",
                    help="emit JSON instead of a table")
    args = ap.parse_args(argv)
    status = build_status(args.log_dir)
    if args.json:
        print(json.dumps(status, indent=2))
    else:
        print(render_text(status))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
