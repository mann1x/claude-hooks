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

    turn_modified = _turn_modified_files(transcript)

    # Claudemem reindex: if the turn touched files and the project has
    # a claudemem index, spawn a background reindex. Runs detached so
    # it never adds latency to the hook itself.
    reindex_cfg = (config.get("hooks") or {}).get("claudemem_reindex") or {}
    if (
        reindex_cfg.get("enabled", True)
        and reindex_cfg.get("check_on_stop", True)
        and turn_modified
    ):
        try:
            from claude_hooks.claudemem_reindex import reindex_if_dirty_async
            reindex_if_dirty_async(
                cwd=event.get("cwd", ""),
                turn_modified=True,
                lock_min_age_seconds=int(reindex_cfg.get("lock_min_age_seconds", 60)),
            )
        except Exception as e:
            log.debug("claudemem reindex skipped: %s", e)

    # code_graph rebuild: same pattern. If the turn modified source
    # files, spawn a detached graph rebuild so the next session (or
    # this session's next Grep symbol-lookup) sees fresh data. The
    # builder's own cooldown lock prevents thrash when many edits
    # land in rapid succession.
    cg_cfg = (config.get("hooks") or {}).get("code_graph") or {}
    if (
        cg_cfg.get("enabled", True)
        and cg_cfg.get("rebuild_on_stop", True)
        and turn_modified
    ):
        try:
            from claude_hooks.code_graph.__main__ import build_async as _cg_build_async
            _cg_build_async(
                cwd=event.get("cwd", ""),
                cooldown_minutes=int(cg_cfg.get("staleness_minutes", 10)),
                min_source_files=int(cg_cfg.get("min_source_files", 5)),
                max_files_to_scan=int(cg_cfg.get("max_files_to_scan", 2000)),
                lock_min_age_seconds=int(cg_cfg.get("lock_min_age_seconds", 60)),
            )
        except Exception as e:
            log.debug("code_graph Stop rebuild skipped: %s", e)

    # Companion engines (axon, gitnexus): when the project has been
    # indexed by either and the turn modified files, spawn that engine's
    # reindex. Silent no-op when neither tool is installed or the
    # project hasn't been initialised.
    comp_cfg = (config.get("hooks") or {}).get("companions") or {}
    if (
        comp_cfg.get("enabled", True)
        and comp_cfg.get("reindex_on_stop", True)
        and turn_modified
    ):
        try:
            from claude_hooks.companion_integration import reindex_if_dirty_async
            reindex_if_dirty_async(
                cwd=event.get("cwd", ""),
                turn_modified=True,
                lock_min_age_seconds=int(comp_cfg.get("lock_min_age_seconds", 60)),
            )
        except Exception as e:
            log.debug("companion reindex skipped: %s", e)

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

    summary_format = str(hook_cfg.get("summary_format", "markdown")).lower()
    summary = _build_summary(event, transcript, fmt=summary_format)
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
    # When summary_format=xml, prefer the <type> value embedded in the
    # summary itself — it was classified from the same data but at the
    # same time, so it stays in sync with the stored text. Falls back
    # to the dedicated _classify_observation when unavailable.
    if hook_cfg.get("classify_observations", True):
        xml_type = _extract_xml_observation_type(summary) if summary_format == "xml" else None
        metadata["observation_type"] = (
            xml_type or _classify_observation(summary, transcript)
        )
    # Tag with the summary format so consumers know how to parse.
    metadata["summary_format"] = summary_format

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
            log.debug("provider %s stored turn summary", provider.name)
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


def _is_real_user_prompt(msg: dict) -> bool:
    """True if ``msg`` is an actual user-typed prompt, not a tool_result echo.

    The Anthropic transcript schema returns tool results as ``role: "user"``
    messages whose content is one or more ``{type: "tool_result"}`` blocks.
    A naive "find the last role:user" treats those as turn boundaries, which
    cuts the noteworthy-check tail down to just the wrap-up after the final
    tool result and almost always misses the action_tools / reasoning markers
    earlier in the turn. Filter them out by requiring at least one non-
    tool_result content block (text, image, etc.) — or no list-shaped content
    at all (string content is always a real prompt).
    """
    if _msg_role(msg) != "user":
        return False
    content = (msg.get("message") or {}).get("content") or msg.get("content") or []
    if not isinstance(content, list):
        return True
    for block in content:
        if not isinstance(block, dict):
            return True
        if block.get("type") != "tool_result":
            return True
    return False


def _find_last_user_idx(transcript: list[dict]) -> int:
    """Return the index of the last *real* user prompt, or -1 if none.

    Skips tool_result echoes that also carry ``role: "user"`` — see
    ``_is_real_user_prompt``.
    """
    for i in range(len(transcript) - 1, -1, -1):
        msg = transcript[i]
        if isinstance(msg, dict) and _is_real_user_prompt(msg):
            return i
    return -1


def _turn_modified_files(transcript: Optional[list[dict]]) -> bool:
    """Return True if the most recent turn called Edit/Write/MultiEdit."""
    if not transcript:
        return False
    last_user_idx = _find_last_user_idx(transcript)
    if last_user_idx < 0:
        return False
    modifying_tools = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
    for msg in transcript[last_user_idx + 1 :]:
        if not isinstance(msg, dict):
            continue
        content = (msg.get("message") or {}).get("content") or msg.get("content") or []
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    if block.get("name") in modifying_tools:
                        return True
    return False


_REASONING_MARKERS = (
    "root cause",
    "diagnosed",
    "diagnose",
    "confirmed",
    "investigated",
    "investigation",
    "the bug is",
    "the issue is",
    "the problem is",
    "the fix is",
    "fixed by",
    "turns out",
    "key insight",
    "key finding",
    "found that",
    "verified that",
    "evidence: ",
    "in conclusion",
)

_TRIVIAL_TOOLS = frozenset({
    "Read", "Glob", "Grep", "TaskList", "TaskGet", "TodoRead",
    "TaskCreate", "TaskUpdate",
})


def _is_noteworthy(transcript: Optional[list[dict]]) -> bool:
    """
    Decide whether the most recent turn was worth remembering.

    Two paths qualify:

    1) **Action turn** — the assistant called a non-trivial tool such
       as ``Edit``/``Write``/``Bash`` (the historical bar). Files
       changed or shell commands executed are concrete enough to
       memorize on their own.
    2) **Diagnostic turn** — the assistant produced reasoning text
       containing markers like *root cause*, *diagnosed*, *confirmed*,
       *fixed by*, AND ran at least one tool (even a "trivial" one
       like ``Read`` or ``Grep`` — the *combination* with reasoning is
       what signals a real investigation, not vibes-only commentary).

    Trivial-only turns (read + describe with no diagnostic markers)
    still skip — they're recoverable from `git log` and the transcript
    itself, so storing them dilutes recall.
    """
    if not transcript:
        return False
    last_user_idx = _find_last_user_idx(transcript)
    if last_user_idx < 0:
        return False
    tail = transcript[last_user_idx + 1 :]

    action_tools = frozenset({
        "Bash", "Edit", "Write", "MultiEdit", "NotebookEdit",
        "mcp__github-mcp__create_pull_request",
        "mcp__github-mcp__create_or_update_file",
        "mcp__github-mcp__push_files",
    })

    saw_any_tool = False
    asst_text_parts: list[str] = []
    for msg in tail:
        if not isinstance(msg, dict):
            continue
        if _msg_role(msg) == "assistant":
            t = _extract_text(msg)
            if t:
                asst_text_parts.append(t)
        content = (msg.get("message") or {}).get("content") or msg.get("content") or []
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "tool_use":
                    continue
                name = block.get("name", "")
                if name in action_tools:
                    return True  # Path 1: action turn
                if name and name not in _TRIVIAL_TOOLS:
                    # MCP calls, WebFetch, WebSearch, etc. — non-trivial
                    # by themselves.
                    return True
                saw_any_tool = True

    if not saw_any_tool:
        return False

    # Path 2: trivial-tool turn upgraded by diagnostic reasoning.
    blob = "\n".join(asst_text_parts).lower()
    return any(m in blob for m in _REASONING_MARKERS)


def _build_summary(
    event: dict,
    transcript: Optional[list[dict]],
    *,
    fmt: str = "markdown",
) -> str:
    """Build a stored summary of the most recent turn.

    ``fmt`` selects the output shape:

    - ``"markdown"`` (default, back-compat) — the original human-readable
      sectioned layout with ``## Prompt`` / ``## Result`` / etc.
    - ``"xml"`` — a structured ``<observation>`` block (ported from
      thedotmack/claude-mem). Better for downstream grep / recall
      because every field is addressable and the type/title/subtitle
      tuple lets handlers dispatch without parsing prose.
    """
    user_text = ""
    asst_text = ""
    files_modified: set[str] = set()
    files_read: set[str] = set()
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
                        if name in ("Edit", "Write", "MultiEdit"):
                            fp = inp.get("file_path")
                            if fp:
                                files_modified.add(fp)
                        elif name == "Read":
                            fp = inp.get("file_path")
                            if fp:
                                files_read.add(fp)
                        elif name == "Bash":
                            cmd = inp.get("command")
                            if cmd:
                                commands.append(cmd[:200])

    # Meta-prompt filter applies to both formats — drop the user_text
    # entirely if it looks like a Caliber / session-analysis prompt.
    _meta_markers = (
        "extract reusable operational lessons",
        "analyze raw tool call events",
        "You are an expert developer experience engineer",
        "claudeMdLearnedSection",
    )
    if user_text and any(m in user_text[:500] for m in _meta_markers):
        user_text = ""

    if fmt == "xml":
        return _build_summary_xml(
            event, user_text, asst_text,
            files_modified, files_read, commands,
        )
    return _build_summary_markdown(
        event, user_text, asst_text,
        files_modified, files_read, commands,
    )


# ------------------------------------------------------------------ #
# Formatters
# ------------------------------------------------------------------ #
def _build_summary_markdown(
    event: dict, user_text: str, asst_text: str,
    files_modified: set[str], files_read: set[str], commands: list[str],
) -> str:
    cwd = event.get("cwd", "")
    parts = [f"# Turn @ {datetime.now(timezone.utc).isoformat(timespec='seconds')}"]
    if cwd:
        parts.append(f"cwd: {cwd}")
    if user_text:
        parts.append(f"\n## Prompt\n{_truncate(user_text, 600)}")
    if asst_text:
        parts.append(f"\n## Result\n{_truncate(asst_text, 1200)}")
    # Back-compat: read + modified go under the same "Files touched" heading.
    files_touched = sorted(files_modified | files_read)[:20]
    if files_touched:
        parts.append(f"\n## Files touched\n" + "\n".join(f"- {f}" for f in files_touched))
    if commands:
        parts.append(f"\n## Commands\n" + "\n".join(f"- `{c}`" for c in commands[:10]))
    return "\n".join(parts)


def _build_summary_xml(
    event: dict, user_text: str, asst_text: str,
    files_modified: set[str], files_read: set[str], commands: list[str],
) -> str:
    """Structured observation layout (ported from thedotmack/claude-mem).

    Every field is independently addressable so downstream recall /
    consolidation handlers can filter on ``<type>`` or ``<files_modified>``
    without prose parsing.
    """
    import html as _html
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cwd = event.get("cwd", "")
    obs_type = _classify_turn_type(
        user_text, asst_text, files_modified, files_read, commands,
    )
    title = _derive_title(asst_text, files_modified, commands) or "(no summary)"
    subtitle = cwd or ""

    def esc(s: str) -> str:
        return _html.escape(s or "", quote=False)

    lines = [f'<observation ts="{ts}">']
    lines.append(f"  <type>{obs_type}</type>")
    lines.append(f"  <title>{esc(title)}</title>")
    if subtitle:
        lines.append(f"  <subtitle>{esc(subtitle)}</subtitle>")
    if cwd:
        lines.append(f"  <cwd>{esc(cwd)}</cwd>")
    if user_text:
        lines.append(f"  <prompt>{esc(_truncate(user_text, 600))}</prompt>")
    if asst_text:
        lines.append(f"  <result>{esc(_truncate(asst_text, 1200))}</result>")
    if files_modified:
        lines.append("  <files_modified>")
        for f in sorted(files_modified)[:20]:
            lines.append(f"    <file>{esc(f)}</file>")
        lines.append("  </files_modified>")
    if files_read:
        lines.append("  <files_read>")
        for f in sorted(files_read)[:20]:
            lines.append(f"    <file>{esc(f)}</file>")
        lines.append("  </files_read>")
    if commands:
        lines.append("  <commands>")
        for c in commands[:10]:
            lines.append(f"    <command>{esc(c)}</command>")
        lines.append("  </commands>")
    lines.append("</observation>")
    return "\n".join(lines)


def _extract_xml_observation_type(summary: str) -> Optional[str]:
    """Parse ``<type>…</type>`` from an XML observation summary.

    Returns None when the summary isn't XML or the tag is missing. Does
    not use an XML parser — the content is tiny and we already escape
    user text when emitting it, so a simple regex is sufficient and
    never raises on malformed input.
    """
    if not summary or "<observation" not in summary:
        return None
    import re as _re
    m = _re.search(r"<type>([^<]+)</type>", summary)
    if not m:
        return None
    val = m.group(1).strip()
    return val or None


_TYPE_KEYWORDS = (
    ("fix", ("fix", "bug", "broken", "traceback", "regression", "failed", "hotfix")),
    ("refactor", ("refactor", "rename", "cleanup", "simplify", "extract", "dedup")),
    ("feature", ("add", "new", "implement", "introduce", "create", "feature")),
    ("investigation", ("investigate", "why", "analysis", "audit", "scan", "mine")),
    ("docs", ("document", "docs", "readme", "comment", "changelog")),
    ("build", ("build", "install", "package", "deploy", "release")),
    ("test", ("test", "coverage", "pytest")),
)


def _classify_turn_type(
    user_text: str, asst_text: str,
    files_modified: set[str], files_read: set[str], commands: list[str],
) -> str:
    """Very lightweight classifier for the XML ``<type>`` field."""
    blob = (user_text + "\n" + asst_text).lower()
    for label, words in _TYPE_KEYWORDS:
        if any(w in blob for w in words):
            return label
    if files_modified:
        return "edit"
    if commands:
        return "shell"
    if files_read:
        return "read"
    return "general"


def _derive_title(
    asst_text: str, files_modified: set[str], commands: list[str],
) -> str:
    """Pick the most informative single-line title for the observation."""
    if asst_text:
        # First non-empty line of the assistant's reply, truncated.
        for line in asst_text.splitlines():
            line = line.strip()
            if line:
                return line[:120]
    if files_modified:
        return f"edit {', '.join(sorted(files_modified)[:3])}"
    if commands:
        return commands[0][:120]
    return ""


def _extract_text(message: dict) -> str:
    """Extract plain text from a transcript message regardless of shape.

    Also strips Claude Code system-injected tags so they don't pollute
    Qdrant recall. Stolen from thedotmack/claude-mem's summariser —
    these tags are boilerplate that the model sees on every turn.
    """
    inner = message.get("message") if isinstance(message.get("message"), dict) else message
    content = inner.get("content")
    text = ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: list[str] = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                t = b.get("text")
                if isinstance(t, str):
                    parts.append(t)
        text = "\n".join(parts)
    return _strip_system_tags(text) if text else text


# ----------------------------------------------------------------- #
# Tag stripping — ported from thedotmack/claude-mem
# ----------------------------------------------------------------- #
_SYSTEM_TAG_PATTERN = None  # lazy-compiled below


def _strip_system_tags(text: str) -> str:
    """Remove ``<system-reminder>…</system-reminder>`` and similar
    Claude-Code-injected blocks. These are boilerplate that recur on
    every turn and drown real content in Qdrant recall.

    Tags stripped (case-insensitive, greedy across newlines):

    - ``<system-reminder>`` — reminders injected after every
      UserPromptSubmit hook run
    - ``<persisted-output>`` — cached tool output injected by Claude Code
    - ``<command-name>`` / ``<command-message>`` / ``<command-args>`` —
      slash-command preambles
    - ``<system-prompt>`` — only the literal outer tag; the body stays
      because that's the canonical system prompt, not noise
    - ``<local-command-stdout>`` / ``<local-command-caveat>`` — bash-
      prefix shell execution artefacts

    The stripping is outer-tag-first; nested tags of the same type are
    handled by the greedy-across-newlines ``.*?`` in each pattern.
    """
    global _SYSTEM_TAG_PATTERN
    if not text or "<" not in text:
        return text
    import re as _re
    if _SYSTEM_TAG_PATTERN is None:
        tags = (
            "system-reminder",
            "persisted-output",
            "command-name",
            "command-message",
            "command-args",
            "local-command-stdout",
            "local-command-caveat",
        )
        _SYSTEM_TAG_PATTERN = _re.compile(
            r"<(" + "|".join(tags) + r")\b[^>]*>.*?</\1>",
            _re.IGNORECASE | _re.DOTALL,
        )
    # Loop until stable — the regex is non-greedy, so nested tags of
    # the same name need multiple passes. Bound at 5 passes to avoid
    # pathological inputs. After paired-tag removal, a second regex
    # sweeps any stray opening / closing tags left behind by nested
    # input (doesn't happen in practice but keeps the output clean).
    cleaned = text
    for _ in range(5):
        new = _SYSTEM_TAG_PATTERN.sub("", cleaned)
        if new == cleaned:
            break
        cleaned = new
    tags_re = r"(system-reminder|persisted-output|command-name|command-message|command-args|local-command-stdout|local-command-caveat)"
    cleaned = _re.sub(
        rf"</?{tags_re}\b[^>]*>", "", cleaned, flags=_re.IGNORECASE,
    )
    # Collapse the 3+ blank lines the substitution tends to leave behind.
    cleaned = _re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


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
    # Find the last user message text (excluding tool_result blocks) so
    # the guard can honour explicit user wrap-up requests.
    last_user_text = ""
    for msg in reversed(transcript):
        if isinstance(msg, dict) and _msg_role(msg) == "user":
            t = _extract_text(msg)
            if t:
                last_user_text = t
                break

    try:
        from claude_hooks.stop_guard import check_message, load_patterns
        patterns = load_patterns(guard_cfg.get("patterns") or [])
        skip_meta = bool(guard_cfg.get("skip_meta_context", True))
        meta_cfg = guard_cfg.get("meta_markers") or []
        meta_markers = tuple(str(m) for m in meta_cfg) or None
        skip_wrapup = bool(guard_cfg.get("skip_on_user_wrap_up", True))
        wrapup_cfg = guard_cfg.get("user_wrap_up_markers") or []
        wrapup_markers = tuple(str(m) for m in wrapup_cfg) or None
        return check_message(
            last_text,
            patterns=patterns,
            skip_meta_context=skip_meta,
            meta_markers=meta_markers,
            last_user_message=last_user_text,
            skip_on_user_wrap_up=skip_wrapup,
            user_wrap_up_markers=wrapup_markers,
        )
    except Exception as e:
        log.debug("stop_guard check failed: %s", e)
        return None
