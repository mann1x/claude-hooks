"""The engine's index of councils that have not finished.

A council lives in two places: its checkpoint (``checkpoints.db`` in the
session dir), which holds the graph's state, and the request that
started it, which nothing else records in a form a runner can be rebuilt
from. This index holds the second, one record per running council, so
an engine that starts can find what the last one left unfinished —
whether it was shut down cleanly (records marked ``suspended``) or
killed (records still marked ``running``).

A record is added when a run is submitted and removed when it reaches a
terminal status. Anything still here at startup is resumed.

File: ``~/.claude/consultants-inflight.json`` (``CONSULTANTS_INFLIGHT_PATH``
overrides it, which the tests use). Written atomically under a lock; the
engine is the only writer.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("consultants.server.inflight")

#: A record that has already been resumed this many times is given up
#: on instead of resumed again: a council that crashes the engine on
#: every resume must not turn a restart into a crash loop.
MAX_RESUMES = 3

_LOCK = threading.Lock()


def index_path() -> Path:
    override = os.environ.get("CONSULTANTS_INFLIGHT_PATH")
    if override:
        return Path(override)
    return Path.home() / ".claude" / "consultants-inflight.json"


def _read() -> dict[str, dict]:
    path = index_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("in-flight index %s unreadable (%s); treating as empty",
                    path, exc)
        return {}
    runs = raw.get("runs") if isinstance(raw, dict) else None
    return {k: v for k, v in (runs or {}).items() if isinstance(v, dict)}


def _write(runs: dict[str, dict]) -> None:
    path = index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".inflight-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "runs": runs}, f, indent=1,
                      sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def register(record: dict) -> None:
    """Add (or replace) the record for ``record["sid"]``."""
    sid = record["sid"]
    rec = dict(record)
    rec.setdefault("state", "running")
    rec.setdefault("resumes", 0)
    rec.setdefault("registered_at", time.time())
    try:
        with _LOCK:
            runs = _read()
            runs[sid] = rec
            _write(runs)
    except OSError:
        log.exception("could not record in-flight run %s; it will not "
                      "survive an engine restart", sid)


def update(sid: str, **fields) -> None:
    try:
        with _LOCK:
            runs = _read()
            if sid not in runs:
                return
            runs[sid].update(fields)
            _write(runs)
    except OSError:
        log.exception("could not update in-flight record %s", sid)


def remove(sid: str) -> None:
    try:
        with _LOCK:
            runs = _read()
            if runs.pop(sid, None) is None:
                return
            _write(runs)
    except OSError:
        log.exception("could not drop in-flight record %s", sid)


def load() -> list[dict]:
    """Every record, oldest first."""
    with _LOCK:
        runs = _read()
    return sorted(runs.values(), key=lambda r: r.get("registered_at", 0))


def get(sid: str) -> Optional[dict]:
    with _LOCK:
        return _read().get(sid)


__all__ = ["MAX_RESUMES", "get", "index_path", "load", "register",
           "remove", "update"]
