"""Tables for the mailbox, in both dialects.

A dedicated table rather than the vector store: the mailbox needs exact
addressing, unread state and ordering, and never needs similarity
search. Embedding a message would cost an embedder round-trip on the
send path for nothing.

On pgvector it lives in the same database as the memories, so it is
cross-host between solidpc and pandorum for free and inherits the backup
already covering that database. On sqlite_vec it lives in the same
``.db`` — a host without shared Postgres simply has a local mailbox,
which is honest rather than silently half-working.
"""
from __future__ import annotations

SCHEMA_VERSION = 1

#: ``ack_needs_read`` is the one invariant worth enforcing in the
#: database: an acknowledgement on an unread message would be a receipt
#: for something nobody received.
_PG = [
    """
    CREATE TABLE IF NOT EXISTS session_registry (
        session_id TEXT PRIMARY KEY,
        alias      TEXT NOT NULL,
        host       TEXT NOT NULL,
        os         TEXT NOT NULL DEFAULT '',
        cwd        TEXT NOT NULL DEFAULT '',
        started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_seen  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS session_registry_alias_idx "
    "ON session_registry (alias)",
    """
    CREATE TABLE IF NOT EXISTS session_messages (
        id           BIGSERIAL PRIMARY KEY,
        created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        edited_at    TIMESTAMPTZ,
        from_alias   TEXT NOT NULL,
        from_session TEXT,
        from_host    TEXT NOT NULL DEFAULT '',
        to_alias     TEXT,
        to_session   TEXT,
        to_host      TEXT,
        broadcast_group TEXT,
        subject      TEXT NOT NULL,
        body         TEXT NOT NULL,
        priority     SMALLINT NOT NULL DEFAULT 0,
        read_at      TIMESTAMPTZ,
        read_by      TEXT,
        ack_body     TEXT,
        ack_at       TIMESTAMPTZ,
        ack_edited_at TIMESTAMPTZ,
        receipt_read_at TIMESTAMPTZ,
        cancelled_at TIMESTAMPTZ,
        expires_at   TIMESTAMPTZ NOT NULL
                     DEFAULT now() + INTERVAL '180 days',
        CONSTRAINT one_recipient CHECK (
            (to_alias IS NULL) <> (to_session IS NULL)),
        CONSTRAINT ack_needs_read CHECK (
            ack_body IS NULL OR read_at IS NOT NULL)
    )
    """,
    "CREATE INDEX IF NOT EXISTS session_messages_to_alias_idx "
    "ON session_messages (to_alias) WHERE read_at IS NULL",
    "CREATE INDEX IF NOT EXISTS session_messages_to_session_idx "
    "ON session_messages (to_session) WHERE read_at IS NULL",
    "CREATE INDEX IF NOT EXISTS session_messages_expiry_idx "
    "ON session_messages (expires_at)",
    "CREATE INDEX IF NOT EXISTS session_messages_receipt_idx "
    "ON session_messages (from_alias) "
    "WHERE ack_body IS NOT NULL AND receipt_read_at IS NULL",
]

#: SQLite has no TIMESTAMPTZ and no BIGSERIAL. Times are ISO-8601 UTC
#: strings, which sort correctly as text — the property that lets the
#: same ORDER BY and range predicates serve both dialects.
_SQLITE = [
    """
    CREATE TABLE IF NOT EXISTS session_registry (
        session_id TEXT PRIMARY KEY,
        alias      TEXT NOT NULL,
        host       TEXT NOT NULL,
        os         TEXT NOT NULL DEFAULT '',
        cwd        TEXT NOT NULL DEFAULT '',
        started_at TEXT NOT NULL,
        last_seen  TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS session_registry_alias_idx "
    "ON session_registry (alias)",
    """
    CREATE TABLE IF NOT EXISTS session_messages (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at   TEXT NOT NULL,
        edited_at    TEXT,
        from_alias   TEXT NOT NULL,
        from_session TEXT,
        from_host    TEXT NOT NULL DEFAULT '',
        to_alias     TEXT,
        to_session   TEXT,
        to_host      TEXT,
        broadcast_group TEXT,
        subject      TEXT NOT NULL,
        body         TEXT NOT NULL,
        priority     INTEGER NOT NULL DEFAULT 0,
        read_at      TEXT,
        read_by      TEXT,
        ack_body     TEXT,
        ack_at       TEXT,
        ack_edited_at TEXT,
        receipt_read_at TEXT,
        cancelled_at TEXT,
        expires_at   TEXT NOT NULL,
        CHECK ((to_alias IS NULL) <> (to_session IS NULL)),
        CHECK (ack_body IS NULL OR read_at IS NOT NULL)
    )
    """,
    "CREATE INDEX IF NOT EXISTS session_messages_to_alias_idx "
    "ON session_messages (to_alias)",
    "CREATE INDEX IF NOT EXISTS session_messages_to_session_idx "
    "ON session_messages (to_session)",
    "CREATE INDEX IF NOT EXISTS session_messages_expiry_idx "
    "ON session_messages (expires_at)",
]

#: Columns read back, in one place so every SELECT agrees with the
#: row-to-dict mapping. Two lists that drift are how a field silently
#: becomes another field's value.
MESSAGE_COLUMNS = (
    "id", "created_at", "edited_at", "from_alias", "from_session",
    "from_host", "to_alias", "to_session", "to_host", "broadcast_group",
    "subject", "body", "priority", "read_at", "read_by", "ack_body",
    "ack_at", "ack_edited_at", "receipt_read_at", "cancelled_at",
    "expires_at",
)

SESSION_COLUMNS = (
    "session_id", "alias", "host", "os", "cwd", "started_at", "last_seen",
)


def statements(dialect: str) -> list[str]:
    if dialect == "postgres":
        return list(_PG)
    if dialect == "sqlite":
        return list(_SQLITE)
    raise ValueError(f"unknown dialect {dialect!r}")
