"""Partial opt-out: keep named parts of the hooks in a disabled project.

``.claude-hooks-disable`` used to be all-or-nothing, and that is too
blunt for developing claude-hooks itself. There the code graph, the LSP
engine, the linters and the guards act on the code being changed, which
is the reason to disable them. But memory and the mailbox are how the
session keeps its context and talks to the other sessions, and losing
them costs more than it saves: a claude-hooks session disabled this way
was invisible on the mailbox, so mail sent to it simply queued.

The marker file now says what to keep::

    # .claude-hooks-disable — everything here is off, except:
    keep: memory, mailbox

An empty marker still turns everything off, exactly as before, so every
existing marker keeps its meaning.

This is an allow-list, not a set of switches over the normal handlers.
A project with a marker never runs ``claude_hooks.hooks.*``: each event
goes to :func:`run`, which can call the memory functions and the mailbox
functions and nothing else. A feature added to a handler later is
therefore off in these projects until someone adds it here on purpose,
which is the direction this has to fail in to be safe for development.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

log = logging.getLogger("claude_hooks.hook_parts")

#: What a marker can keep. ``memory`` is recall and the per-turn store;
#: ``mailbox`` is registration, announcements and the during-turn notice.
PARTS = frozenset({"memory", "mailbox"})

_KEEP_WORD = re.compile(r"^\s*keep\s*[:=]?\s*", re.IGNORECASE)


def find_marker(cwd: str, marker_filename: str) -> Optional[Path]:
    """The nearest marker at or above ``cwd``, or None.

    Nearest wins, so a subdirectory can say something different from the
    project it sits in.
    """
    if not cwd:
        return None
    p = Path(cwd).resolve()
    while True:
        candidate = p / marker_filename
        if candidate.is_file():
            return candidate
        if p.parent == p:
            return None
        p = p.parent


def parse_marker(text: str) -> frozenset:
    """The parts a marker keeps. Empty means everything is off.

    Accepts ``keep: a, b``, ``keep = a b``, or bare names, one or several
    per line; ``#`` starts a comment. An unknown name is logged and
    ignored — it must not enable anything, and it must not silently
    disable the parts that were spelled right.
    """
    kept: set[str] = set()
    for raw in text.splitlines():
        line = _KEEP_WORD.sub("", raw.split("#", 1)[0])
        for name in re.split(r"[\s,]+", line.strip().lower()):
            if not name:
                continue
            if name in PARTS:
                kept.add(name)
            else:
                log.warning("hook_parts: unknown part %r in marker — ignored "
                            "(known: %s)", name, ", ".join(sorted(PARTS)))
    return frozenset(kept)


def kept_parts(cwd: str, marker_filename: str) -> Optional[frozenset]:
    """None when no marker applies; otherwise the parts it keeps."""
    marker = find_marker(cwd, marker_filename)
    if marker is None:
        return None
    try:
        return parse_marker(marker.read_text(encoding="utf-8",
                                             errors="replace"))
    except OSError:
        log.debug("hook_parts: marker %s unreadable — treating as empty",
                  marker, exc_info=True)
        return frozenset()


# ---------------------------------------------------------------- #
# The restricted handlers
# ---------------------------------------------------------------- #

def run(event_name: str, *, event: dict, config: dict, providers,
        keep: frozenset) -> Optional[dict]:
    """Handle ``event_name`` with only the ``keep`` parts. Soft-fails."""
    fn = _EVENTS.get(event_name)
    if fn is None or not keep:
        return None
    try:
        return fn(event, config, providers, keep)
    except Exception:
        log.warning("hook_parts: %s failed", event_name, exc_info=True)
        return None


def _context(event_name: str, parts: list[str]) -> Optional[dict]:
    text = "\n\n".join(p for p in parts if p)
    if not text:
        return None
    return {"hookSpecificOutput": {"hookEventName": event_name,
                                   "additionalContext": text}}


def _session_start(event, config, providers, keep):
    parts: list[str] = []
    if "memory" in keep:
        hook_cfg = (config.get("hooks") or {}).get("session_start") or {}
        source = (event.get("source") or "startup").lower()
        if source == "compact" and hook_cfg.get("compact_recall", True):
            from claude_hooks.recall import run_recall
            parts.append(run_recall(
                hook_cfg.get("compact_recall_query",
                             "session context, key decisions, and "
                             "important patterns"),
                config=config, providers=providers,
                hook_name="user_prompt_submit",
                cwd=event.get("cwd", "")) or "")
    if "mailbox" in keep:
        from claude_hooks.mailbox import hook as mailbox
        # Every source, compact and resume included: a resumed session is
        # the same session, and its row may have gone at SessionEnd.
        parts.append(mailbox.register_session(
            event=event, config=config, providers=providers))
        parts.append(mailbox.announce_block(
            event=event, config=config, providers=providers))
    return _context("SessionStart", parts)


def _user_prompt_submit(event, config, providers, keep):
    parts: list[str] = []
    if "memory" in keep:
        hook_cfg = (config.get("hooks") or {}).get("user_prompt_submit") or {}
        prompt = (event.get("prompt") or "").strip()
        if (hook_cfg.get("enabled", True)
                and len(prompt) >= int(hook_cfg.get("min_prompt_chars", 30))):
            from claude_hooks.recall import run_recall
            parts.append(run_recall(
                prompt, config=config, providers=providers,
                hook_name="user_prompt_submit", cwd=event.get("cwd", ""),
                max_total_chars=int(hook_cfg.get("max_total_chars", 4000)),
                progressive=bool(hook_cfg.get("progressive"))) or "")
    if "mailbox" in keep:
        from claude_hooks.mailbox import hook as mailbox
        parts.append(mailbox.announce_block(
            event=event, config=config, providers=providers))
    return _context("UserPromptSubmit", parts)


def _stop(event, config, providers, keep):
    from claude_hooks.hooks import stop
    lines: list[str] = []
    if "memory" in keep:
        transcript_path = event.get("transcript_path")
        transcript = (stop._read_transcript(transcript_path)
                      if transcript_path else None)
        lines.append(stop.store_turn(event=event, config=config,
                                     providers=providers,
                                     transcript=transcript))
    if "mailbox" in keep:
        lines.append(stop._mailbox_notice(event, config, providers))
    message = "\n".join(line for line in lines if line)
    return {"systemMessage": message} if message else None


def _session_end(event, config, providers, keep):
    if "mailbox" in keep:
        from claude_hooks.mailbox import hook as mailbox
        mailbox.unregister_session(event=event, config=config,
                                   providers=providers)
    return None


_EVENTS = {
    "SessionStart": _session_start,
    "UserPromptSubmit": _user_prompt_submit,
    "Stop": _stop,
    "SessionEnd": _session_end,
}
