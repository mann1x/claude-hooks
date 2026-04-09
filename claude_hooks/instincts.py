"""
Instinct extraction — auto-distill reusable debugging patterns from bug fixes.

When the Stop hook detects a bug-fix pattern in the transcript (Bash error
followed by Edit), it extracts the pattern as a markdown "instinct" file
under ``~/.claude/instincts/``. Instincts are promoted cross-project:
future sessions recall them when encountering similar errors.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from claude_hooks.config import expand_user_path

log = logging.getLogger("claude_hooks.instincts")


@dataclass
class Instinct:
    title: str
    action: str
    evidence: str
    confidence: float
    created: str
    source_session: str
    source_file: str


def detect_bug_fix(transcript: Optional[list[dict]]) -> Optional[dict]:
    """
    Detect a bug-fix pattern: a Bash tool call that produced an error,
    followed by an Edit/Write that fixed it.
    Returns dict with error info or None.
    """
    if not transcript:
        return None

    # Find last user message.
    last_user_idx = -1
    for i in range(len(transcript) - 1, -1, -1):
        msg = transcript[i]
        if isinstance(msg, dict):
            role = (msg.get("message") or {}).get("role") or msg.get("role")
            if role == "user":
                last_user_idx = i
                break
    if last_user_idx < 0:
        return None

    tail = transcript[last_user_idx + 1:]
    error_info: Optional[dict] = None
    fix_file: Optional[str] = None

    for msg in tail[-20:]:  # Only look at last 20 messages.
        if not isinstance(msg, dict):
            continue
        content = (msg.get("message") or {}).get("content") or msg.get("content") or []
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue

            # Look for tool_result with error indicators.
            if block.get("type") == "tool_result":
                text = str(block.get("content", "")).lower()
                if any(kw in text for kw in ("error", "traceback", "failed", "exception", "errno")):
                    error_info = {
                        "error_text": str(block.get("content", ""))[:300],
                    }

            # Look for Edit/Write after an error.
            if block.get("type") == "tool_use" and error_info:
                name = block.get("name", "")
                inp = block.get("input") or {}
                if name in ("Edit", "Write", "MultiEdit"):
                    fix_file = inp.get("file_path", "")
                    error_info["fix_file"] = fix_file
                    error_info["fix_snippet"] = (
                        inp.get("new_string", "") or inp.get("content", "")
                    )[:200]
                    return error_info

    return None


def extract_instinct(
    bug_fix: dict,
    summary: str,
    session_id: str,
) -> Instinct:
    """Create an Instinct from a detected bug-fix pattern."""
    error_text = bug_fix.get("error_text", "")
    fix_file = bug_fix.get("fix_file", "")
    fix_snippet = bug_fix.get("fix_snippet", "")

    # Build a title from the error.
    title = _derive_title(error_text, fix_file)

    action = f"When encountering this error in {Path(fix_file).name or 'code'}, "
    if fix_snippet:
        action += f"apply a fix like: {fix_snippet[:100]}"
    else:
        action += "check the surrounding code for the same pattern."

    evidence = f"Error: {error_text[:150]}\nFixed in: {fix_file}"

    return Instinct(
        title=title,
        action=action,
        evidence=evidence,
        confidence=0.6,
        created=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        source_session=session_id,
        source_file=fix_file,
    )


def save_instinct(instinct: Instinct, instincts_dir: Path) -> Path:
    """Write the instinct as a markdown file with YAML frontmatter."""
    instincts_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", instinct.title.lower())[:50].strip("-")
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    filename = f"{slug}-{ts}.md"
    path = instincts_dir / filename

    content = (
        f"---\n"
        f"title: \"{instinct.title}\"\n"
        f"confidence: {instinct.confidence}\n"
        f"created: {instinct.created}\n"
        f"source_session: {instinct.source_session}\n"
        f"source_file: {instinct.source_file}\n"
        f"---\n\n"
        f"## Action\n{instinct.action}\n\n"
        f"## Evidence\n{instinct.evidence}\n"
    )
    path.write_text(content, encoding="utf-8")
    log.info("instinct saved: %s", path)
    return path


def merge_if_duplicate(instinct: Instinct, instincts_dir: Path) -> Optional[Path]:
    """Check for an existing instinct about the same file/error. Merge if found."""
    if not instincts_dir.exists():
        return None

    for p in instincts_dir.glob("*.md"):
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        # Simple match: same source file mentioned in frontmatter.
        if instinct.source_file and instinct.source_file in text:
            # Bump confidence and add evidence.
            if "confidence:" in text:
                old_conf = re.search(r"confidence:\s*([\d.]+)", text)
                if old_conf:
                    new_conf = min(1.0, float(old_conf.group(1)) + 0.1)
                    text = text.replace(
                        f"confidence: {old_conf.group(1)}",
                        f"confidence: {new_conf}",
                    )
            text += f"\n- [{instinct.created}] {instinct.evidence[:100]}\n"
            p.write_text(text, encoding="utf-8")
            log.info("instinct merged into: %s", p)
            return p

    return None


def _derive_title(error_text: str, fix_file: str) -> str:
    """Generate a short title from the error text."""
    # Try to extract the key error type.
    for pattern in [
        r"(\w+Error):",
        r"(\w+Exception):",
        r"(FAIL|ERROR|FATAL):",
        r"(command not found|permission denied|no such file)",
    ]:
        m = re.search(pattern, error_text, re.IGNORECASE)
        if m:
            base = m.group(1)
            name = Path(fix_file).name if fix_file else "code"
            return f"{base} in {name}"

    name = Path(fix_file).name if fix_file else "code"
    return f"Fix pattern in {name}"
