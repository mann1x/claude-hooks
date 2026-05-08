"""On-disk artifacts for a completed consultation.

Every consultation writes three files under
``<project>/.claude-hooks/consultants/<sid>/``:

- ``summary.md``   — synthesizer's final answer with YAML front-matter.
                      Caliber and OpenWolf both read YAML front-matter,
                      so this is the canonical "what did the council
                      conclude" artifact.
- ``transcript.md`` — full per-role conversation, headed by role and
                      round, tool calls inline.
- ``metadata.json`` — structured record (token usage, durations,
                      retries, exit reason). Machine-readable.

Writes are atomic: write to ``<file>.tmp`` then rename. This module
deliberately avoids any third-party dependency (no PyYAML) so it can
be exercised by the main claude-hooks test suite without installing
LangChain.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterable, Optional


SUMMARY_FILENAME = "summary.md"
TRANSCRIPT_FILENAME = "transcript.md"
METADATA_FILENAME = "metadata.json"
# v1.1: SQLite sidecar holding the full per-role LLM message threads
# (see docs/PLAN-consultants-v1.1-message-history.md). Written
# in real time by consultants.engine.recorder.MessageRecorder during
# the run; finalize() flips meta.status at completion. Optional —
# v1.0 sessions will never have one and the reopen path falls back
# cleanly to turn-content reconstruction.
TRANSCRIPT_DB_FILENAME = "transcript.db"


# ----------------------- types -------------------------------------- #

@dataclass
class RoleTurn:
    """One turn by one role. ``role`` is planner/researcher/critic/
    synthesizer; ``round`` is 1-indexed within that role (researcher
    can have multiple rounds when critic asks for more)."""
    role: str
    round: int
    content: str
    tool_calls: list[dict] = field(default_factory=list)
    tool_results: list[dict] = field(default_factory=list)
    duration_seconds: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class ConsultationResult:
    session_id: str
    created: str           # ISO 8601 with TZ offset, e.g. 2026-05-06T20:00:00+02:00
    question: str
    models: dict[str, str]      # role -> ollama tag
    topology: str               # "council"
    effort: str                 # "low" | "medium" | "high" | "max"
    final_answer: str           # synthesizer's output
    turns: list[RoleTurn]
    duration_seconds: float
    status: str                 # "completed" | "failed"
    error: Optional[str] = None
    cwd: Optional[str] = None
    # Cumulative token counters across the whole consultation.
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    # Per-role retry counts (cloud flaps).
    retries_by_role: dict[str, int] = field(default_factory=dict)
    # Live-session iteration: when set, this consultation is a
    # follow-up that reused parent_sid's plan + research + warm
    # ChatClients. The chain is reconstructable by walking
    # parent_sid pointers.
    parent_sid: Optional[str] = None


# ----------------------- YAML front-matter writer -------------------- #
# We write a tightly-bounded subset of YAML by hand to avoid pulling in
# PyYAML for the tests. Schema is fixed by ``ConsultationResult`` —
# strings (escaped), numbers, dicts of strings, no lists / nested
# structures other than the ``models`` mapping.

_QUOTE_NEEDED_RX = re.compile(r"[:#\n\"'\\\t\r]|^\s|\s$|^[-?{}\[\]&*!|>%@`]")


def _yaml_str(value: str) -> str:
    """Quote a string for YAML if necessary. Always uses double quotes
    when quoting; escapes ``\\`` and ``"``. Fence-pole question text
    that contains newlines or quotes goes through this safely."""
    if not value:
        return '""'
    if _QUOTE_NEEDED_RX.search(value):
        escaped = value.replace("\\", "\\\\").replace("\"", "\\\"")
        escaped = escaped.replace("\n", "\\n").replace("\r", "\\r")
        return f'"{escaped}"'
    return value


def _yaml_front_matter(result: ConsultationResult) -> str:
    lines: list[str] = ["---"]
    lines.append(f"session_id: {_yaml_str(result.session_id)}")
    lines.append(f"created: {_yaml_str(result.created)}")
    lines.append(f"question: {_yaml_str(result.question)}")
    lines.append("models:")
    for role in sorted(result.models):
        lines.append(f"  {role}: {_yaml_str(result.models[role])}")
    lines.append(f"topology: {_yaml_str(result.topology)}")
    lines.append(f"effort: {_yaml_str(result.effort)}")
    lines.append(f"duration_seconds: {result.duration_seconds:.2f}")
    lines.append(f"status: {_yaml_str(result.status)}")
    if result.error:
        lines.append(f"error: {_yaml_str(result.error)}")
    if result.cwd:
        lines.append(f"cwd: {_yaml_str(result.cwd)}")
    if result.parent_sid:
        lines.append(f"parent_sid: {_yaml_str(result.parent_sid)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def render_summary(result: ConsultationResult) -> str:
    """Return the full summary.md text (front-matter + body)."""
    body = result.final_answer.rstrip() + "\n"
    return _yaml_front_matter(result) + "\n" + body


def render_transcript(result: ConsultationResult) -> str:
    """Return the full transcript.md text. One ``## <Role> (round N)``
    header per turn, content then a horizontal rule between turns."""
    parts: list[str] = [f"# Consultation transcript — {result.session_id}\n"]
    parts.append(f"_Question_: {result.question}\n")
    parts.append("")
    for turn in result.turns:
        if turn.round > 1 or _is_multi_round(result.turns, turn.role):
            header = f"## {turn.role.title()} (round {turn.round})"
        else:
            header = f"## {turn.role.title()}"
        parts.append(header)
        parts.append("")
        parts.append(turn.content.rstrip() or "_(no textual output)_")
        if turn.tool_calls:
            parts.append("")
            parts.append("**Tool calls:**")
            for i, tc in enumerate(turn.tool_calls, 1):
                name = tc.get("name") or tc.get("function", {}).get("name", "?")
                args = tc.get("arguments") or tc.get("function", {}).get("arguments")
                if isinstance(args, dict):
                    args_str = json.dumps(args)
                else:
                    args_str = str(args) if args is not None else "{}"
                parts.append(f"{i}. `{name}({args_str})`")
        if turn.tool_results:
            parts.append("")
            parts.append("**Tool results:**")
            for i, tr in enumerate(turn.tool_results, 1):
                content = tr.get("content", "")
                snippet = (content[:400] + "…") if len(content) > 400 else content
                parts.append(f"{i}. ```\n{snippet}\n```")
        parts.append("")
        parts.append("---")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def _is_multi_round(turns: Iterable[RoleTurn], role: str) -> bool:
    """True if the same role appears more than once in turns."""
    return sum(1 for t in turns if t.role == role) > 1


def render_metadata(result: ConsultationResult) -> str:
    """Return the full metadata.json text (pretty-printed)."""
    payload = asdict(result)
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


# ----------------------- atomic write -------------------------------- #

def _atomic_write_text(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically.

    Uses the same dir as the target so the rename is filesystem-local
    (POSIX guarantees atomicity within one fs; on Windows, ``os.replace``
    is also atomic).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def session_dir(cwd: Path, session_id: str) -> Path:
    """Return the session directory under .claude-hooks/consultants/."""
    return cwd / ".claude-hooks" / "consultants" / session_id


def write_consultation(result: ConsultationResult, cwd: Path) -> Path:
    """Write summary.md, transcript.md, metadata.json under the session
    directory. Returns the directory path. Idempotent: re-writing the
    same result overwrites in place."""
    sdir = session_dir(cwd, result.session_id)
    _atomic_write_text(sdir / SUMMARY_FILENAME, render_summary(result))
    _atomic_write_text(sdir / TRANSCRIPT_FILENAME, render_transcript(result))
    _atomic_write_text(sdir / METADATA_FILENAME, render_metadata(result))
    return sdir
