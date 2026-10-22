"""Tables for the resource manager, in both dialects.

On pgvector they live in the memory database, which solidpc and pandorum
already share, so one claim table covers every host and every session.
Without Postgres the same tables live in a local SQLite file
(:func:`claude_hooks.resources.store.backend_from_config`): locking then
covers this host only, which is the truth on a host with no shared store.

Times are ISO-8601 UTC text in both dialects (they sort correctly), and a
claim's items are JSON text, so one set of queries serves both.
"""
from __future__ import annotations

CHANNEL = "claude_hooks_resources"
#: The advisory lock every decision takes (``hashtext`` of the channel
#: would do, but a constant reads the same in a ``pg_locks`` dump).
ADVISORY_KEY = 0x5245534F  # "RESO"

_CLAIMS_COLS = """
    id TEXT UNIQUE NOT NULL,
    items TEXT NOT NULL,
    state TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    alias TEXT NOT NULL DEFAULT '',
    host TEXT NOT NULL DEFAULT '',
    pid {INT},
    pid_started {REAL},
    reason TEXT NOT NULL DEFAULT '',
    cwd TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    granted_at TEXT NOT NULL DEFAULT '',
    renewed_at TEXT NOT NULL,
    expires_at TEXT NOT NULL DEFAULT '',
    lease_s INTEGER NOT NULL DEFAULT 0
"""

_EVENTS_COLS = """
    at TEXT NOT NULL,
    claim_id TEXT NOT NULL DEFAULT '',
    event TEXT NOT NULL,
    items TEXT NOT NULL DEFAULT '',
    alias TEXT NOT NULL DEFAULT '',
    host TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT ''
"""

_DEFS = """
CREATE TABLE IF NOT EXISTS resource_defs (
    name TEXT PRIMARY KEY,
    capacity INTEGER,
    description TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
)
"""


def statements(dialect: str) -> list[str]:
    if dialect == "postgres":
        claims = ("CREATE TABLE IF NOT EXISTS resource_claims ("
                  "seq BIGSERIAL PRIMARY KEY,"
                  + _CLAIMS_COLS.format(INT="BIGINT",
                                        REAL="DOUBLE PRECISION") + ")")
        events = ("CREATE TABLE IF NOT EXISTS resource_events ("
                  "seq BIGSERIAL PRIMARY KEY," + _EVENTS_COLS + ")")
    else:
        claims = ("CREATE TABLE IF NOT EXISTS resource_claims ("
                  "seq INTEGER PRIMARY KEY AUTOINCREMENT,"
                  + _CLAIMS_COLS.format(INT="INTEGER", REAL="REAL") + ")")
        events = ("CREATE TABLE IF NOT EXISTS resource_events ("
                  "seq INTEGER PRIMARY KEY AUTOINCREMENT," + _EVENTS_COLS
                  + ")")
    return [claims, events, _DEFS,
            "CREATE INDEX IF NOT EXISTS resource_claims_session_idx "
            "ON resource_claims (session_id)",
            "CREATE INDEX IF NOT EXISTS resource_events_at_idx "
            "ON resource_events (at)"]


CLAIM_COLUMNS = ("seq", "id", "items", "state", "session_id", "alias", "host",
                 "pid", "pid_started", "reason", "cwd", "created_at",
                 "granted_at", "renewed_at", "expires_at", "lease_s")
