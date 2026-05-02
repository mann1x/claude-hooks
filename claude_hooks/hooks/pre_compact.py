"""PreCompact handler — synthesise a wrap-up summary before
context is auto-compacted.

Reads the session transcript, builds a deterministic markdown
summary of the eight-section ``/wrapup`` skill structure (with
the model-judgment sections clearly marked), persists it next to
the project (preferring ``.wolf/`` then ``docs/wrapup/``, falling
back to ``~/.claude/wrapup-pre-compact/``), and returns the
markdown as ``additionalContext`` so it lands inside the
compaction window.

Activation gates (BOTH must be true):

1. ``hooks.pre_compact.enabled`` is true (default true). User
   disables by flipping the flag in ``config/claude-hooks.json``.
2. The ``/wrapup`` skill is installed — i.e.
   ``~/.claude/skills/wrapup/SKILL.md`` exists and is readable.
   Removing the skill silences the hook by design: this is the
   "if the skill is enabled, run; otherwise stay out of the way"
   constraint.

The hook always exits 0 and never raises to the caller — a broken
synthesis must not block Claude Code's compaction.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from claude_hooks.providers import Provider

log = logging.getLogger("claude_hooks.hooks.pre_compact")

DEFAULT_WRAPUP_SKILL_PATH = Path.home() / ".claude" / "skills" / "wrapup" / "SKILL.md"


def _wrapup_skill_present(hook_cfg: dict) -> bool:
    """Resolve and stat the wrapup SKILL.md path. Honours an explicit
    ``wrapup_skill_path`` config override; otherwise defaults to the
    user-level skills dir."""
    pinned = hook_cfg.get("wrapup_skill_path") or ""
    if pinned:
        return Path(pinned).expanduser().is_file()
    return DEFAULT_WRAPUP_SKILL_PATH.is_file()


def handle(*, event: dict, config: dict, providers: list[Provider]) -> Optional[dict]:
    hook_cfg = (config.get("hooks") or {}).get("pre_compact") or {}
    if not hook_cfg.get("enabled", True):
        log.debug("pre_compact disabled by config — skipping")
        return None

    if not _wrapup_skill_present(hook_cfg):
        log.debug(
            "pre_compact: wrapup skill not installed at %s — skipping",
            DEFAULT_WRAPUP_SKILL_PATH,
        )
        return None

    transcript_path = event.get("transcript_path") or ""
    cwd = event.get("cwd") or ""
    session_id = event.get("session_id") or ""

    try:
        from claude_hooks.wrapup_synth import (
            read_transcript,
            synthesize_markdown,
            resolve_output_path,
            write_to_disk,
        )
    except Exception as e:
        log.warning("pre_compact: synth import failed: %s", e)
        return None

    try:
        transcript = read_transcript(transcript_path) if transcript_path else []
    except Exception as e:
        log.debug("pre_compact: transcript read failed: %s", e)
        transcript = []

    try:
        markdown = synthesize_markdown(
            transcript, cwd=cwd, session_id=session_id,
        )
    except Exception as e:
        log.warning("pre_compact: synthesis failed: %s", e)
        return None

    saved_to: Optional[Path] = None
    if hook_cfg.get("save_to_file", True):
        try:
            output_path = resolve_output_path(cwd, session_id)
            saved_to = write_to_disk(markdown, output_path)
            if saved_to:
                log.info("pre_compact: wrap-up written to %s", saved_to)
        except Exception as e:
            log.debug("pre_compact: file write failed: %s", e)

    # Build the additionalContext block. The file-location pointer
    # appears as the LAST line so post-compaction the assistant sees
    # it most recently in context (recent context dominates attention)
    # and reliably knows where to Read the full summary from disk.
    parts: list[str] = [markdown]
    if saved_to:
        if not parts[-1].endswith("\n"):
            parts.append("")
        parts.append("---")
        parts.append("")
        parts.append(
            f"**State summary saved to:** `{saved_to}` — Read this file "
            f"to recover the full pre-compaction context."
        )
    additional_context = "\n".join(parts)

    return {
        "hookSpecificOutput": {
            "hookEventName": "PreCompact",
            "additionalContext": additional_context,
        }
    }
