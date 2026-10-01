"""Who wrote the prompt: the user, or the harness.

Claude Code fires ``UserPromptSubmit`` for every turn it starts, and many
of those are not user messages:

* ``task-notification`` — a background Bash job, a subagent or a Monitor
  finished (``<task-notification>…``). In the 300 most recent transcripts
  on solidpc these outnumber typed prompts two to one.
* ``scheduled`` — a ``ScheduleWakeup`` / ``/loop`` firing, or a cron job
  (``CronCreate``). Free-form text written in advance by the model or
  the user, re-sent on every tick.

Treating them as user prompts is wrong in several places. Recall runs a
HyDE expansion over notification XML. The Stop hook stores the turn
under ``## Prompt <task-notification>``, and later recall surfaces those
turns as if the user had asked them. The stop guard reads a notification
as "the user's last word".

The hook input does not say where a prompt came from. Claude Code knows:
it passes ``promptSource`` / ``wakeupSource`` into the hook builder, but
2.1.284 compiles that field out of the payload. The transcript has it,
though: every user row carries ``origin.kind`` (``human`` /
``task-notification``) and ``promptSource`` (``typed`` / ``queued`` /
``suggestion_accepted`` / ``system``), with ``isMeta`` set on scheduled
prompts. So:

1. ``<task-notification>`` text is classified from the text alone.
2. Otherwise the transcript is searched for the row carrying this exact
   prompt, and its origin decides.
3. Otherwise a wakeup's harness suffix (``(When this fires: …``) or a
   ``/loop`` sentinel marks it scheduled.
4. Otherwise ``human``: the safe default is today's behaviour.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("claude_hooks.prompt_origin")

HUMAN = "human"
TASK_NOTIFICATION = "task-notification"
SCHEDULED = "scheduled"
SYNTHETIC_KINDS = (TASK_NOTIFICATION, SCHEDULED)

_NOTIFICATION_PREFIX = "<task-notification>"
_SCHEDULED_MARKERS = (
    "(When this fires:",            # ScheduleWakeup harness suffix
    "<<autonomous-loop",            # /loop sentinels
)
_HUMAN_SOURCES = ("typed", "queued", "suggestion_accepted")

#: How far back from the end of the transcript to look. A prompt is
#: always among the last rows; reading the whole file (hundreds of MB on a
#: long session) on every prompt would cost more than the recall it gates.
_TAIL_BYTES = 512 * 1024


@dataclass(frozen=True)
class PromptOrigin:
    kind: str           # HUMAN | TASK_NOTIFICATION | SCHEDULED
    decided_by: str     # "text" | "transcript" | "marker" | "default"

    @property
    def synthetic(self) -> bool:
        return self.kind in SYNTHETIC_KINDS


def row_text(row: dict) -> str:
    """The prompt text of a transcript user row ("" for a tool_result)."""
    content = (row.get("message") or {}).get("content")
    if content is None:
        content = row.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result":
            return ""
        if block.get("type") == "text":
            parts.append(block.get("text") or "")
    return "\n".join(parts)


def row_kind(row: dict) -> Optional[str]:
    """Classify a transcript user row by the metadata Claude Code wrote,
    or ``None`` when the row carries none (old transcripts)."""
    origin = (row.get("origin") or {}).get("kind")
    if origin == TASK_NOTIFICATION:
        return TASK_NOTIFICATION
    if origin == HUMAN:
        return HUMAN
    source = row.get("promptSource")
    if source in _HUMAN_SOURCES:
        return HUMAN
    if source == "system" or row.get("isMeta"):
        return SCHEDULED
    return None


def classify_text(prompt: str) -> Optional[str]:
    text = (prompt or "").lstrip()
    if text.startswith(_NOTIFICATION_PREFIX):
        return TASK_NOTIFICATION
    return None


def _tail_rows(path: str) -> list[dict]:
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - _TAIL_BYTES))
            raw = f.read()
    except OSError:
        return []
    lines = raw.split(b"\n")
    if size > _TAIL_BYTES:
        lines = lines[1:]              # the first one is cut mid-line
    rows = []
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("type") == "user":
            rows.append(row)
    return rows


def classify_from_transcript(prompt: str,
                             transcript_path: Optional[str]) -> Optional[str]:
    if not transcript_path:
        return None
    want = (prompt or "").strip()
    if not want:
        return None
    for row in reversed(_tail_rows(transcript_path)):
        if row_text(row).strip() == want:
            return row_kind(row)
    return None


def classify(prompt: str,
             transcript_path: Optional[str] = None) -> PromptOrigin:
    kind = classify_text(prompt)
    if kind:
        return PromptOrigin(kind, "text")
    try:
        kind = classify_from_transcript(prompt, transcript_path)
    except Exception:  # pragma: no cover — never fail a prompt over this
        log.debug("transcript classification failed", exc_info=True)
        kind = None
    if kind:
        return PromptOrigin(kind, "transcript")
    if any(m in (prompt or "") for m in _SCHEDULED_MARKERS):
        return PromptOrigin(SCHEDULED, "marker")
    return PromptOrigin(HUMAN, "default")


def last_human_text(transcript: list[dict]) -> str:
    """Text of the most recent prompt the user wrote (typed, queued or an
    accepted suggestion), skipping notifications, scheduled prompts and
    tool results. Rows without origin metadata count as human, as
    before."""
    for row in reversed(transcript or []):
        if not isinstance(row, dict) or row.get("type", "user") != "user":
            continue
        role = (row.get("message") or {}).get("role") or row.get("role")
        if role not in (None, "user"):
            continue
        text = row_text(row)
        if not text:
            continue
        kind = row_kind(row) or classify_text(text) or HUMAN
        if kind == HUMAN:
            return text
    return ""


__all__ = [
    "HUMAN", "SCHEDULED", "SYNTHETIC_KINDS", "TASK_NOTIFICATION",
    "PromptOrigin", "classify", "classify_from_transcript",
    "classify_text", "last_human_text", "row_kind", "row_text",
]
