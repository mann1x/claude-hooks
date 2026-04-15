"""
SQLite rollup for proxy JSONL logs.

Plan S1 scope (see docs/PLAN-stats-sqlite.md). Ingest existing
``~/.claude/claude-hooks-proxy/*.jsonl`` files into a persistent DB
and maintain daily / session / model / rate-limit rollups.

Stdlib only (``sqlite3``). Idempotent — re-running the ingester
never double-counts thanks to an ``ingestion_state`` cursor
(``source_file``, ``lines_processed``) and a ``UNIQUE(source_file,
source_line)`` guard on ``requests``.

S2 columns (``is_sidechain``, ``agent_id``, ``parent_session_id``,
``is_meta``, ``request_class``) and S3 columns (``thinking_*``) are
created up front so there's no migration when those phases ship —
they just stay NULL until the proxy populates them in future JSONL
lines.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger("claude_hooks.proxy.stats_db")

SCHEMA_VERSION = 4


def _schema_ddl() -> list[str]:
    """Return the full DDL for schema version ``SCHEMA_VERSION``.

    One statement per list entry so the caller can ``executescript``
    or apply piecewise.
    """
    return [
        # ---- tables ------------------------------------------------
        """
        CREATE TABLE IF NOT EXISTS ingestion_state (
            source_file      TEXT PRIMARY KEY,
            lines_processed  INTEGER NOT NULL DEFAULT 0,
            last_ts          TEXT,
            updated_at       TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS requests (
            id                           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts                           TEXT NOT NULL,
            date                         TEXT NOT NULL,     -- YYYY-MM-DD UTC, indexed
            source_file                  TEXT NOT NULL,
            source_line                  INTEGER NOT NULL,
            session_id                   TEXT,
            method                       TEXT,
            path                         TEXT,
            status                       INTEGER,
            duration_ms                  INTEGER,
            req_bytes                    INTEGER,
            resp_bytes                   INTEGER,
            model_requested              TEXT,
            model_delivered              TEXT,
            model_effective              TEXT,              -- delivered ?? requested
            input_tokens                 INTEGER,
            output_tokens                INTEGER,
            cache_creation_input_tokens  INTEGER,
            cache_read_input_tokens      INTEGER,
            ephemeral_5m_input_tokens    INTEGER,
            ephemeral_1h_input_tokens    INTEGER,
            service_tier                 TEXT,
            stop_reason                  TEXT,
            is_warmup                    INTEGER NOT NULL DEFAULT 0,
            warmup_blocked               INTEGER NOT NULL DEFAULT 0,
            synthetic                    INTEGER NOT NULL DEFAULT 0,
            http_version                 TEXT,
            error                        TEXT,
            -- S2 placeholders (NULL until request-body parser lands)
            is_sidechain                 INTEGER,
            agent_id                     TEXT,
            parent_session_id            TEXT,
            is_meta                      INTEGER,
            request_class                TEXT,
            -- S3 placeholders (NULL until SSE thinking tail lands)
            thinking_delta_count         INTEGER,
            thinking_signature_bytes     INTEGER,
            thinking_output_tokens       INTEGER,
            UNIQUE (source_file, source_line)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_requests_date ON requests(date)",
        "CREATE INDEX IF NOT EXISTS idx_requests_session ON requests(session_id, date)",
        "CREATE INDEX IF NOT EXISTS idx_requests_model ON requests(date, model_effective)",
        """
        CREATE TABLE IF NOT EXISTS daily_rollup (
            date                         TEXT PRIMARY KEY,
            request_count                INTEGER NOT NULL DEFAULT 0,
            warmup_count                 INTEGER NOT NULL DEFAULT 0,
            warmup_blocked_count         INTEGER NOT NULL DEFAULT 0,
            synthetic_count              INTEGER NOT NULL DEFAULT 0,
            status_2xx                   INTEGER NOT NULL DEFAULT 0,
            status_4xx                   INTEGER NOT NULL DEFAULT 0,
            status_5xx                   INTEGER NOT NULL DEFAULT 0,
            status_429                   INTEGER NOT NULL DEFAULT 0,
            model_divergence_count       INTEGER NOT NULL DEFAULT 0,
            total_input_tokens           INTEGER NOT NULL DEFAULT 0,
            total_output_tokens          INTEGER NOT NULL DEFAULT 0,
            total_cache_creation_tokens  INTEGER NOT NULL DEFAULT 0,
            total_cache_read_tokens      INTEGER NOT NULL DEFAULT 0,
            cache_hit_rate               REAL,
            total_req_bytes              INTEGER NOT NULL DEFAULT 0,
            total_resp_bytes             INTEGER NOT NULL DEFAULT 0,
            total_duration_ms            INTEGER NOT NULL DEFAULT 0,
            updated_at                   TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS session_rollup (
            session_id                   TEXT NOT NULL,
            date                         TEXT NOT NULL,
            request_count                INTEGER NOT NULL DEFAULT 0,
            warmup_count                 INTEGER NOT NULL DEFAULT 0,
            warmup_blocked_count         INTEGER NOT NULL DEFAULT 0,
            total_input_tokens           INTEGER NOT NULL DEFAULT 0,
            total_output_tokens          INTEGER NOT NULL DEFAULT 0,
            total_cache_creation_tokens  INTEGER NOT NULL DEFAULT 0,
            total_cache_read_tokens      INTEGER NOT NULL DEFAULT 0,
            first_ts                     TEXT,
            last_ts                      TEXT,
            PRIMARY KEY (session_id, date)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS model_rollup (
            date                         TEXT NOT NULL,
            model                        TEXT NOT NULL,
            request_count                INTEGER NOT NULL DEFAULT 0,
            input_tokens                 INTEGER NOT NULL DEFAULT 0,
            output_tokens                INTEGER NOT NULL DEFAULT 0,
            cache_creation_tokens        INTEGER NOT NULL DEFAULT 0,
            cache_read_tokens            INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (date, model)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS ratelimit_windows (
            id                           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts                           TEXT NOT NULL,
            five_hour_utilization        REAL,
            seven_day_utilization        REAL,
            representative_claim         TEXT,
            five_hour_status             TEXT,
            seven_day_status             TEXT,
            unified_status               TEXT,
            five_hour_reset              INTEGER,
            seven_day_reset              INTEGER
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_ratelimit_ts ON ratelimit_windows(ts)",
    ]


def connect(db_path: Path) -> sqlite3.Connection:
    """Open (and migrate if needed) the stats DB.

    Safe to call repeatedly — DDL uses ``IF NOT EXISTS`` and
    ``PRAGMA user_version`` guards migrations.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current >= SCHEMA_VERSION:
        return
    # v0 -> v1: full schema (IF NOT EXISTS so re-running is safe on
    # partially created DBs).
    if current < 1:
        for stmt in _schema_ddl():
            conn.execute(stmt)
    # v1 -> v2: S2 columns + agent_rollup.
    if current < 2:
        _migrate_v2(conn)
    # v2 -> v3: S3 thinking-metric totals on daily_rollup.
    if current < 3:
        _migrate_v3(conn)
    # v3 -> v4: visible/redacted thinking split + tool-use aggregates.
    if current < 4:
        _migrate_v4(conn)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _migrate_v2(conn: sqlite3.Connection) -> None:
    """Add S2 columns to ``requests`` and create ``agent_rollup``.

    Columns are added with ``ALTER TABLE`` because ``ADD COLUMN`` is
    SQLite-safe (no rewrite); existing rows get NULL.
    """
    existing = {
        r[1] for r in conn.execute("PRAGMA table_info(requests)").fetchall()
    }
    additions = [
        ("account_uuid", "TEXT"),
        ("cc_version", "TEXT"),
        ("cc_entrypoint", "TEXT"),
        ("effort", "TEXT"),
        ("thinking_type", "TEXT"),
        ("max_tokens", "INTEGER"),
        ("num_tools", "INTEGER"),
        ("num_messages", "INTEGER"),
        ("agent_type", "TEXT"),
        ("agent_name", "TEXT"),
        ("beta_features", "TEXT"),
    ]
    for col, typ in additions:
        if col not in existing:
            conn.execute(f"ALTER TABLE requests ADD COLUMN {col} {typ}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_agent "
                 "ON requests(date, agent_name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_class "
                 "ON requests(date, request_class)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_rollup (
            date                         TEXT NOT NULL,
            agent_name                   TEXT NOT NULL,
            agent_type                   TEXT,
            request_count                INTEGER NOT NULL DEFAULT 0,
            warmup_blocked_count         INTEGER NOT NULL DEFAULT 0,
            input_tokens                 INTEGER NOT NULL DEFAULT 0,
            output_tokens                INTEGER NOT NULL DEFAULT 0,
            cache_creation_tokens        INTEGER NOT NULL DEFAULT 0,
            cache_read_tokens            INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (date, agent_name)
        )
    """)


def _migrate_v3(conn: sqlite3.Connection) -> None:
    """Add S3 thinking-metric totals to ``daily_rollup``.

    ``requests`` already has the per-row S3 columns (carved out in v1
    as nullable placeholders), so nothing changes there — we only
    need daily totals so dashboards can chart reasoning-depth trend
    without scanning every row.
    """
    existing = {
        r[1] for r in conn.execute("PRAGMA table_info(daily_rollup)").fetchall()
    }
    additions = [
        ("thinking_request_count", "INTEGER NOT NULL DEFAULT 0"),
        ("total_thinking_delta_count", "INTEGER NOT NULL DEFAULT 0"),
        ("total_thinking_signature_bytes", "INTEGER NOT NULL DEFAULT 0"),
        ("total_thinking_output_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ]
    for col, typ in additions:
        if col not in existing:
            conn.execute(f"ALTER TABLE daily_rollup ADD COLUMN {col} {typ}")


def _migrate_v4(conn: sqlite3.Connection) -> None:
    """Visible/redacted thinking split on ``requests`` + tool-use
    aggregate columns on both ``requests`` and ``daily_rollup``.

    Tool categories follow stellaraccident's catalog in #42796:

      research  = Read, Grep, Glob, WebFetch, WebSearch
      mutation  = Edit, Write, MultiEdit, NotebookEdit
      bash, task (subagent spawn), other = rest

    ``tool_use_counts`` stays as a JSON blob on requests so we keep
    the full picture (incl. MCP / new tools we haven't categorised
    yet); the categorised columns are pre-aggregated for fast daily
    rollups.
    """
    req_cols = {
        r[1] for r in conn.execute("PRAGMA table_info(requests)").fetchall()
    }
    req_adds = [
        ("thinking_visible_delta_count", "INTEGER"),
        ("thinking_redacted_delta_count", "INTEGER"),
        ("tool_use_counts", "TEXT"),               # JSON map
        ("tool_read_count", "INTEGER"),
        ("tool_edit_count", "INTEGER"),
        ("tool_research_count", "INTEGER"),
        ("tool_mutation_count", "INTEGER"),
        ("tool_bash_count", "INTEGER"),
        ("tool_task_count", "INTEGER"),
        ("tool_total_count", "INTEGER"),
    ]
    for col, typ in req_adds:
        if col not in req_cols:
            conn.execute(f"ALTER TABLE requests ADD COLUMN {col} {typ}")

    daily_cols = {
        r[1] for r in conn.execute("PRAGMA table_info(daily_rollup)").fetchall()
    }
    daily_adds = [
        ("total_thinking_visible_delta_count",  "INTEGER NOT NULL DEFAULT 0"),
        ("total_thinking_redacted_delta_count", "INTEGER NOT NULL DEFAULT 0"),
        ("total_tool_read_count",     "INTEGER NOT NULL DEFAULT 0"),
        ("total_tool_edit_count",     "INTEGER NOT NULL DEFAULT 0"),
        ("total_tool_research_count", "INTEGER NOT NULL DEFAULT 0"),
        ("total_tool_mutation_count", "INTEGER NOT NULL DEFAULT 0"),
        ("total_tool_bash_count",     "INTEGER NOT NULL DEFAULT 0"),
        ("total_tool_task_count",     "INTEGER NOT NULL DEFAULT 0"),
        ("total_tool_total_count",    "INTEGER NOT NULL DEFAULT 0"),
    ]
    for col, typ in daily_adds:
        if col not in daily_cols:
            conn.execute(f"ALTER TABLE daily_rollup ADD COLUMN {col} {typ}")


# ------------------------------------------------------------------ #
# Ingestion
# ------------------------------------------------------------------ #
@dataclass
class IngestResult:
    source_file: str
    lines_read: int        # total lines in file at ingest time
    lines_inserted: int    # new rows written
    lines_skipped: int     # already-seen lines (cursor advanced past)
    parse_errors: int


def ingest_file(
    conn: sqlite3.Connection,
    path: Path,
    *,
    batch_size: int = 500,
) -> IngestResult:
    """Read new lines from a JSONL file into the ``requests`` table.

    Uses ``ingestion_state.lines_processed`` as a cursor — previously
    seen lines are skipped without parsing. Re-running is cheap and
    idempotent.
    """
    from datetime import datetime, timezone

    source = path.name
    row = conn.execute(
        "SELECT lines_processed FROM ingestion_state WHERE source_file=?",
        (source,),
    ).fetchone()
    cursor = row[0] if row else 0

    inserted = 0
    skipped = 0
    errors = 0
    lines_read = 0
    last_ts: Optional[str] = None

    batch: list[tuple] = []

    with open(path, "r", encoding="utf-8") as f:
        for i, raw in enumerate(f, start=1):
            lines_read = i
            if i <= cursor:
                skipped += 1
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                errors += 1
                continue
            if not isinstance(rec, dict):
                errors += 1
                continue
            row_values = _row_from_record(rec, source, i)
            if row_values is None:
                errors += 1
                continue
            last_ts = row_values[0]
            batch.append(row_values)
            if len(batch) >= batch_size:
                inserted += _flush(conn, batch)
                batch.clear()
                # Also capture ratelimit snapshots from the same batch.
    if batch:
        inserted += _flush(conn, batch)

    # Record ratelimit window snapshots (independent of requests dedup).
    _ingest_ratelimit_snapshots(conn, path, cursor)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        """
        INSERT INTO ingestion_state(source_file, lines_processed, last_ts, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(source_file) DO UPDATE SET
            lines_processed = excluded.lines_processed,
            last_ts = excluded.last_ts,
            updated_at = excluded.updated_at
        """,
        (source, lines_read, last_ts, now),
    )
    return IngestResult(source, lines_read, inserted, skipped, errors)


def _row_from_record(
    rec: dict, source_file: str, source_line: int,
) -> Optional[tuple]:
    """Flatten a JSONL record into a ``requests`` row tuple."""
    ts = rec.get("ts")
    if not isinstance(ts, str) or len(ts) < 10:
        return None
    date = ts[:10]

    usage = rec.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}

    model_req = rec.get("model_requested")
    model_del = rec.get("model_delivered")
    model_eff = model_del or model_req

    beta_features = rec.get("beta_features")
    if isinstance(beta_features, list):
        beta_features = ",".join(beta_features)
    elif not isinstance(beta_features, (str, type(None))):
        beta_features = None

    # Tool-use aggregation (S4 / stellaraccident metrics).
    tool_counts = rec.get("tool_use_counts")
    cats = _categorise_tools(tool_counts if isinstance(tool_counts, dict) else None)
    tool_counts_json = (
        json.dumps(tool_counts) if isinstance(tool_counts, dict) and tool_counts
        else None
    )

    return (
        ts, date, source_file, source_line,
        rec.get("session_id"),
        rec.get("method"),
        rec.get("path"),
        rec.get("status"),
        rec.get("duration_ms"),
        rec.get("req_bytes"),
        rec.get("resp_bytes"),
        model_req,
        model_del,
        model_eff,
        usage.get("input_tokens"),
        usage.get("output_tokens"),
        usage.get("cache_creation_input_tokens"),
        usage.get("cache_read_input_tokens"),
        usage.get("ephemeral_5m_input_tokens"),
        usage.get("ephemeral_1h_input_tokens"),
        usage.get("service_tier"),
        rec.get("stop_reason"),
        1 if rec.get("is_warmup") else 0,
        1 if rec.get("warmup_blocked") else 0,
        1 if rec.get("synthetic") else 0,
        rec.get("http_version"),
        rec.get("error"),
        # S2 placeholders — is_sidechain now populated from agent_type.
        _bool_or_none(rec.get("is_sidechain")),
        rec.get("agent_id") or rec.get("agent_name"),
        rec.get("parent_session_id"),
        _bool_or_none(rec.get("is_meta")),
        rec.get("request_class"),
        # S3 placeholders.
        rec.get("thinking_delta_count"),
        rec.get("thinking_signature_bytes"),
        rec.get("thinking_output_tokens"),
        # S2 additions.
        rec.get("account_uuid"),
        rec.get("cc_version"),
        rec.get("cc_entrypoint"),
        rec.get("effort"),
        rec.get("thinking_type"),
        rec.get("max_tokens"),
        rec.get("num_tools"),
        rec.get("num_messages"),
        rec.get("agent_type"),
        rec.get("agent_name"),
        beta_features,
        # S4 additions — visible/redacted thinking + tool-use aggregates.
        rec.get("thinking_visible_delta_count"),
        rec.get("thinking_redacted_delta_count"),
        tool_counts_json,
        cats["read"] or None,
        cats["edit"] or None,
        cats["research"] or None,
        cats["mutation"] or None,
        cats["bash"] or None,
        cats["task"] or None,
        cats["total"] or None,
    )


def _bool_or_none(v) -> Optional[int]:
    if v is None:
        return None
    return 1 if v else 0


# Tool categorisation per stellaraccident's #42796 catalog. Anything
# not listed here falls into ``other`` (counted only in tool_total).
_TOOLS_RESEARCH = {"Read", "Grep", "Glob", "WebFetch", "WebSearch"}
_TOOLS_MUTATION = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def _categorise_tools(counts: Optional[dict]) -> dict[str, int]:
    """Return per-category totals for a ``tool_use_counts`` dict.

    Returns all-zero entries even when ``counts`` is None/empty so the
    ingestion path always writes integers (not NULL) to the columns.
    Keys: read, edit, research, mutation, bash, task, total.
    """
    out = {"read": 0, "edit": 0, "research": 0, "mutation": 0,
           "bash": 0, "task": 0, "total": 0}
    if not isinstance(counts, dict):
        return out
    for name, n in counts.items():
        if not isinstance(n, int) or n <= 0:
            continue
        out["total"] += n
        if name == "Read":
            out["read"] += n
        elif name == "Edit":
            out["edit"] += n
        if name in _TOOLS_RESEARCH:
            out["research"] += n
        elif name in _TOOLS_MUTATION:
            out["mutation"] += n
        elif name == "Bash":
            out["bash"] += n
        elif name == "Task":
            out["task"] += n
    return out


_INSERT_SQL = """
    INSERT OR IGNORE INTO requests(
        ts, date, source_file, source_line,
        session_id, method, path, status, duration_ms,
        req_bytes, resp_bytes,
        model_requested, model_delivered, model_effective,
        input_tokens, output_tokens,
        cache_creation_input_tokens, cache_read_input_tokens,
        ephemeral_5m_input_tokens, ephemeral_1h_input_tokens,
        service_tier, stop_reason,
        is_warmup, warmup_blocked, synthetic,
        http_version, error,
        is_sidechain, agent_id, parent_session_id, is_meta, request_class,
        thinking_delta_count, thinking_signature_bytes, thinking_output_tokens,
        account_uuid, cc_version, cc_entrypoint, effort, thinking_type,
        max_tokens, num_tools, num_messages, agent_type, agent_name,
        beta_features,
        thinking_visible_delta_count, thinking_redacted_delta_count,
        tool_use_counts,
        tool_read_count, tool_edit_count, tool_research_count,
        tool_mutation_count, tool_bash_count, tool_task_count, tool_total_count
    ) VALUES (?, ?, ?, ?,  ?, ?, ?, ?, ?,  ?, ?,  ?, ?, ?,
              ?, ?,  ?, ?,  ?, ?,  ?, ?,  ?, ?, ?,  ?, ?,
              ?, ?, ?, ?, ?,  ?, ?, ?,
              ?, ?, ?, ?, ?,  ?, ?, ?, ?, ?,  ?,
              ?, ?,  ?,  ?, ?, ?, ?, ?, ?, ?)
"""


def _flush(conn: sqlite3.Connection, batch: list[tuple]) -> int:
    cur = conn.executemany(_INSERT_SQL, batch)
    return cur.rowcount or 0


def _ingest_ratelimit_snapshots(
    conn: sqlite3.Connection, path: Path, start_line: int,
) -> None:
    """Write one ``ratelimit_windows`` row per JSONL record that carries
    unified headers. Only looks at lines past ``start_line`` so repeated
    runs don't create duplicates. There's no UNIQUE index here — the
    timeseries can legitimately contain adjacent identical rows (state
    snapshots from distinct requests). Dedup is handled by the cursor.
    """
    with open(path, "r", encoding="utf-8") as f:
        for i, raw in enumerate(f, start=1):
            if i <= start_line:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            rl = rec.get("rate_limit")
            if not isinstance(rl, dict):
                continue
            five = _float_or_none(rl.get("anthropic-ratelimit-unified-5h-utilization"))
            seven = _float_or_none(rl.get("anthropic-ratelimit-unified-7d-utilization"))
            if five is None and seven is None:
                continue
            conn.execute(
                """INSERT INTO ratelimit_windows(
                    ts, five_hour_utilization, seven_day_utilization,
                    representative_claim, five_hour_status, seven_day_status,
                    unified_status, five_hour_reset, seven_day_reset
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    rec.get("ts"),
                    five,
                    seven,
                    rl.get("anthropic-ratelimit-unified-representative-claim"),
                    rl.get("anthropic-ratelimit-unified-5h-status"),
                    rl.get("anthropic-ratelimit-unified-7d-status"),
                    rl.get("anthropic-ratelimit-unified-status"),
                    _int_or_none(rl.get("anthropic-ratelimit-unified-5h-reset")),
                    _int_or_none(rl.get("anthropic-ratelimit-unified-7d-reset")),
                ),
            )


def _float_or_none(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int_or_none(v):
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ #
# Rollup recompute
# ------------------------------------------------------------------ #
def rebuild_rollups(conn: sqlite3.Connection, *, dates: Optional[Iterable[str]] = None) -> None:
    """Rebuild daily / session / model rollups from ``requests``.

    If ``dates`` is given, only those date partitions are rebuilt;
    otherwise all dates present in ``requests`` are rebuilt. Rollups
    are derived, never accumulated — always safe to recompute.
    """
    from datetime import datetime, timezone

    if dates is None:
        rows = conn.execute("SELECT DISTINCT date FROM requests").fetchall()
        date_list = [r[0] for r in rows]
    else:
        date_list = list(dates)

    if not date_list:
        return

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for d in date_list:
        # Daily
        agg = conn.execute(
            """
            SELECT
                COUNT(*),
                COALESCE(SUM(is_warmup), 0),
                COALESCE(SUM(warmup_blocked), 0),
                COALESCE(SUM(synthetic), 0),
                COALESCE(SUM(CASE WHEN status BETWEEN 200 AND 299 THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN status BETWEEN 400 AND 499 THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN status BETWEEN 500 AND 599 THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN status = 429 THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE
                    WHEN model_requested IS NOT NULL
                     AND model_delivered IS NOT NULL
                     AND model_requested <> model_delivered
                    THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(input_tokens), 0),
                COALESCE(SUM(output_tokens), 0),
                COALESCE(SUM(cache_creation_input_tokens), 0),
                COALESCE(SUM(cache_read_input_tokens), 0),
                COALESCE(SUM(req_bytes), 0),
                COALESCE(SUM(resp_bytes), 0),
                COALESCE(SUM(duration_ms), 0),
                COALESCE(SUM(CASE
                    WHEN thinking_delta_count IS NOT NULL
                      OR thinking_signature_bytes IS NOT NULL
                    THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(thinking_delta_count), 0),
                COALESCE(SUM(thinking_signature_bytes), 0),
                COALESCE(SUM(thinking_output_tokens), 0),
                COALESCE(SUM(thinking_visible_delta_count), 0),
                COALESCE(SUM(thinking_redacted_delta_count), 0),
                COALESCE(SUM(tool_read_count), 0),
                COALESCE(SUM(tool_edit_count), 0),
                COALESCE(SUM(tool_research_count), 0),
                COALESCE(SUM(tool_mutation_count), 0),
                COALESCE(SUM(tool_bash_count), 0),
                COALESCE(SUM(tool_task_count), 0),
                COALESCE(SUM(tool_total_count), 0)
            FROM requests WHERE date = ?
            """,
            (d,),
        ).fetchone()
        (
            rc, wm, wmb, syn, s2, s4, s5, s429, div,
            inp, out, cc, cr, rb, rsb, dur,
            thrc, thdc, thsb, thot,
            thvis, thred,
            tr, te, tres, tmut, tbash, ttask, ttotal,
        ) = agg
        denom = (cc or 0) + (cr or 0)
        hit_rate = ((cr or 0) / denom) if denom > 0 else None
        conn.execute(
            """
            INSERT INTO daily_rollup(
                date, request_count, warmup_count, warmup_blocked_count,
                synthetic_count, status_2xx, status_4xx, status_5xx, status_429,
                model_divergence_count,
                total_input_tokens, total_output_tokens,
                total_cache_creation_tokens, total_cache_read_tokens,
                cache_hit_rate, total_req_bytes, total_resp_bytes,
                total_duration_ms, updated_at,
                thinking_request_count, total_thinking_delta_count,
                total_thinking_signature_bytes, total_thinking_output_tokens,
                total_thinking_visible_delta_count,
                total_thinking_redacted_delta_count,
                total_tool_read_count, total_tool_edit_count,
                total_tool_research_count, total_tool_mutation_count,
                total_tool_bash_count, total_tool_task_count,
                total_tool_total_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?,
                      ?, ?,
                      ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                request_count=excluded.request_count,
                warmup_count=excluded.warmup_count,
                warmup_blocked_count=excluded.warmup_blocked_count,
                synthetic_count=excluded.synthetic_count,
                status_2xx=excluded.status_2xx,
                status_4xx=excluded.status_4xx,
                status_5xx=excluded.status_5xx,
                status_429=excluded.status_429,
                model_divergence_count=excluded.model_divergence_count,
                total_input_tokens=excluded.total_input_tokens,
                total_output_tokens=excluded.total_output_tokens,
                total_cache_creation_tokens=excluded.total_cache_creation_tokens,
                total_cache_read_tokens=excluded.total_cache_read_tokens,
                cache_hit_rate=excluded.cache_hit_rate,
                total_req_bytes=excluded.total_req_bytes,
                total_resp_bytes=excluded.total_resp_bytes,
                total_duration_ms=excluded.total_duration_ms,
                updated_at=excluded.updated_at,
                thinking_request_count=excluded.thinking_request_count,
                total_thinking_delta_count=excluded.total_thinking_delta_count,
                total_thinking_signature_bytes=excluded.total_thinking_signature_bytes,
                total_thinking_output_tokens=excluded.total_thinking_output_tokens,
                total_thinking_visible_delta_count=excluded.total_thinking_visible_delta_count,
                total_thinking_redacted_delta_count=excluded.total_thinking_redacted_delta_count,
                total_tool_read_count=excluded.total_tool_read_count,
                total_tool_edit_count=excluded.total_tool_edit_count,
                total_tool_research_count=excluded.total_tool_research_count,
                total_tool_mutation_count=excluded.total_tool_mutation_count,
                total_tool_bash_count=excluded.total_tool_bash_count,
                total_tool_task_count=excluded.total_tool_task_count,
                total_tool_total_count=excluded.total_tool_total_count
            """,
            (d, rc, wm, wmb, syn, s2, s4, s5, s429, div,
             inp, out, cc, cr, hit_rate, rb, rsb, dur, now,
             thrc, thdc, thsb, thot,
             thvis, thred,
             tr, te, tres, tmut, tbash, ttask, ttotal),
        )

        # Session rollup — rebuild for the date.
        conn.execute("DELETE FROM session_rollup WHERE date = ?", (d,))
        conn.execute(
            """
            INSERT INTO session_rollup(
                session_id, date, request_count,
                warmup_count, warmup_blocked_count,
                total_input_tokens, total_output_tokens,
                total_cache_creation_tokens, total_cache_read_tokens,
                first_ts, last_ts
            )
            SELECT
                session_id, date, COUNT(*),
                COALESCE(SUM(is_warmup), 0),
                COALESCE(SUM(warmup_blocked), 0),
                COALESCE(SUM(input_tokens), 0),
                COALESCE(SUM(output_tokens), 0),
                COALESCE(SUM(cache_creation_input_tokens), 0),
                COALESCE(SUM(cache_read_input_tokens), 0),
                MIN(ts), MAX(ts)
            FROM requests
            WHERE date = ? AND session_id IS NOT NULL
            GROUP BY session_id, date
            """,
            (d,),
        )

        # Model rollup — rebuild for the date.
        conn.execute("DELETE FROM model_rollup WHERE date = ?", (d,))
        conn.execute(
            """
            INSERT INTO model_rollup(
                date, model, request_count,
                input_tokens, output_tokens,
                cache_creation_tokens, cache_read_tokens
            )
            SELECT
                date,
                COALESCE(model_effective, '<unknown>'),
                COUNT(*),
                COALESCE(SUM(input_tokens), 0),
                COALESCE(SUM(output_tokens), 0),
                COALESCE(SUM(cache_creation_input_tokens), 0),
                COALESCE(SUM(cache_read_input_tokens), 0)
            FROM requests
            WHERE date = ?
            GROUP BY date, model_effective
            """,
            (d,),
        )

        # Agent rollup — S2. Rows keyed by agent_name, skipping rows
        # where the parser couldn't classify (pre-S2 JSONL).
        conn.execute("DELETE FROM agent_rollup WHERE date = ?", (d,))
        conn.execute(
            """
            INSERT INTO agent_rollup(
                date, agent_name, agent_type, request_count,
                warmup_blocked_count,
                input_tokens, output_tokens,
                cache_creation_tokens, cache_read_tokens
            )
            SELECT
                date,
                agent_name,
                MAX(agent_type),
                COUNT(*),
                COALESCE(SUM(warmup_blocked), 0),
                COALESCE(SUM(input_tokens), 0),
                COALESCE(SUM(output_tokens), 0),
                COALESCE(SUM(cache_creation_input_tokens), 0),
                COALESCE(SUM(cache_read_input_tokens), 0)
            FROM requests
            WHERE date = ? AND agent_name IS NOT NULL
            GROUP BY date, agent_name
            """,
            (d,),
        )


# ------------------------------------------------------------------ #
# Top-level helper
# ------------------------------------------------------------------ #
def ingest_dir(
    db_path: Path, log_dir: Path, *, since: Optional[str] = None,
) -> list[IngestResult]:
    """Ingest every JSONL in ``log_dir`` into ``db_path`` and rebuild
    rollups for the affected dates. Returns per-file ingest results.
    """
    conn = connect(db_path)
    results: list[IngestResult] = []
    touched_dates: set[str] = set()
    try:
        files = sorted(log_dir.glob("*.jsonl"))
        for f in files:
            if since and f.stem < since:
                continue
            # Snapshot the cursor BEFORE ingest so we can detect whether
            # new rows actually landed — only then do we rebuild the day.
            row = conn.execute(
                "SELECT lines_processed FROM ingestion_state WHERE source_file=?",
                (f.name,),
            ).fetchone()
            before = row[0] if row else 0
            r = ingest_file(conn, f)
            results.append(r)
            if r.lines_inserted > 0 or r.lines_read > before:
                touched_dates.add(f.stem)
        if touched_dates:
            rebuild_rollups(conn, dates=touched_dates)
    finally:
        conn.close()
    return results
