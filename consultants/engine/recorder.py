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
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


SCHEMA_VERSION = 2


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

-- v2 (M4): typed CouncilEvent stream. Separate table from
-- ``events`` so the existing LLM/tool/node-boundary rows stay
-- untouched and old post-mortem tooling keeps working. The SSE
-- bridge reads from this table for ``Last-Event-ID`` resume.
CREATE TABLE IF NOT EXISTS runtime_events (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    kind        TEXT    NOT NULL,
    role        TEXT,
    round       INTEGER,
    lane_idx    INTEGER,
    payload     TEXT    NOT NULL  -- json blob of the full event dict
);

CREATE INDEX IF NOT EXISTS idx_runtime_events_kind ON runtime_events(kind);
CREATE INDEX IF NOT EXISTS idx_runtime_events_ts   ON runtime_events(ts);
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
        # Truncation tally. Every role's every LLM call — single-shot
        # and agent-loop iteration alike — passes through record_llm,
        # which makes this the one place that sees them all. Guarded by
        # ``_lock`` because council lanes record from worker threads.
        self._truncations_by_role: dict[str, int] = {}
        self._truncations: list[dict] = []
        # Whether each role's *most recent* completion was cut. This is
        # the question that decides pass/fail, and it is a different
        # question from the tally above: a cut completion that the
        # continuation loop then finished is a fact worth recording and
        # a poor reason to fail a 40-minute council. Only an unrecovered
        # cut — the role's last word, still mid-sentence — is a failure.
        self._last_call_truncated: dict[str, bool] = {}

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
        so non-serializable values don't sink the run.

        Also auto-emits a narrow mirror row into ``runtime_events`` so
        the M9 SSE bridge has a coherent timeline without the engine
        having to call ``record_runtime_event`` separately (#214).
        """
        if self._closed:
            return
        now = time.time()
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
                now,
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
        trunc = self._note_truncation(
            response, role=role, round=round, lane_idx=lane_idx,
            model=model,
        )
        # Narrow mirror for SSE consumers.
        self._emit_runtime_event(
            kind="llm_call",
            role=role,
            round=round,
            lane_idx=lane_idx,
            payload={
                "ts": now,
                "model": model,
                "duration_ms": duration_ms,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "error": error,
                "truncated": trunc.kind if trunc else None,
            },
        )

    # ------------------------------------------------------------------ #
    # Truncation tally (2026-08-12)
    # ------------------------------------------------------------------ #

    def _note_truncation(self, response, *, role: str, round: int,
                         lane_idx, model):
        """Classify one response and tally it if it was cut short.

        Placed in ``record_llm`` rather than at the role nodes because
        this is the only function every role's every call reaches —
        single-shot roles, agent-loop iterations, and the tooled
        ``tool_executor``/``coder`` lanes all funnel here. Wiring the
        check at the nodes would mean N chances to forget one, and the
        incident this exists to prevent was precisely a role whose
        truncation nobody was looking for.

        Never raises: a detector that can sink a run is worse than the
        blindness it replaces.
        """
        try:
            from claude_hooks.truncation import classify
            trunc = classify(response, role=role, model=model or "")
        except Exception:  # pragma: no cover — detector must not mask
            log.exception("truncation.classify raised; ignored")
            return None
        if trunc is None:
            with self._lock:
                self._last_call_truncated[role] = False
            return None
        with self._lock:
            self._truncations_by_role[role] = (
                self._truncations_by_role.get(role, 0) + 1)
            self._last_call_truncated[role] = True
            entry = trunc.public_dict()
            entry.update({"round": round, "lane_idx": lane_idx})
            self._truncations.append(entry)
        log.warning(
            "TRUNCATED completion: role=%s model=%s round=%s lane=%s "
            "kind=%s — %s",
            role, model, round, lane_idx, trunc.kind, trunc.detail,
        )
        try:
            self._emit_runtime_event(
                kind="truncation", role=role, round=round,
                lane_idx=lane_idx, payload=entry,
            )
        except Exception:  # pragma: no cover
            log.exception("truncation runtime event failed; ignored")
        return trunc

    def truncations_by_role(self) -> dict[str, int]:
        """Per-role count of completions detected as cut short."""
        with self._lock:
            return dict(self._truncations_by_role)

    def truncations(self) -> list[dict]:
        """Full detail of every truncation seen, in observation order."""
        with self._lock:
            return list(self._truncations)

    def unrecovered_truncations(self) -> list[str]:
        """Roles whose most recent completion was still cut short —
        i.e. the continuation loop ran out of attempts, or never ran.
        These are the ones that made it into the deliverable."""
        with self._lock:
            return sorted(r for r, cut in self._last_call_truncated.items()
                          if cut)

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
        can SUM the volume without re-reading the blobs.

        Also auto-emits a narrow mirror row into ``runtime_events``
        for SSE consumers (#214). The narrow payload omits the full
        ``args`` / ``output`` blobs (they live in the audit log) and
        keeps just the tool name, output volume, duration, and any
        error.
        """
        if self._closed:
            return
        now = time.time()
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
                now,
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
        self._emit_runtime_event(
            kind="tool_call",
            role=role,
            round=round,
            lane_idx=lane_idx,
            payload={
                "ts": now,
                "tool": tool,
                "output_chars": out_chars,
                "duration_ms": duration_ms,
                "error": error,
            },
        )

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
        timeline.

        Also auto-emits a narrow mirror row into ``runtime_events`` so
        the M9 SSE bridge has a coherent timeline (#214). For
        ``node_enter`` an INFO log line is also emitted so ops
        watching the daemon log can see role transitions without
        querying the DB.
        """
        if self._closed:
            return
        if kind not in ("node_enter", "node_exit"):
            raise ValueError(f"node kind must be node_enter|node_exit, got {kind!r}")
        now = time.time()
        conn = self._conn()
        conn.execute(
            """
            INSERT INTO events (
                ts, kind, role, round, lane_idx,
                duration_ms, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (now, kind, role, round, lane_idx, duration_ms, error),
        )
        conn.commit()
        # M9 mirror for SSE consumers.
        self._emit_runtime_event(
            kind=kind,
            role=role,
            round=round,
            lane_idx=lane_idx,
            payload={
                "ts": now,
                "duration_ms": duration_ms,
                "error": error,
            },
        )
        # Per-role INFO log on node_enter — single source of truth
        # for ops watching the daemon log (#214 Fix D).
        if kind == "node_enter":
            lane_tag = "" if lane_idx is None else f" lane={lane_idx}"
            log.info(
                "consultants: node_enter role=%s round=%d%s",
                role, round, lane_tag,
            )

    def _emit_runtime_event(
        self,
        *,
        kind: str,
        role: Optional[str],
        round: Optional[int],
        lane_idx: Optional[int],
        payload: Optional[dict[str, Any]] = None,
    ) -> None:
        """Best-effort narrow mirror to ``runtime_events``.

        Used by ``record_node`` / ``record_llm`` / ``record_tool`` to
        emit a coherent timeline for the M9 SSE bridge. Failures are
        logged + swallowed — the audit-log writes (events table) are
        the source of truth and must succeed; SSE timeline rows are
        observability and best-effort.
        """
        try:
            self.record_event(
                kind=kind, role=role, round=round,
                lane_idx=lane_idx, payload=payload,
            )
        except Exception:  # pragma: no cover — defensive
            log.exception(
                "recorder._emit_runtime_event failed; SSE will miss "
                "kind=%s role=%s round=%s lane_idx=%s",
                kind, role, round, lane_idx,
            )

    def record_event(
        self,
        *,
        kind: str,
        role: Optional[str] = None,
        round: Optional[int] = None,
        lane_idx: Optional[int] = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> None:
        """Append a typed CouncilEvent row to ``runtime_events``.

        The M3 stall layer's ``on_event`` sink and the M4 SSE
        bridge both call this. Schema is intentionally narrow:
        the ``payload`` JSON column carries the full event dict so
        new event types don't need a schema migration. The
        indexed top-level columns (kind, role, round, lane_idx)
        let post-mortem queries filter without parsing JSON.

        ``ts`` is read from ``payload["ts"]`` when present; falls
        back to ``time.time()``. This keeps the wall-clock the
        emitter saw consistent with what the consumer sees, even
        if the recorder write is slightly delayed.

        No-op when the recorder is closed (the same pattern as
        ``record_llm`` / ``record_tool`` / ``record_node``).
        """
        if self._closed:
            return
        if not kind:
            raise ValueError("record_event: kind is required")
        payload_dict = dict(payload or {})
        ts = float(payload_dict.get("ts") or time.time())
        # Belt and braces: ensure 'kind' is in the payload so a
        # consumer reading just the JSON blob doesn't have to
        # cross-reference the row's ``kind`` column.
        payload_dict.setdefault("kind", kind)
        conn = self._conn()
        conn.execute(
            """
            INSERT INTO runtime_events (
                ts, kind, role, round, lane_idx, payload
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (ts, kind, role, round, lane_idx,
             _safe_dumps(payload_dict) or "{}"),
        )
        conn.commit()

    def list_runtime_events(
        self,
        *,
        since_event_id: int = 0,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Read back runtime_events rows in insertion order.

        Used by the SSE bridge's ``Last-Event-ID`` resume path:
        the consumer sends ``Last-Event-ID: 42`` and we replay
        every row with ``event_id > 42`` before resuming the live
        stream. ``limit`` caps replay so a long-disconnected
        consumer doesn't choke the bridge.

        Returns a list of dicts shaped
        ``{"event_id": int, "ts": float, "kind": str,
        "role": str|None, "round": int|None,
        "lane_idx": int|None, "payload": dict}`` — the payload is
        parsed back from JSON for caller convenience.
        """
        if self._closed:
            return []
        conn = self._conn()
        rows = conn.execute(
            """
            SELECT event_id, ts, kind, role, round, lane_idx, payload
            FROM runtime_events
            WHERE event_id > ?
            ORDER BY event_id ASC
            LIMIT ?
            """,
            (int(since_event_id), int(limit)),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            try:
                payload = json.loads(r[6]) if r[6] else {}
            except json.JSONDecodeError:
                payload = {"__parse_error__": r[6][:200]}
            out.append({
                "event_id": r[0],
                "ts": r[1],
                "kind": r[2],
                "role": r[3],
                "round": r[4],
                "lane_idx": r[5],
                "payload": payload,
            })
        return out

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


# ---------------------------------------------------------------- #
# Reconstruction reader — used by the reopen path (Phase 4) and the
# warm-session population (Phase 4 also wires this in to keep the
# disk-loaded and warm code paths symmetric).
# ---------------------------------------------------------------- #

def load_role_messages(
    db_path: Path | str,
) -> tuple[Optional[dict[str, list[dict]]],
           Optional[dict[str, dict[int, list[dict]]]]]:
    """Reconstruct per-role and per-lane LLM message threads from a
    finalized ``transcript.db``.

    Returns ``(role_messages, role_lane_messages)``:

    - ``role_messages[role]`` — for each role that issued at least one
      ``llm_call`` event, the complete final thread: the request's
      full ``messages`` list (system + user + tool messages
      accumulated across iters) plus the final assistant message
      from the response. When a role had multiple lanes, this picks
      the most recent llm_call across all lanes — useful for the
      "feed prior conversation to the synthesizer" follow-up case.

    - ``role_lane_messages[role][lane_idx]`` — same shape but split
      per lane, populated for any role that has non-NULL ``lane_idx``
      events (in practice: researcher only). Useful when a follow-up
      wants to extend a SPECIFIC lane.

    Returns ``(None, None)`` when the file is missing, corrupt, or
    has no llm_call events. Callers fall back to today's
    turn-content reconstruction in that case.

    The db is opened read-only via the URI form so this is safe to
    call against a session whose engine instance is still alive.
    """
    p = Path(db_path)
    if not p.is_file():
        return (None, None)
    try:
        # mode=ro avoids creating WAL files when probing a fresh path
        # and lets us run against a still-open writer without locking.
        conn = sqlite3.connect(
            f"file:{p}?mode=ro", uri=True, timeout=5.0,
            check_same_thread=False,
        )
    except sqlite3.OperationalError:
        return (None, None)
    try:
        rows = conn.execute(
            "SELECT request_json, response_json, role, round, lane_idx "
            "FROM events WHERE kind = 'llm_call' ORDER BY ts"
        ).fetchall()
    except sqlite3.DatabaseError:
        # File exists but isn't a valid SQLite DB (e.g. v1.0 left a
        # zero-byte file from an earlier crash, or someone replaced
        # the artifact with garbage). Tolerate.
        try:
            conn.close()
        except Exception:
            pass
        return (None, None)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    if not rows:
        return (None, None)

    # Walk events in ts order; for each role and (role, lane_idx) we
    # keep only the LAST llm_call's payloads since the request's
    # ``messages`` list at iter N+1 already contains everything from
    # iter N plus that round's tool results. The final assistant
    # message comes from the response.
    last_by_role: dict[str, tuple[Optional[dict], Optional[dict]]] = {}
    last_by_lane: dict[str, dict[int, tuple[Optional[dict], Optional[dict]]]] = {}
    for req_j, resp_j, role, _round, lane_idx in rows:
        try:
            req = json.loads(req_j) if req_j else None
        except (json.JSONDecodeError, TypeError):
            req = None
        try:
            resp = json.loads(resp_j) if resp_j else None
        except (json.JSONDecodeError, TypeError):
            resp = None
        last_by_role[role] = (req, resp)
        if lane_idx is not None:
            last_by_lane.setdefault(role, {})[int(lane_idx)] = (req, resp)

    def _thread(req: Optional[dict],
                resp: Optional[dict]) -> Optional[list[dict]]:
        """Collapse (request, response) into a flat message list:
        the request's prior messages + the final assistant message
        from the response. None if either side is missing the
        expected shape."""
        if not isinstance(req, dict):
            return None
        msgs = req.get("messages") or []
        if not isinstance(msgs, list):
            return None
        out = list(msgs)
        if isinstance(resp, dict):
            choices = resp.get("choices") or []
            if choices and isinstance(choices, list):
                msg = (choices[0] or {}).get("message")
                if isinstance(msg, dict):
                    out.append(msg)
        return out

    role_messages: dict[str, list[dict]] = {}
    for role, (req, resp) in last_by_role.items():
        thread = _thread(req, resp)
        if thread is not None:
            role_messages[role] = thread

    role_lane_messages: dict[str, dict[int, list[dict]]] = {}
    for role, lanes in last_by_lane.items():
        per_lane: dict[int, list[dict]] = {}
        for lane_idx, (req, resp) in lanes.items():
            thread = _thread(req, resp)
            if thread is not None:
                per_lane[lane_idx] = thread
        if per_lane:
            role_lane_messages[role] = per_lane

    if not role_messages and not role_lane_messages:
        return (None, None)
    return (role_messages or None, role_lane_messages or None)
