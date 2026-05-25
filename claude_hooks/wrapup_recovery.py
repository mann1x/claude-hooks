"""Post-compact wrap-up recovery.

When the PreCompact hook fires, ``wrapup_synth`` writes a markdown
state-summary file to disk before Claude Code drops the conversation
window. The hook also emits the markdown as ``additionalContext`` so
the post-compaction model sees it inline, but in practice that inline
context can get summarised away during the very next compaction cycle
or trimmed before the next user turn — and then the model has lost
the connection state (pod IDs, IPs, URLs, in-progress work) that it
needed to resume.

The fix here is small and additive: every UserPromptSubmit we scan
the three known wrap-up directories for any file modified within the
last ``max_age_seconds`` (default 24h), and prepend a short pointer
block to ``additionalContext`` telling the model to read the latest
file. Cheap (one ``os.scandir`` per directory) and the pointer block
is ~5 lines of markdown.

Triggers on **only** the existence of a recent file — if the user
hasn't compacted recently, nothing surfaces. The pointer's job is to
survive across the boundary; once the next session has read the file,
the model has the state it needs and the pointer is just one extra
line of context per turn.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("claude_hooks.wrapup_recovery")

_DEFAULT_MAX_AGE_SECONDS = 24 * 3600
_FNAME_PREFIX = "wrapup-pre-compact"


def _candidate_dirs(cwd: str) -> list[Path]:
    out: list[Path] = []
    if cwd:
        out.append(Path(cwd) / ".wolf")
        out.append(Path(cwd) / "docs" / "wrapup")
    out.append(Path.home() / ".claude" / "wrapup-pre-compact")
    return out


def _seen_marker_for(path: Path) -> Path:
    """Sidecar path used to mark a wrap-up file as already-surfaced."""
    return path.with_suffix(path.suffix + ".seen")


def find_recent_wrapup(cwd: str, *,
                       max_age_seconds: int = _DEFAULT_MAX_AGE_SECONDS,
                       now: Optional[float] = None,
                       skip_seen: bool = True) -> Optional[Path]:
    """Return the most recently-modified pre-compact wrap-up file
    found in any candidate directory, or None if nothing fresh.

    When ``skip_seen`` is True (default), files with a ``.seen``
    sidecar marker are ignored — the recovery pointer is one-shot
    per wrap-up file, so we don't re-inject it on every turn.
    """
    now = now if now is not None else time.time()
    cutoff = now - max_age_seconds
    best: Optional[tuple[float, Path]] = None
    for d in _candidate_dirs(cwd):
        try:
            if not d.is_dir():
                continue
            for entry in os.scandir(d):
                if not entry.is_file():
                    continue
                name = entry.name
                if not name.startswith(_FNAME_PREFIX) and _FNAME_PREFIX not in name:
                    continue
                if not name.endswith(".md"):
                    continue
                p = Path(entry.path)
                if skip_seen and _seen_marker_for(p).exists():
                    continue
                try:
                    mtime = entry.stat().st_mtime
                except OSError:
                    continue
                if mtime < cutoff:
                    continue
                if best is None or mtime > best[0]:
                    best = (mtime, p)
        except OSError as e:
            log.debug("wrapup_recovery: scandir(%s) failed: %s", d, e)
    return best[1] if best else None


def mark_seen(path: Path) -> bool:
    """Create the ``.seen`` sidecar so we don't re-inject the pointer
    for this wrap-up file. Best-effort — silent on failure."""
    try:
        _seen_marker_for(path).write_text("", encoding="utf-8")
        return True
    except OSError as e:
        log.debug("wrapup_recovery: mark_seen(%s) failed: %s", path, e)
        return False


def _extract_reconnect_block(text: str) -> str:
    """Return the fenced reconnect block synthesised into the wrap-up
    file (between the ``RECONNECT`` sentinels), or ``""`` when absent.

    The sentinels are written by :mod:`claude_hooks.wrapup_synth` ONLY
    when the session actually made an ssh connection — so a session with
    no remote work yields ``""`` here and the recovery block stays a bare
    pointer (zero extra tokens). The returned text already includes the
    ```` ``` ```` fences, so it renders as a code block when inlined.
    """
    try:
        from claude_hooks.wrapup_synth import (
            RECONNECT_SENTINEL_BEGIN as _BEGIN,
            RECONNECT_SENTINEL_END as _END,
        )
    except Exception:  # pragma: no cover - import guard
        _BEGIN, _END = "<!-- RECONNECT:BEGIN -->", "<!-- RECONNECT:END -->"
    i = text.find(_BEGIN)
    if i < 0:
        return ""
    j = text.find(_END, i + len(_BEGIN))
    if j < 0:
        return ""
    return text[i + len(_BEGIN):j].strip()


def get_cfg(config: dict) -> dict:
    raw = (config.get("hooks") or {}).get("wrapup_recovery") or {}
    return {
        "enabled": bool(raw.get("enabled", True)),
        "max_age_seconds": int(raw.get("max_age_seconds", _DEFAULT_MAX_AGE_SECONDS)),
    }


def format_recovery_block(cwd: str, config: dict, *,
                          now: Optional[float] = None,
                          mark: bool = True) -> str:
    """Return a short markdown pointer block, or empty string when
    disabled or no recent wrap-up file exists.

    On a hit, by default writes a ``.seen`` sidecar next to the
    wrap-up file so subsequent UserPromptSubmit calls skip it — the
    pointer is one-shot per file. Pass ``mark=False`` to inspect
    without consuming.
    """
    cfg = get_cfg(config)
    if not cfg["enabled"]:
        return ""
    path = find_recent_wrapup(cwd, max_age_seconds=cfg["max_age_seconds"], now=now)
    if path is None:
        return ""
    if mark:
        mark_seen(path)
    # Compact form. The full rationale (why-trimmed, what-to-recover)
    # used to be inline but stacked ~100 tokens across every turn for
    # 24h. The shorter form costs ~25 tokens and the file path itself
    # tells the model what to do.
    block = f"## Pre-compact wrap-up\n\nResume state: `{path}` — read first."

    # Inline the reconnect command(s) when the wrap-up captured a remote
    # connection. A bare file pointer is too easily ignored, and losing
    # the vast.ai / pod reconnect command (port included!) across a
    # compact is the exact failure this guards against. Costs nothing
    # when there are no connections — the sentinels are simply absent.
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        log.debug("wrapup_recovery: read(%s) failed: %s", path, e)
        text = ""
    reconnect = _extract_reconnect_block(text) if text else ""
    if reconnect:
        block += (
            "\n\n**Reconnect (initial + last known-good — verify before "
            "trusting):**\n" + reconnect
        )
    return block
