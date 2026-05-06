"""Per-project sessions index for fast ``/consultants--list``.

Maintains ``<project>/.claude-hooks/consultants/sessions.json`` as an
append-only ordered list of session metadata. The full
``metadata.json`` per session is the source of truth; this index just
exists so ``--list`` doesn't have to scan and parse every session
directory.

Schema::

    {
      "version": 1,
      "sessions": [
        {
          "session_id": "advice-...",
          "created": "2026-05-06T20:00:00+02:00",
          "question": "first 200 chars",
          "topology": "council",
          "effort": "medium",
          "status": "completed",
          "duration_seconds": 287.4
        },
        ...
      ]
    }

Atomic writes via tempfile + rename. Stdlib only.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional


SESSIONS_INDEX_FILENAME = "sessions.json"
INDEX_VERSION = 1
QUESTION_PREVIEW_LIMIT = 200


@dataclass
class SessionEntry:
    session_id: str
    created: str
    question: str               # truncated to QUESTION_PREVIEW_LIMIT chars
    topology: str
    effort: str
    status: str
    duration_seconds: float


def index_path(cwd: Path) -> Path:
    return cwd / ".claude-hooks" / "consultants" / SESSIONS_INDEX_FILENAME


def load_index(cwd: Path) -> list[SessionEntry]:
    p = index_path(cwd)
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, dict):
        return []
    sessions = raw.get("sessions") or []
    out: list[SessionEntry] = []
    for s in sessions:
        if not isinstance(s, dict):
            continue
        try:
            out.append(SessionEntry(
                session_id=str(s["session_id"]),
                created=str(s.get("created", "")),
                question=str(s.get("question", "")),
                topology=str(s.get("topology", "council")),
                effort=str(s.get("effort", "medium")),
                status=str(s.get("status", "unknown")),
                duration_seconds=float(s.get("duration_seconds", 0.0)),
            ))
        except (KeyError, ValueError, TypeError):
            continue
    return out


def _atomic_write(path: Path, content: str) -> None:
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


def _save_index(cwd: Path, entries: list[SessionEntry]) -> None:
    payload = {
        "version": INDEX_VERSION,
        "sessions": [asdict(e) for e in entries],
    }
    _atomic_write(
        index_path(cwd),
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )


def append(cwd: Path, entry: SessionEntry) -> None:
    """Add an entry. If a session with the same id is already in the
    index, replace it (handles re-runs / status updates)."""
    entries = load_index(cwd)
    truncated_q = entry.question[:QUESTION_PREVIEW_LIMIT]
    fixed = SessionEntry(
        session_id=entry.session_id,
        created=entry.created,
        question=truncated_q,
        topology=entry.topology,
        effort=entry.effort,
        status=entry.status,
        duration_seconds=entry.duration_seconds,
    )
    replaced = False
    for i, e in enumerate(entries):
        if e.session_id == fixed.session_id:
            entries[i] = fixed
            replaced = True
            break
    if not replaced:
        entries.append(fixed)
    _save_index(cwd, entries)


def get(cwd: Path, session_id: str) -> Optional[SessionEntry]:
    for e in load_index(cwd):
        if e.session_id == session_id:
            return e
    return None


def list_recent(cwd: Path, limit: int = 50) -> list[SessionEntry]:
    """Return the most recent ``limit`` entries (last in file = most
    recent). Sort key is the file order, not the timestamp — append()
    is the only writer and it's monotonically increasing in practice."""
    entries = load_index(cwd)
    if limit <= 0:
        return list(entries)
    return list(entries[-limit:][::-1])
