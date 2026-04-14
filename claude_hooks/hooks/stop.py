"""
Stop handler — fired when the assistant finishes responding to a turn.

If the turn was *noteworthy* (default heuristic: the assistant wrote/edited
files OR ran a non-trivial Bash command), summarize it and store the summary
into all providers whose ``store_mode`` is ``auto``.

We deliberately don't try to be clever about content extraction. The summary
is built from the transcript file Claude Code writes alongside the session,
which contains the full message history. We pull the last assistant message
plus a one-line list of touched files / executed commands.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from claude_hooks.config import expand_user_path
from claude_hooks.providers import Provider

log = logging.getLogger("claude_hooks.hooks.stop")


def handle(*, event: dict, config: dict, providers: list[Provider]) -> Optional[dict]:
    hook_cfg = (config.get("hooks") or {}).get("stop") or {}
    if not hook_cfg.get("enabled", True):
        return None

    transcript_path = event.get("transcript_path")
    transcript = _read_transcript(transcript_path) if transcript_path else None

    # Stop-phrase guard: if the assistant is about to stop with an
    # ownership-dodging or session-quitting phrase, block the stop and
    # feed back a correction. Skip if the hook already fired this turn
    # (stop_hook_active) to avoid infinite loops.
    guard_cfg = (config.get("hooks") or {}).get("stop_guard") or {}
    if guard_cfg.get("enabled", False) and not event.get("stop_hook_active", False):
        correction = _run_stop_guard(transcript, guard_cfg)
        if correction:
            log.info("stop_guard blocked stop: %s", correction[:80])
            return {
                "decision": "block",
                "reason": f"STOP HOOK VIOLATION: {correction}",
            }

    threshold = (hook_cfg.get("store_threshold") or "noteworthy").lower()
    if threshold == "off":
        return None

    if threshold == "noteworthy":
        if not _is_noteworthy(transcript):
            log.debug("turn not noteworthy — skipping store")
            return None

    summary = _build_summary(event, transcript)
    if not summary:
        return None

    # Append OpenWolf data (cerebrum learnings, bug fixes) if available.
    try:
        from claude_hooks.openwolf import store_content
        wolf_content = store_content(event.get("cwd", ""))
        if wolf_content:
            summary += f"\n\n---\n## OpenWolf context\n{wolf_content}"
    except Exception as e:
        log.debug("openwolf store content skipped: %s", e)

    metadata = {
        "type": "session_turn",
        "session_id": event.get("session_id", ""),
        "cwd": event.get("cwd", ""),
        "stored_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    # Classify the observation type for better downstream filtering.
    if hook_cfg.get("classify_observations", True):
        metadata["observation_type"] = _classify_observation(summary, transcript)

    stored = []
    failed = []
    for provider in providers:
        provider_cfg = ((config.get("providers") or {}).get(provider.name)) or {}
        if (provider_cfg.get("store_mode") or "auto").lower() != "auto":
            continue

        # Dedup check: skip if a near-duplicate already exists.
        dedup_threshold = float(provider_cfg.get("dedup_threshold", 0.0))
        if dedup_threshold > 0.0 and len(summary) >= 100:
            try:
                from claude_hooks.dedup import should_store as dedup_ok
                if not dedup_ok(summary, provider, threshold=dedup_threshold):
                    log.info("skipping store to %s: near-duplicate detected", provider.name)
                    continue
            except Exception as e:
                log.debug("dedup check failed, storing anyway: %s", e)

        try:
            provider.store(summary, metadata=metadata)
            stored.append(provider.name)
        except Exception as e:
            failed.append((provider.name, str(e)))
            log.warning("provider %s store failed: %s", provider.name, e)

    # Instinct extraction: detect bug-fix patterns and save as reusable instincts.
    if hook_cfg.get("extract_instincts"):
        try:
            from claude_hooks.instincts import (
                detect_bug_fix, extract_instinct, merge_if_duplicate, save_instinct,
            )
            bug_fix = detect_bug_fix(transcript)
            if bug_fix:
                instinct = extract_instinct(bug_fix, summary, event.get("session_id", ""))
                instincts_dir = expand_user_path(
                    hook_cfg.get("instincts_dir", "~/.claude/instincts")
                )
                merged = merge_if_duplicate(instinct, instincts_dir)
                if not merged:
                    save_instinct(instinct, instincts_dir)
        except Exception as e:
            log.debug("instinct extraction skipped: %s", e)

    if not stored and not failed:
        return None

    parts = []
    if stored:
        parts.append(f"stored to {', '.join(stored)}")
    if failed:
        parts.append(f"failed: {', '.join(n for n, _ in failed)}")
    return {"systemMessage": f"[claude-hooks] {' · '.join(parts)}"}


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #
def _read_transcript(path: str) -> Optional[list[dict]]:
    """Load a JSONL transcript file. Returns None on any error."""
    try:
        p = Path(os.path.expanduser(path))
        if not p.exists():
            return None
        out: list[dict] = []
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out
    except OSError:
        return None


def _msg_role(msg: dict) -> str:
    """Extract the role from a transcript message."""
    return (msg.get("message") or {}).get("role") or msg.get("role") or ""


def _find_last_user_idx(transcript: list[dict]) -> int:
    """Return the index of the last user message, or -1 if none."""
    for i in range(len(transcript) - 1, -1, -1):
        msg = transcript[i]
        if isinstance(msg, dict) and _msg_role(msg) == "user":
            return i
    return -1


def _is_noteworthy(transcript: Optional[list[dict]]) -> bool:
    """
    Decide whether the most recent turn was worth remembering.
    Default heuristic: the assistant called a non-trivial tool.
    Trivial tools: TaskList, TaskGet, TodoRead, view-only Reads.
    """
    if not transcript:
        return False
    last_user_idx = _find_last_user_idx(transcript)
    if last_user_idx < 0:
        return False
    tail = transcript[last_user_idx + 1 :]

    interesting_tools = {
        "Bash", "Edit", "Write", "MultiEdit", "NotebookEdit",
        "mcp__github-mcp__create_pull_request",
        "mcp__github-mcp__create_or_update_file",
        "mcp__github-mcp__push_files",
    }
    for msg in tail:
        if not isinstance(msg, dict):
            continue
        content = (msg.get("message") or {}).get("content") or msg.get("content") or []
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    if block.get("name") in interesting_tools:
                        return True
    return False


def _build_summary(event: dict, transcript: Optional[list[dict]]) -> str:
    """
    Build a one-paragraph summary of the most recent turn for storage.
    Includes the user's prompt (truncated), the assistant's last text reply
    (truncated), and a list of files touched.
    """
    user_text = ""
    asst_text = ""
    files_touched: set[str] = set()
    commands: list[str] = []

    if transcript:
        last_user_idx = _find_last_user_idx(transcript)
        if last_user_idx >= 0:
            user_msg = transcript[last_user_idx]
            user_text = _extract_text(user_msg)
            for msg in transcript[last_user_idx + 1 :]:
                if not isinstance(msg, dict):
                    continue
                content = (msg.get("message") or {}).get("content") or msg.get("content") or []
                if _msg_role(msg) == "assistant":
                    asst_text = _extract_text(msg) or asst_text
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") != "tool_use":
                            continue
                        name = block.get("name", "")
                        inp = block.get("input") or {}
                        if name in ("Edit", "Write", "MultiEdit", "Read"):
                            fp = inp.get("file_path")
                            if fp:
                                files_touched.add(fp)
                        elif name == "Bash":
                            cmd = inp.get("command")
                            if cmd:
                                commands.append(cmd[:200])

    cwd = event.get("cwd", "")
    parts = [f"# Turn @ {datetime.now(timezone.utc).isoformat(timespec='seconds')}"]
    if cwd:
        parts.append(f"cwd: {cwd}")
    if user_text:
        # Skip storing meta/system prompts (e.g. Caliber learning extraction,
        # session analysis) — they pollute Qdrant recall on context resume.
        _meta_markers = (
            "extract reusable operational lessons",
            "analyze raw tool call events",
            "You are an expert developer experience engineer",
            "claudeMdLearnedSection",
        )
        if not any(m in user_text[:500] for m in _meta_markers):
            parts.append(f"\n## Prompt\n{_truncate(user_text, 600)}")
    if asst_text:
        parts.append(f"\n## Result\n{_truncate(asst_text, 1200)}")
    if files_touched:
        parts.append(f"\n## Files touched\n" + "\n".join(f"- {f}" for f in sorted(files_touched)[:20]))
    if commands:
        parts.append(f"\n## Commands\n" + "\n".join(f"- `{c}`" for c in commands[:10]))
    return "\n".join(parts)


def _extract_text(message: dict) -> str:
    """Extract plain text from a transcript message regardless of shape."""
    inner = message.get("message") if isinstance(message.get("message"), dict) else message
    content = inner.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                t = b.get("text")
                if isinstance(t, str):
                    parts.append(t)
        return "\n".join(parts)
    return ""


def _truncate(text: str, max_chars: int) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 20].rstrip() + "\n…(truncated)"


# ---------------------------------------------------------------------- #
# Observation classification
# ---------------------------------------------------------------------- #
_FIX_KEYWORDS = {
    "fix", "fixed", "bug", "error", "broken", "issue", "resolved", "patch",
    "workaround", "hotfix", "regression", "traceback", "exception",
}
_PREF_KEYWORDS = {
    "actually", "prefer", "don't", "always use", "never use",
    "should be", "not like that", "wrong approach",
}
_DECISION_KEYWORDS = {
    "chose", "decided", "architecture", "approach", "design", "strategy",
    "trade-off", "switched to", "migrated", "opted for", "went with",
}
_GOTCHA_KEYWORDS = {
    "gotcha", "pitfall", "watch out", "careful", "trap", "surprising",
    "unexpected", "quirk", "caveat", "heads up", "warning",
}


def _classify_observation(
    summary: str, transcript: Optional[list[dict]]
) -> str:
    """Classify a turn into: fix, preference, decision, gotcha, or general."""
    lower = summary.lower()

    # Priority 1: fix — transcript shows error followed by edit, or fix keywords
    if transcript:
        last_user_idx = _find_last_user_idx(transcript)
        if last_user_idx >= 0:
            tail = transcript[last_user_idx + 1:]
            saw_error = False
            saw_edit = False
            for msg in tail:
                if not isinstance(msg, dict):
                    continue
                content = (msg.get("message") or {}).get("content") or msg.get("content") or []
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "tool_result":
                            text = str(block.get("content", "")).lower()
                            if "error" in text or "traceback" in text or "failed" in text:
                                saw_error = True
                        if block.get("type") == "tool_use":
                            name = block.get("name", "")
                            if name in ("Edit", "Write", "MultiEdit") and saw_error:
                                saw_edit = True
            if saw_error and saw_edit:
                return "fix"

    if any(kw in lower for kw in _FIX_KEYWORDS):
        return "fix"

    # Priority 2: decision (before preference — "instead" is in both but
    # "decided"/"chose" is a stronger signal)
    if any(kw in lower for kw in _DECISION_KEYWORDS):
        return "decision"

    # Priority 3: preference
    if any(kw in lower for kw in _PREF_KEYWORDS):
        return "preference"

    # Priority 4: gotcha
    if any(kw in lower for kw in _GOTCHA_KEYWORDS):
        return "gotcha"

    return "general"


def _run_stop_guard(
    transcript: Optional[list[dict]],
    guard_cfg: dict,
) -> Optional[str]:
    """Return the stop-guard correction for the last assistant message, or None.

    Inspired by rtfpessoa/code-factory's stop-phrase-guard.sh:
    https://github.com/rtfpessoa/code-factory/blob/main/hooks/stop-phrase-guard.sh
    """
    if not transcript:
        return None
    # Find the last assistant text block.
    last_text = ""
    for msg in reversed(transcript):
        if isinstance(msg, dict) and _msg_role(msg) == "assistant":
            text = _extract_text(msg)
            if text:
                last_text = text
                break
    if not last_text:
        return None
    try:
        from claude_hooks.stop_guard import check_message, load_patterns
        patterns = load_patterns(guard_cfg.get("patterns") or [])
        return check_message(last_text, patterns=patterns)
    except Exception as e:
        log.debug("stop_guard check failed: %s", e)
        return None
