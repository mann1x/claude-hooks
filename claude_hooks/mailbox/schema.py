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

SCHEMA_VERSION = 2

#: One registration per ``(alias, host)``, and the migration that makes
#: it possible.
#:
#: ``session_id`` is the primary key, so a client that restarts under a
#: new id simply inserted another row: observed live as five
#: ``xollama@solidpc`` rows in one cwd, four of them an hour stale
#: behind the one doing the work. Delivery already collapses to distinct
#: ``(alias, host)`` pairs, so the duplicates stopped double-sending —
#: they corrupt what is *readable* instead. ``mailbox-sessions`` shows a
#: crowd where there is one session, the SessionStart collision note
#: warns about peers that are the same session, and every liveness
#: question — is this alias alive, should it be evicted — gets answered
#: from whichever row happens to be found first, which is usually a dead
#: one.
#:
#: The DELETE runs before the index because the index cannot be created
#: while duplicates exist, and it keeps the most recently seen row,
#: breaking ties on ``session_id`` so the choice is deterministic rather
#: than storage order. It is a no-op once the index exists, which is why
#: it is safe on the schema path that every process runs.
_DEDUPE_REGISTRY = """
    DELETE FROM session_registry
     WHERE EXISTS (
        SELECT 1 FROM session_registry AS newer
         WHERE newer.alias = session_registry.alias
           AND newer.host  = session_registry.host
           AND (newer.last_seen > session_registry.last_seen
                OR (newer.last_seen = session_registry.last_seen
                    AND newer.session_id > session_registry.session_id)))
"""

_UNIQUE_REGISTRY = (
    "CREATE UNIQUE INDEX IF NOT EXISTS session_registry_alias_host_uidx "
    "ON session_registry (alias, host)"
)

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
        last_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
        client_pid     BIGINT,
        client_started DOUBLE PRECISION
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
    _DEDUPE_REGISTRY,
    _UNIQUE_REGISTRY,
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
        last_seen  TEXT NOT NULL,
        client_pid     INTEGER,
        client_started REAL
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
    _DEDUPE_REGISTRY,
    _UNIQUE_REGISTRY,
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
    "client_pid", "client_started",
)

#: Columns added after the table first shipped: ``(name, postgres type,
#: sqlite type)``. ``client_pid`` + ``client_started`` are the Claude Code
#: process behind a registration, which is what tells a live holder of
#: an alias from a dead one (``claude_hooks/mailbox/identity.py``).
#: Added by :func:`missing_columns` + :func:`add_column`, never by an
#: unconditional ``ALTER``: every hook process runs the schema path, and
#: on Postgres an ``ALTER TABLE`` takes an exclusive lock even when the
#: column is already there.
ADDED_REGISTRY_COLUMNS = (
    ("client_pid", "BIGINT", "INTEGER"),
    ("client_started", "DOUBLE PRECISION", "REAL"),
)


def missing_columns(cur, dialect: str) -> list[tuple[str, str]]:
    """``(column, type)`` pairs the registry table still lacks."""
    if dialect == "postgres":
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'session_registry' "
            "AND table_schema = current_schema()")
        have = {r[0] for r in cur.fetchall()}
        return [(c, pg) for c, pg, _ in ADDED_REGISTRY_COLUMNS
                if c not in have]
    cur.execute("PRAGMA table_info(session_registry)")
    have = {r[1] for r in cur.fetchall()}
    return [(c, lite) for c, _, lite in ADDED_REGISTRY_COLUMNS
            if c not in have]


def add_column(column: str, coltype: str, dialect: str) -> str:
    # IF NOT EXISTS where the dialect has it: two processes can both see
    # the column missing, and the loser must not fail its schema pass.
    guard = "IF NOT EXISTS " if dialect == "postgres" else ""
    return (f"ALTER TABLE session_registry ADD COLUMN {guard}"
            f"{column} {coltype}")


def statements(dialect: str) -> list[str]:
    if dialect == "postgres":
        return list(_PG)
    if dialect == "sqlite":
        return list(_SQLITE)
    raise ValueError(f"unknown dialect {dialect!r}")
