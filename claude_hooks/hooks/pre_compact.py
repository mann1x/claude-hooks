"""PreCompact handler — synthesise a wrap-up summary before
context is auto-compacted.

Reads the session transcript, builds a deterministic markdown
summary of the eight-section ``/wrapup`` skill structure (with
the model-judgment sections clearly marked), and persists it next
to the project (preferring ``.wolf/`` then ``docs/wrapup/``,
falling back to ``~/.claude/wrapup-pre-compact/``).

The post-compaction assistant picks up the file via
:mod:`claude_hooks.wrapup_recovery`, which prepends a one-shot
pointer block to ``additionalContext`` on the next
``UserPromptSubmit`` after compaction.

Why we don't return ``hookSpecificOutput.additionalContext`` from
PreCompact: Claude Code's PreCompact event schema does NOT include
``hookSpecificOutput`` — it only accepts the universal
``continue`` / ``stopReason`` / ``suppressOutput`` envelope. Trying
to emit ``additionalContext`` from this hook fails the JSON-schema
validator with ``(root): Invalid input`` and the whole hook is
reported as failed even though the disk write succeeded. So the
disk file + post-compact recovery is the ONLY delivery channel.

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
            DEFAULT_MAX_TRANSCRIPT_BYTES,
        )
    except Exception as e:
        log.warning("pre_compact: synth import failed: %s", e)
        return None

    # Bound the transcript read so a huge long-lived transcript can't
    # blow the PreCompact hook's wall-clock budget (the 581 MB
    # backup_models case: the read timed out, the wrap-up was never
    # written, and the post-compact recovery had nothing to surface).
    # ``max_transcript_mb`` of 0 (or negative) disables the cap.
    max_mb = hook_cfg.get("max_transcript_mb", 24)
    try:
        max_bytes = int(max_mb) * 1024 * 1024
    except (TypeError, ValueError):
        max_bytes = DEFAULT_MAX_TRANSCRIPT_BYTES
    if max_bytes < 0:
        max_bytes = 0

    try:
        transcript = (
            read_transcript(transcript_path, max_bytes=max_bytes)
            if transcript_path else []
        )
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

    if hook_cfg.get("save_to_file", True):
        try:
            output_path = resolve_output_path(cwd, session_id)
            saved_to = write_to_disk(markdown, output_path)
            if saved_to:
                log.info("pre_compact: wrap-up written to %s", saved_to)
        except Exception as e:
            log.debug("pre_compact: file write failed: %s", e)

    # No JSON output. PreCompact's hook schema doesn't accept
    # hookSpecificOutput.additionalContext (Claude Code rejects it as
    # "(root): Invalid input"). The wrap-up file on disk is the only
    # delivery channel; wrapup_recovery surfaces the pointer on the
    # next post-compaction UserPromptSubmit.
    return None
