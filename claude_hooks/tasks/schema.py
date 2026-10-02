"""The task index, in both dialects.

The rows index the files (:mod:`claude_hooks.tasks.files`); they are
never the record. They exist for what files cannot do cheaply: listing
and searching across every project and host, and recall.

Times are ISO-8601 UTC text in both dialects, which sorts correctly and
keeps one set of queries for both. List and map fields are JSON text.

The embedding is a base64 float32 blob in a TEXT column rather than a
``vector(n)``: its dimension follows whichever embedder the host runs,
and a typed column would need a migration every time that changes. A
project holds hundreds to a few thousand tasks, so similarity is
computed in Python over the candidate rows; ``embed_model`` marks rows
whose vector came from another model, which are re-embedded rather than
compared across spaces.
"""
from __future__ import annotations

SCHEMA_VERSION = 1

_COMMON_COLUMNS = """
        project     TEXT NOT NULL,
        id          TEXT NOT NULL,
        prefix      TEXT NOT NULL,
        num         INTEGER NOT NULL,
        title       TEXT NOT NULL,
        status      TEXT NOT NULL,
        priority    TEXT NOT NULL DEFAULT 'M',
        area        TEXT NOT NULL DEFAULT '',
        tags        TEXT NOT NULL DEFAULT '[]',
        depends     TEXT NOT NULL DEFAULT '[]',
        plan        TEXT NOT NULL DEFAULT '',
        due         TEXT NOT NULL DEFAULT '',
        links       TEXT NOT NULL DEFAULT '{}',
        created     TEXT NOT NULL DEFAULT '',
        updated     TEXT NOT NULL DEFAULT '',
        sessions    TEXT NOT NULL DEFAULT '[]',
        description TEXT NOT NULL DEFAULT '',
        acceptance  TEXT NOT NULL DEFAULT '',
        log         TEXT NOT NULL DEFAULT '',
        body        TEXT NOT NULL DEFAULT '',
        root        TEXT NOT NULL DEFAULT '',
        host        TEXT NOT NULL DEFAULT '',
        file_hash   TEXT NOT NULL DEFAULT '',
        embedding   TEXT,
        embed_hash  TEXT,
        embed_model TEXT,
        indexed_at  TEXT NOT NULL DEFAULT '',
"""

_PROJECTS = """
    CREATE TABLE IF NOT EXISTS task_projects (
        project TEXT PRIMARY KEY,
        prefix  TEXT NOT NULL,
        root    TEXT NOT NULL DEFAULT '',
        host    TEXT NOT NULL DEFAULT '',
        updated TEXT NOT NULL DEFAULT ''
    )
"""

_INDEXES = [
    "CREATE INDEX IF NOT EXISTS tasks_status_idx ON tasks (project, status)",
    "CREATE INDEX IF NOT EXISTS tasks_updated_idx ON tasks (updated)",
]

_PG = [
    "CREATE TABLE IF NOT EXISTS tasks (" + _COMMON_COLUMNS
    + "        archived    BOOLEAN NOT NULL DEFAULT FALSE,\n"
      "        PRIMARY KEY (project, id)\n    )",
    _PROJECTS,
    *_INDEXES,
]

_SQLITE = [
    "CREATE TABLE IF NOT EXISTS tasks (" + _COMMON_COLUMNS
    + "        archived    INTEGER NOT NULL DEFAULT 0,\n"
      "        PRIMARY KEY (project, id)\n    )",
    _PROJECTS,
    *_INDEXES,
]

#: Columns written and read back, in one place so the INSERT, every
#: SELECT and the row-to-dict mapping cannot drift apart.
COLUMNS = (
    "project", "id", "prefix", "num", "title", "status", "priority",
    "area", "tags", "depends", "plan", "due", "links", "created",
    "updated", "sessions", "description", "acceptance", "log", "body",
    "root", "host", "file_hash", "archived", "indexed_at",
)

PROJECT_COLUMNS = ("project", "prefix", "root", "host", "updated")


def statements(dialect: str) -> list[str]:
    if dialect == "postgres":
        return list(_PG)
    if dialect == "sqlite":
        return list(_SQLITE)
    raise ValueError(f"unknown dialect {dialect!r}")
