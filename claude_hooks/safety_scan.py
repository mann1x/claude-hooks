"""
Command safety scanner — content-based (not prefix-based) pattern
matcher for Bash commands about to be executed via the Bash tool.

Returns an ``ask`` permission decision when a dangerous pattern matches
anywhere in the command string — even if hidden after a pipe, behind
``find -exec``, in a subshell, or chained with ``&&``/``;``. Safe
commands auto-approve (no output).

Maintains an append-only JSONL log of scanner decisions at
``~/.claude/permission-scanner/YYYY-MM-DD.jsonl`` with automatic
rotation after ``log_retention_days`` (default 90).

Ported from rtfpessoa/code-factory's command-safety-scanner.sh:
https://github.com/rtfpessoa/code-factory/blob/main/hooks/command-safety-scanner.sh
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from claude_hooks.config import expand_user_path
from claude_hooks.safety_patterns import DEFAULT_PATTERNS

log = logging.getLogger("claude_hooks.safety_scan")


# Compiled-pattern cache keyed by (use_defaults, extras_tuple). The PreToolUse
# hook fires on every Bash/Edit/Write, so recompiling ~40 regexes each call
# is wasteful. Typical session sees a single cache entry.
_PATTERN_CACHE: dict[tuple, list[tuple[re.Pattern, str, str]]] = {}


def _extras_key(extra: Optional[list]) -> tuple:
    """Hashable canonical form for the `extra` argument."""
    if not extra:
        return ()
    out: list[tuple[str, str, str]] = []
    for entry in extra:
        if not isinstance(entry, dict):
            continue
        pattern = str(entry.get("pattern", ""))
        if not pattern:
            continue
        name = str(entry.get("name", pattern[:32]))
        reason = str(entry.get("reason", f"matches '{pattern}'"))
        out.append((pattern, name, reason))
    return tuple(out)


def compile_patterns(
    extra: Optional[list] = None,
    use_defaults: bool = True,
) -> list[tuple[re.Pattern, str, str]]:
    """Build the (regex, short_name, reason) list from defaults + extras.

    Result is cached keyed by (use_defaults, extras_tuple) so repeated calls
    with the same configuration return in O(1).
    """
    extras_key = _extras_key(extra)
    cache_key = (use_defaults, extras_key)
    cached = _PATTERN_CACHE.get(cache_key)
    if cached is not None:
        return cached

    raw: list[tuple[str, str, str]] = []
    if use_defaults:
        raw.extend(DEFAULT_PATTERNS)
    raw.extend(extras_key)

    compiled: list[tuple[re.Pattern, str, str]] = []
    for pattern, name, reason in raw:
        try:
            compiled.append((re.compile(pattern, re.IGNORECASE), name, reason))
        except re.error as e:
            log.warning("safety_scan: bad pattern %r skipped: %s", pattern, e)

    _PATTERN_CACHE[cache_key] = compiled
    return compiled


def reset_pattern_cache() -> None:
    """Clear the compiled-pattern cache. For tests."""
    _PATTERN_CACHE.clear()


def scan_command(
    command: str,
    patterns: list[tuple[re.Pattern, str, str]],
) -> Optional[tuple[str, str]]:
    """Return ``(short_name, reason)`` of the first matching pattern, or None."""
    if not command:
        return None
    for regex, name, reason in patterns:
        if regex.search(command):
            return name, reason
    return None


def log_match(
    *,
    log_dir: Path,
    pattern_name: str,
    reason: str,
    command: str,
    retention_days: int = 90,
) -> None:
    """Append a JSONL record of the scanner decision. Never raises."""
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return

    _maybe_rotate(log_dir, retention_days)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_file = log_dir / f"{today}.jsonl"
    record = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pattern": pattern_name,
        "reason": reason,
        "command": command[:500],
    }
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _maybe_rotate(log_dir: Path, retention_days: int) -> None:
    """Delete .jsonl files older than ``retention_days``. Runs at most daily."""
    marker = log_dir / ".last-rotation"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        if marker.exists() and marker.read_text(encoding="utf-8").strip() == today:
            return
    except OSError:
        pass

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    try:
        for entry in log_dir.glob("*.jsonl"):
            try:
                mtime = datetime.fromtimestamp(entry.stat().st_mtime, tz=timezone.utc)
                if mtime < cutoff:
                    entry.unlink()
            except OSError:
                continue
    except OSError:
        pass

    try:
        marker.write_text(today, encoding="utf-8")
    except OSError:
        pass


def default_log_dir() -> Path:
    return expand_user_path("~/.claude/permission-scanner")


def build_ask_response(reason: str) -> dict:
    """Build the Claude Code PreToolUse "ask" response JSON."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": reason,
        }
    }
