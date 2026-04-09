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

from claude_hooks.providers import Provider

log = logging.getLogger("claude_hooks.hooks.stop")


def handle(*, event: dict, config: dict, providers: list[Provider]) -> Optional[dict]:
    hook_cfg = (config.get("hooks") or {}).get("stop") or {}
    if not hook_cfg.get("enabled", True):
        return None

    threshold = (hook_cfg.get("store_threshold") or "noteworthy").lower()
    if threshold == "off":
        return None

    transcript_path = event.get("transcript_path")
    transcript = _read_transcript(transcript_path) if transcript_path else None

    if threshold == "noteworthy":
        if not _is_noteworthy(transcript):
            log.debug("turn not noteworthy — skipping store")
            return None

    summary = _build_summary(event, transcript)
    if not summary:
        return None

    metadata = {
        "type": "session_turn",
        "session_id": event.get("session_id", ""),
        "cwd": event.get("cwd", ""),
        "stored_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    stored = []
    failed = []
    for provider in providers:
        provider_cfg = ((config.get("providers") or {}).get(provider.name)) or {}
        if (provider_cfg.get("store_mode") or "auto").lower() != "auto":
            continue
        try:
            provider.store(summary, metadata=metadata)
            stored.append(provider.name)
        except Exception as e:
            failed.append((provider.name, str(e)))
            log.warning("provider %s store failed: %s", provider.name, e)

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


def _is_noteworthy(transcript: Optional[list[dict]]) -> bool:
    """
    Decide whether the most recent turn was worth remembering.
    Default heuristic: the assistant called a non-trivial tool.
    Trivial tools: TaskList, TaskGet, TodoRead, view-only Reads.
    """
    if not transcript:
        return False
    # Walk backwards to find the most recent user message, then look at
    # everything after it.
    last_user_idx = -1
    for i in range(len(transcript) - 1, -1, -1):
        msg = transcript[i]
        if not isinstance(msg, dict):
            continue
        role = (msg.get("message") or {}).get("role") or msg.get("role")
        if role == "user":
            last_user_idx = i
            break
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
        last_user_idx = -1
        for i in range(len(transcript) - 1, -1, -1):
            msg = transcript[i]
            if not isinstance(msg, dict):
                continue
            role = (msg.get("message") or {}).get("role") or msg.get("role")
            if role == "user":
                last_user_idx = i
                break
        if last_user_idx >= 0:
            user_msg = transcript[last_user_idx]
            user_text = _extract_text(user_msg)
            for msg in transcript[last_user_idx + 1 :]:
                if not isinstance(msg, dict):
                    continue
                role = (msg.get("message") or {}).get("role") or msg.get("role")
                content = (msg.get("message") or {}).get("content") or msg.get("content") or []
                if role == "assistant":
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
