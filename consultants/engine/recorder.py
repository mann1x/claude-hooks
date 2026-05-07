"""SQLite-backed message + event recorder for one consultation.

Phase 1 of the v1.1 message-history plan
(`docs/PLAN-consultants-v1.1-message-history.md`).

Each consultation gets a single `transcript.db` file living next to
`summary.md` / `transcript.md` / `metadata.json`. The recorder writes
LLM calls (request/response JSON), tool calls, and node enter/exit
events as they happen, in WAL mode so concurrent fan-out lanes don't
serialize on the writer.

Connections are NOT thread-safe in stdlib `sqlite3`, so every thread
that calls `record_*` gets its own connection from a `threading.local`
pool. The recorder owns close-time teardown.

Schema is versioned via `meta.schema_version`; v1 covers the council
topology with researcher fan-out. Future bumps either ALTER TABLE
within v1 or write a sidecar `transcript_v2.db`.

The recorder is intentionally decoupled from the rest of the engine:
it does not import council / graph / agent_loop, only `sqlite3` +
stdlib. That keeps it usable from tests without spinning up a graph.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


SCHEMA_VERSION = 1


_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS meta (
    schema_version       INTEGER NOT NULL DEFAULT 1,
    sid                  TEXT    NOT NULL,
    cwd                  TEXT    NOT NULL,
    question             TEXT    NOT NULL,
    effort               TEXT    NOT NULL,
    topology             TEXT    NOT NULL,
    parent_sid           TEXT,
    started_at           REAL    NOT NULL,
    finished_at          REAL,
    status               TEXT    NOT NULL,
    error                TEXT,
    subject_baseline_tag TEXT,
    models_json          TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    event_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                REAL NOT NULL,
    kind              TEXT NOT NULL,
    role              TEXT NOT NULL,
    round             INTEGER NOT NULL DEFAULT 1,
    lane_idx          INTEGER,
    model             TEXT,
    request_json      TEXT,
    response_json     TEXT,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    tool              TEXT,
    args              TEXT,
    output            TEXT,
    output_chars      INTEGER,
    duration_ms       INTEGER,
    error             TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_role_kind ON events(role, kind, round);
CREATE INDEX IF NOT EXISTS idx_events_ts        ON events(ts);
"""


_PRAGMAS = (
    "PRAGMA journal_mode = WAL;",
    "PRAGMA synchronous = NORMAL;",
    "PRAGMA foreign_keys = ON;",
    "PRAGMA temp_store = MEMORY;",
)


def _safe_dumps(obj: Any) -> Optional[str]:
    """`json.dumps` with `default=str` so non-serializable values
    (datetimes, custom classes that pass through us) get stringified
    instead of blowing up the recorder mid-run.

    Returns None when the input is None so we don't store the literal
    string ``"null"`` in NULLable columns.
    """
    if obj is None:
        return None
    try:
        return json.dumps(obj, default=str, ensure_ascii=False)
    except Exception as exc:  # pragma: no cover — last resort
        return json.dumps({"__recorder_dump_error__": str(exc)})


@dataclass
class RecorderMeta:
    """Header data the recorder writes into the `meta` table on first
    open. Mirrors the metadata.json header so the .db file is
    independently parseable without the JSON sibling."""

    sid: str
    cwd: str
    question: str
    effort: str
    topology: str
    models: dict[str, str]
    parent_sid: Optional[str] = None
    subject_baseline_tag: Optional[str] = None
    started_at: float = field(default_factory=time.time)


class MessageRecorder:
    """Per-consultation SQLite recorder. One instance per `sid`.

    Thread-safe via per-thread connection pool. The first thread that
    opens the file applies pragmas + schema + meta row; subsequent
    threads inherit the on-disk pragmas (WAL is sticky) and just
    insert into `events`.
    """

    def __init__(self, db_path: Path | str, *, meta: RecorderMeta) -> None:
        self.db_path = Path(db_path)
        self._meta = meta
        self._tls = threading.local()
        self._lock = threading.Lock()
        self._all_conns: list[sqlite3.Connection] = []
        self._closed = False
        self._meta_written = False

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # Open the primary connection eagerly so schema + meta land
        # before any worker thread races to write events. The eager
        # open also surfaces any path/permissions error at construction
        # time rather than at the first record_* call.
        self._init_db()

    # ------------------------------------------------------------------ #
    # Connection management
    # ------------------------------------------------------------------ #

    def _new_conn(self) -> sqlite3.Connection:
        # ``check_same_thread=False`` lets the finalizer thread close
        # peer threads' connections at teardown. We still hand each
        # thread its own connection for writes (the safety property
        # the same-thread default exists to enforce); we only need
        # cross-thread access for ``close()`` so wal_checkpoint(
        # TRUNCATE) can actually shrink the WAL. Without this flag,
        # ``conn.close()`` raises sqlite3.ProgrammingError on a
        # cross-thread call, the close fails silently inside
        # finalize's except guard, and the WAL stays at high-water
        # mark.
        conn = sqlite3.connect(
            str(self.db_path), timeout=30.0, check_same_thread=False,
        )
        for pragma in _PRAGMAS:
            conn.execute(pragma)
        with self._lock:
            self._all_conns.append(conn)
        return conn

    def _conn(self) -> sqlite3.Connection:
        if self._closed:
            raise RuntimeError(f"recorder for {self._meta.sid} is closed")
        conn = getattr(self._tls, "conn", None)
        if conn is None:
            conn = self._new_conn()
            self._tls.conn = conn
        return conn

    def _init_db(self) -> None:
        conn = self._conn()
        conn.executescript(_SCHEMA_SQL)
        # Idempotent meta row: if the file already exists from an earlier
        # run with the same sid (shouldn't happen in practice but might in
        # tests), keep the original started_at so duration math works.
        existing = conn.execute(
            "SELECT 1 FROM meta WHERE sid = ?", (self._meta.sid,)
        ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO meta (
                    schema_version, sid, cwd, question, effort, topology,
                    parent_sid, started_at, finished_at, status, error,
                    subject_baseline_tag, models_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?)
                """,
                (
                    SCHEMA_VERSION,
                    self._meta.sid,
                    self._meta.cwd,
                    self._meta.question,
                    self._meta.effort,
                    self._meta.topology,
                    self._meta.parent_sid,
                    self._meta.started_at,
                    "running",
                    self._meta.subject_baseline_tag,
                    _safe_dumps(self._meta.models) or "{}",
                ),
            )
            conn.commit()
        self._meta_written = True

    # ------------------------------------------------------------------ #
    # Recording API — fan-in from council nodes + agent_loop callbacks
    # ------------------------------------------------------------------ #

    def record_llm(
        self,
        *,
        role: str,
        round: int = 1,
        lane_idx: Optional[int] = None,
        model: Optional[str] = None,
        request: Optional[dict] = None,
        response: Optional[dict] = None,
        prompt_tokens: Optional[int] = None,
        completion_tokens: Optional[int] = None,
        duration_ms: Optional[int] = None,
        error: Optional[str] = None,
    ) -> None:
        """Append one LLM call event. `request` and `response` are
        full payloads — the recorder JSON-encodes them with default=str
        so non-serializable values don't sink the run."""
        if self._closed:
            return
        conn = self._conn()
        conn.execute(
            """
            INSERT INTO events (
                ts, kind, role, round, lane_idx,
                model, request_json, response_json,
                prompt_tokens, completion_tokens,
                duration_ms, error
            ) VALUES (?, 'llm_call', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                time.time(),
                role,
                round,
                lane_idx,
                model,
                _safe_dumps(request),
                _safe_dumps(response),
                prompt_tokens,
                completion_tokens,
                duration_ms,
                error,
            ),
        )
        conn.commit()

    def record_tool(
        self,
        *,
        role: str,
        round: int = 1,
        lane_idx: Optional[int] = None,
        tool: str,
        args: Optional[str] = None,
        output: Optional[str] = None,
        duration_ms: Optional[int] = None,
        error: Optional[str] = None,
    ) -> None:
        """Append one tool execution event. `args` / `output` are
        stored as raw text (they're already strings on the agent_loop
        side); we record `output_chars` separately so summary queries
        can SUM the volume without re-reading the blobs."""
        if self._closed:
            return
        conn = self._conn()
        out_chars = len(output) if isinstance(output, str) else None
        conn.execute(
            """
            INSERT INTO events (
                ts, kind, role, round, lane_idx,
                tool, args, output, output_chars,
                duration_ms, error
            ) VALUES (?, 'tool_call', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                time.time(),
                role,
                round,
                lane_idx,
                tool,
                args,
                output,
                out_chars,
                duration_ms,
                error,
            ),
        )
        conn.commit()

    def record_node(
        self,
        *,
        role: str,
        kind: str,  # 'node_enter' | 'node_exit'
        round: int = 1,
        lane_idx: Optional[int] = None,
        duration_ms: Optional[int] = None,
        error: Optional[str] = None,
    ) -> None:
        """Append a node-boundary event. Cheap to skip if a caller
        doesn't care; debugging tools query these to render the graph
        timeline."""
        if self._closed:
            return
        if kind not in ("node_enter", "node_exit"):
            raise ValueError(f"node kind must be node_enter|node_exit, got {kind!r}")
        conn = self._conn()
        conn.execute(
            """
            INSERT INTO events (
                ts, kind, role, round, lane_idx,
                duration_ms, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (time.time(), kind, role, round, lane_idx, duration_ms, error),
        )
        conn.commit()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def finalize(
        self,
        *,
        status: str,
        finished_at: Optional[float] = None,
        error: Optional[str] = None,
    ) -> None:
        """Mark the consultation terminal in `meta` and shrink the WAL
        log via `wal_checkpoint(TRUNCATE)` + `VACUUM`. Safe to call
        multiple times — second call updates the row in place."""
        if self._closed:
            return
        if status not in ("completed", "failed", "cancelled"):
            raise ValueError(f"unexpected terminal status: {status!r}")
        conn = self._conn()
        conn.execute(
            "UPDATE meta SET status = ?, finished_at = ?, error = ? WHERE sid = ?",
            (status, finished_at if finished_at is not None else time.time(), error, self._meta.sid),
        )
        conn.commit()
        # Drop every peer connection before checkpointing. SQLite's
        # ``wal_checkpoint(TRUNCATE)`` silently downgrades to PASSIVE
        # mode (folds pages but leaves the WAL file at high-water
        # mark) when ANY other connection — even an idle one from a
        # fan-out writer thread — is still attached. We hold N
        # writer-thread conns in ``_all_conns``; close all but the
        # finalizing one so TRUNCATE actually shrinks the WAL.
        with self._lock:
            for c in list(self._all_conns):
                if c is not conn:
                    try:
                        c.close()
                    except Exception:
                        pass
            self._all_conns = [conn]
        # Invalidate other threads' TLS refs — they'll get a fresh
        # conn from ``_conn()`` if they ever record again (they
        # shouldn't; finalize is the terminal call).
        try:
            del self._tls.conn
        except AttributeError:
            pass
        self._tls.conn = conn  # the finalizer's TLS slot points at the live conn
        # VACUUM first to compact the main DB, then
        # ``wal_checkpoint(TRUNCATE)`` to shrink the WAL to zero.
        # Order matters: in WAL mode VACUUM writes its compaction
        # output through the WAL, so a TRUNCATE before VACUUM gets
        # immediately re-filled (~100 KB on a 4-lane fan-out).
        # Doing the TRUNCATE last leaves both files at minimum size.
        # Both calls are cheap (~50 ms total) and only run once per
        # consultation.
        try:
            conn.execute("VACUUM;")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        except sqlite3.OperationalError:
            # VACUUM can fail if another connection has an open
            # transaction. Not worth retrying — meta is already
            # committed; the WAL will get checkpointed on next open.
            pass

    def close(self) -> None:
        """Close every per-thread connection. Idempotent."""
        if self._closed:
            return
        self._closed = True
        with self._lock:
            for conn in self._all_conns:
                try:
                    conn.close()
                except Exception:
                    pass
            self._all_conns.clear()
        # Drop the per-thread reference too so a stale TLS slot
        # doesn't outlive the recorder.
        try:
            del self._tls.conn
        except AttributeError:
            pass

    # Context-manager sugar so callers can do
    #   with MessageRecorder(...) as rec: ...
    # and get finalize+close on the way out. Status defaults to
    # 'completed' on clean exit, 'failed' if an exception escaped.
    def __enter__(self) -> "MessageRecorder":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.finalize(status="completed")
        else:
            self.finalize(status="failed", error=f"{exc_type.__name__}: {exc}")
        self.close()
