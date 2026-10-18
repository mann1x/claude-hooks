"""A host-wide index of councils, for the dashboard.

Each council's record lives in its project, at
``<cwd>/.claude-hooks/consultants/<sid>/`` (``transcript.db``,
``summary.md``, ``metadata.json``), and ``/v1/sessions`` lists one
project at a time. Nothing on the host listed every council. This
index does, at ``~/.claude/consultants-councils.db``
(``CONSULTANTS_COUNCIL_INDEX`` overrides it, and ``tests/conftest.py``
isolates it per test):

- :func:`record` is called with every in-flight registration (consult,
  follow-up, retry-lanes and the resubmit of a resume): one row per
  sid, upserted.
- :func:`finish` is called when the run settles, with its final status.
- :func:`backfill` finds councils that ran before the index existed by
  scanning the session stores of every project Claude Code knows (the
  ``projects`` keys of ``~/.claude.json``) plus any roots given. It
  reads only ``metadata.json`` / ``summary.md`` / ``transcript.db``
  ``meta``, never writes to a project.

The index is a pointer, never the record: a row whose session directory
is gone is reported as ``missing`` by :func:`list_councils`, and every
detail is read from the project's own files.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger("consultants.server.council_index")

SCHEMA = """
CREATE TABLE IF NOT EXISTS councils (
    sid          TEXT PRIMARY KEY,
    kind         TEXT,
    cwd          TEXT NOT NULL,
    question     TEXT,
    effort       TEXT,
    root_sid     TEXT,
    parent_sid   TEXT,
    shell        TEXT,
    status       TEXT,
    started_at   REAL,
    finished_at  REAL,
    updated_at   REAL
);
CREATE INDEX IF NOT EXISTS idx_councils_started ON councils(started_at);
CREATE INDEX IF NOT EXISTS idx_councils_root    ON councils(root_sid);
"""
QUESTION_MAX_CHARS = 2000
_LOCK = threading.Lock()


def index_path() -> Path:
    override = os.environ.get("CONSULTANTS_COUNCIL_INDEX")
    return Path(override) if override else Path(
        os.path.expanduser("~/.claude/consultants-councils.db"))


def session_dir(cwd: str, sid: str) -> Path:
    return Path(cwd) / ".claude-hooks" / "consultants" / sid


def _connect(path: Optional[Path] = None) -> sqlite3.Connection:
    p = path or index_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p), timeout=10)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def record(rec: dict) -> None:
    """Upsert one council from an in-flight record. Never raises."""
    sid = rec.get("sid")
    if not sid or not rec.get("cwd"):
        return
    shell = rec.get("shell")
    row = {
        "sid": sid, "kind": rec.get("kind"), "cwd": rec.get("cwd"),
        "question": (rec.get("question") or "")[:QUESTION_MAX_CHARS],
        "effort": rec.get("effort"), "root_sid": rec.get("root_sid") or sid,
        "parent_sid": rec.get("parent_sid"),
        "shell": json.dumps(shell) if shell else None,
        "started_at": rec.get("started_at") or time.time(),
        "now": time.time(),
    }
    try:
        with _LOCK, _connect() as con:
            con.execute(
                "INSERT INTO councils (sid, kind, cwd, question, effort, "
                "root_sid, parent_sid, shell, status, started_at, updated_at) "
                "VALUES (:sid, :kind, :cwd, :question, :effort, :root_sid, "
                ":parent_sid, :shell, 'running', :started_at, :now) "
                "ON CONFLICT(sid) DO UPDATE SET status = 'running', "
                "finished_at = NULL, updated_at = :now, "
                "kind = COALESCE(excluded.kind, kind), "
                "effort = COALESCE(excluded.effort, effort), "
                "shell = COALESCE(excluded.shell, shell)", row)
    except sqlite3.Error:
        log.exception("council index: record %s failed", sid)


def finish(sid: str, status: str, *, at: Optional[float] = None) -> None:
    """Mark a council settled (``completed`` / ``failed`` / ``suspended``
    / ``cancelled`` …). Never raises."""
    if not sid:
        return
    at = time.time() if at is None else at
    try:
        with _LOCK, _connect() as con:
            con.execute(
                "UPDATE councils SET status = ?, updated_at = ?, "
                "finished_at = CASE WHEN ? = 'suspended' THEN NULL ELSE ? END "
                "WHERE sid = ?", (status, at, status, at, sid))
    except sqlite3.Error:
        log.exception("council index: finish %s failed", sid)


def list_councils(*, limit: int = 200, status: Optional[str] = None,
                  query: Optional[str] = None,
                  cwd: Optional[str] = None) -> list[dict]:
    """Newest first. ``query`` matches the question or the sid."""
    sql = "SELECT * FROM councils"
    where, args = [], []
    if status:
        where.append("status = ?")
        args.append(status)
    if cwd:
        where.append("cwd = ?")
        args.append(cwd)
    if query:
        where.append("(question LIKE ? OR sid LIKE ?)")
        args += [f"%{query}%", f"%{query}%"]
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY started_at DESC LIMIT ?"
    args.append(int(limit))
    try:
        with _LOCK, _connect() as con:
            rows = [dict(r) for r in con.execute(sql, args)]
    except sqlite3.Error:
        log.exception("council index: list failed")
        return []
    for r in rows:
        r["shell"] = json.loads(r["shell"]) if r.get("shell") else None
        r["missing"] = not session_dir(r["cwd"], r["sid"]).is_dir()
    return rows


def get(sid: str) -> Optional[dict]:
    try:
        with _LOCK, _connect() as con:
            r = con.execute("SELECT * FROM councils WHERE sid = ?",
                            (sid,)).fetchone()
    except sqlite3.Error:
        return None
    if r is None:
        return None
    d = dict(r)
    d["shell"] = json.loads(d["shell"]) if d.get("shell") else None
    return d


# ------------------------------------------------------------ backfill
def known_project_roots() -> list[str]:
    """Projects councils are known to run in: the ones Claude Code has
    opened (``~/.claude.json``), and those named by in-flight councils
    and sandbox views. A council run with ``--cwd`` elsewhere is found
    by :func:`find_stores`."""
    roots: list[str] = []
    try:
        data = json.loads(Path(os.path.expanduser("~/.claude.json"))
                          .read_text(encoding="utf-8"))
        roots += list(data.get("projects") or {})
    except (OSError, ValueError):
        pass
    try:
        from consultants.server import inflight
        roots += [r.get("cwd") or "" for r in inflight.load()]
    except Exception:  # pragma: no cover — defensive
        pass
    try:
        from claude_hooks.sandbox.views import registered
        roots += [r.get("repo") or "" for r in registered()]
    except Exception:  # pragma: no cover — defensive
        pass
    return [p for p in dict.fromkeys(roots) if p and os.path.isdir(p)]


#: Directories a store scan never descends into.
SCAN_SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__",
             ".cache", "site-packages", ".tox", "target", "dist", "build",
             "proc", "sys", "dev", "run", "tmp"}


def find_stores(bases: Iterable[str], *, max_depth: int = 5) -> list[str]:
    """Project roots under ``bases`` (``max_depth`` levels down) that hold
    a council store, for :func:`backfill`. Symlinks are not followed."""
    found: list[str] = []
    for base in bases:
        stack = [(os.path.realpath(base), 0)]
        while stack:
            d, depth = stack.pop()
            if os.path.isdir(os.path.join(d, ".claude-hooks", "consultants")):
                found.append(d)
            if depth >= max_depth:
                continue
            try:
                with os.scandir(d) as it:
                    for e in it:
                        if e.name in SCAN_SKIP or e.name == ".claude-hooks":
                            continue
                        try:
                            if e.is_dir(follow_symlinks=False):
                                stack.append((e.path, depth + 1))
                        except OSError:
                            pass
            except OSError:
                pass
    return list(dict.fromkeys(found))


def _status_from_disk(sdir: Path, meta: dict) -> str:
    if meta.get("status"):
        return str(meta["status"])
    if (sdir / "summary.md").is_file():
        return "completed"
    return "unknown"


def _read_meta(sdir: Path) -> dict:
    meta: dict = {}
    try:
        meta = json.loads((sdir / "metadata.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    db = sdir / "transcript.db"
    if db.is_file():
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
            try:
                cols = [c[1] for c in con.execute("PRAGMA table_info(meta)")]
                row = con.execute("SELECT * FROM meta LIMIT 1").fetchone()
                if row:
                    for k, v in zip(cols, row):
                        meta.setdefault(k, v)
                first = con.execute("SELECT MIN(ts), MAX(ts) FROM events"
                                    ).fetchone()
                if first and first[0]:
                    meta.setdefault("_first_ts", first[0])
                    meta.setdefault("_last_ts", first[1])
            finally:
                con.close()
        except sqlite3.Error:
            pass
    return meta


#: Statuses of a council some engine is (or will be) running.
LIVE_STATUSES = ("running", "paused", "awaiting_tool", "awaiting_adversary",
                 "suspended", "queued")


def _insert_settled(row: dict, status: str, at: float,
                    path: Optional[Path]) -> None:
    """Add a council already settled, in one statement; an existing row
    is never touched (2026-10-18: a backfill's ``record()`` upsert set 60
    finished councils back to ``running``)."""
    try:
        with _LOCK, _connect(path) as con:
            con.execute(
                "INSERT OR IGNORE INTO councils (sid, kind, cwd, question, "
                "effort, root_sid, parent_sid, shell, status, started_at, "
                "finished_at, updated_at) VALUES (:sid, :kind, :cwd, "
                ":question, :effort, :root_sid, :parent_sid, NULL, :status, "
                ":started_at, :finished_at, :now)",
                {**row, "question": (row.get("question") or "")[:QUESTION_MAX_CHARS],
                 "root_sid": row.get("root_sid") or row["sid"],
                 "status": status, "now": time.time(),
                 "finished_at": None if status in LIVE_STATUSES else at})
    except sqlite3.Error:
        log.exception("council index: backfill of %s failed", row.get("sid"))


def disk_status(cwd: str, sid: str) -> tuple[str, Optional[float]]:
    """``(status, finished_at)`` from the council's own record on disk;
    ``("unknown", None)`` when it has none."""
    sdir = session_dir(cwd, sid)
    if not sdir.is_dir():
        return "unknown", None
    meta = _read_meta(sdir)
    st = _status_from_disk(sdir, meta)
    at = meta.get("finished_at") or meta.get("_last_ts")
    return st, (float(at) if at else None)


def settle_stale(live: Iterable[str]) -> int:
    """Rows marked running whose council no engine runs or will resume
    (``live``: the in-flight sids) take their status from disk when it is
    settled there. Run at engine start, after the resumes. Returns how
    many changed."""
    live = set(live)
    path = index_path()
    try:
        with _LOCK, _connect(path) as con:
            rows = [dict(r) for r in con.execute(
                "SELECT sid, cwd FROM councils WHERE status IN (%s)"
                % ",".join("?" * len(LIVE_STATUSES)), LIVE_STATUSES)]
    except sqlite3.Error:
        log.exception("council index: stale scan failed")
        return 0
    n = 0
    for r in rows:
        if r["sid"] in live:
            continue
        st, at = disk_status(r["cwd"], r["sid"])
        if st in LIVE_STATUSES or st == "unknown":
            continue
        try:
            with _LOCK, _connect(path) as con:
                con.execute("UPDATE councils SET status = ?, finished_at = ?, "
                            "updated_at = ? WHERE sid = ?",
                            (st, at or time.time(), time.time(), r["sid"]))
            n += 1
        except sqlite3.Error:
            log.exception("council index: settling %s failed", r["sid"])
    return n


def backfill(roots: Iterable[str] = (), *, dry_run: bool = False) -> int:
    """Index the councils found in each root's session store that the
    index does not have yet. Returns how many were added. The index is
    resolved once, and a row already there is never written."""
    path = index_path()
    try:
        with _LOCK, _connect(path) as con:
            seen = {r[0] for r in con.execute("SELECT sid FROM councils")}
    except sqlite3.Error:
        log.exception("council index: backfill could not read the index")
        return 0
    added = 0
    for root in dict.fromkeys([*known_project_roots(), *roots]):
        store = Path(root) / ".claude-hooks" / "consultants"
        try:
            entries = sorted(p for p in store.iterdir() if p.is_dir())
        except OSError:
            continue
        for sdir in entries:
            sid = sdir.name
            if sid in seen or not sid.startswith("csl-"):
                continue
            if not ((sdir / "transcript.db").is_file()
                    or (sdir / "metadata.json").is_file()):
                continue
            meta = _read_meta(sdir)
            started = (meta.get("started_at") or meta.get("created_at")
                       or meta.get("_first_ts") or sdir.stat().st_mtime)
            row = {
                "sid": sid, "kind": meta.get("kind") or (
                    "follow-up" if meta.get("parent_sid") else "consult"),
                "cwd": str(Path(root).resolve()),
                "question": meta.get("question") or "",
                "effort": meta.get("effort"),
                "root_sid": meta.get("root_sid"),
                "parent_sid": meta.get("parent_sid"),
                "started_at": float(started),
            }
            seen.add(sid)
            added += 1
            if dry_run:
                continue
            _insert_settled(
                row, _status_from_disk(sdir, meta),
                float(meta.get("finished_at") or meta.get("_last_ts") or started),
                path)
    return added


__all__ = ["record", "finish", "list_councils", "get", "backfill",
           "settle_stale", "disk_status", "LIVE_STATUSES",
           "index_path", "session_dir", "known_project_roots", "find_stores"]
