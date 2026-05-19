"""Checkpointer factory for the v2 council.

Single ``make_checkpointer(cfg, cwd, sid, *, postgres_pool=None)``
returning a ``CheckpointerHandle`` that the server holds for the
session's lifetime. Two backends:

- **SQLite (default)** — per-session file under
  ``<cwd>/.claude-hooks/consultants/<sid>/checkpoints.db``. Zero
  dependency (sqlite3 is stdlib). One file per session keeps disk
  layout self-contained: deleting the session directory cleans up
  the checkpoint too.

- **Postgres (opt-in)** — shared connection pool managed at FastAPI
  lifespan scope (the runner builds the pool once, every
  ``make_checkpointer`` call reuses it). Requires
  ``langgraph-checkpoint-postgres`` + ``psycopg`` from the
  ``[postgres]`` extra; absent → clear error pointing at the install
  command.

The factory deliberately returns a **handle** (not a bare saver)
because SQLite's connection lifecycle is per-session and Postgres's
is per-app; the handle's ``close()`` abstracts the difference. The
runner stores the handle on the SessionState and calls ``.close()``
in the session's cleanup path.

Both backends call ``saver.setup()`` on first use. The call is
idempotent on both — safe to invoke on every session start.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional


log = logging.getLogger("consultants.engine.checkpointer")


# ----------------------- config types ---------------------------- #

@dataclass
class CheckpointerConfig:
    """Subset of ``ConsultantsConfig`` the checkpointer cares about.
    Lives as its own type so tests can construct it without building
    a full ``ConsultantsConfig`` mock."""

    backend: str = "sqlite"          # "sqlite" | "postgres"
    # Postgres connection URL. Honored only when backend == "postgres".
    # Format: postgresql://user:pass@host:port/dbname  OR
    # postgresql+psycopg://...
    url: Optional[str] = None
    # Per-backend tuning knobs (set on the dataclass so config files
    # can populate them; sensible defaults so the common path needs
    # nothing).
    postgres_pool_min: int = 1
    postgres_pool_max: int = 10
    postgres_pool_timeout_s: float = 30.0


# ----------------------- handle ---------------------------------- #

class CheckpointerHandle:
    """Wraps a ``BaseCheckpointSaver`` with an explicit ``close()``.

    Why not a context manager exclusively: LangGraph's compile pattern
    is ``builder.compile(checkpointer=saver)`` where ``saver`` lives
    for the full session — well past any ``with`` block we could put
    around it. The handle pattern lets the runner store it on
    SessionState and call ``.close()`` in the session-end path.

    Context-manager support is also provided for tests / one-shot
    invocations.
    """

    def __init__(self, saver: Any, closer: Callable[[], None],
                 *, backend: str, label: str = ""):
        self.saver = saver
        self._closer = closer
        self.backend = backend
        self.label = label
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._closer()
        except Exception as exc:  # pragma: no cover — defensive
            log.warning(
                "CheckpointerHandle(%s).close raised: %s",
                self.label, exc,
            )
        self._closed = True

    def __enter__(self):
        return self.saver

    def __exit__(self, *exc):
        self.close()


# ----------------------- SQLite path ----------------------------- #

def _make_sqlite_handle(*, cwd: Path, sid: str) -> CheckpointerHandle:
    """Open a per-session SQLite checkpoint DB under
    ``<cwd>/.claude-hooks/consultants/<sid>/checkpoints.db``.

    Uses ``check_same_thread=False`` because LangGraph's async
    runtime may invoke the saver from multiple loop threads in a
    server context. SQLite + WAL mode handles this fine.
    """
    from langgraph.checkpoint.sqlite import SqliteSaver

    db_path = cwd / ".claude-hooks" / "consultants" / sid / "checkpoints.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    # WAL + busy_timeout: better concurrency for the stream-while-
    # writing pattern. Same setup the recorder uses.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    saver = SqliteSaver(conn)
    saver.setup()
    return CheckpointerHandle(
        saver, conn.close,
        backend="sqlite",
        label=str(db_path),
    )


# ----------------------- Postgres path --------------------------- #
# The pool lives at FastAPI lifespan scope. Building it requires the
# [postgres] extra. The pool object is returned by
# ``make_postgres_pool`` and threaded into ``make_checkpointer``;
# every per-session saver reuses it.

_POSTGRES_DEPS_ERROR = (
    "postgres checkpointer backend requested but the [postgres] "
    "extra is not installed. Install it with:\n"
    "  pip install -e 'consultants[postgres]'\n"
    "...inside the claude-hooks-consultants conda env."
)


def make_postgres_pool(cfg: CheckpointerConfig):
    """Build a shared ``AsyncConnectionPool`` for the Postgres
    checkpointer. Call once at app startup; thread the returned pool
    through every ``make_checkpointer(cfg, ..., postgres_pool=...)``
    call. Close at app shutdown.

    Raises ``RuntimeError`` if the [postgres] extra isn't installed,
    with a copy-pasteable install hint.
    """
    if not cfg.url:
        raise ValueError(
            "postgres backend requested but cfg.url is empty"
        )
    try:
        from psycopg_pool import ConnectionPool
    except ImportError as exc:
        raise RuntimeError(_POSTGRES_DEPS_ERROR) from exc

    # Sync pool because LangGraph's PostgresSaver accepts a sync
    # connection-pool interface; the saver itself runs on a worker
    # thread under the async runtime. AsyncPostgresSaver is its own
    # type with its own pool (psycopg_pool.AsyncConnectionPool); we
    # default to the sync variant for simplicity and because the
    # checkpointer call cadence is low (per-superstep), not per-token.
    pool = ConnectionPool(
        conninfo=cfg.url,
        min_size=cfg.postgres_pool_min,
        max_size=cfg.postgres_pool_max,
        timeout=cfg.postgres_pool_timeout_s,
        open=False,
    )
    pool.open(wait=True, timeout=cfg.postgres_pool_timeout_s)
    return pool


def _make_postgres_handle(*, cfg: CheckpointerConfig,
                          postgres_pool: Any) -> CheckpointerHandle:
    """Wrap a PostgresSaver around a shared pool. The pool's
    lifecycle is owned by the app; the handle's close() is a no-op
    so the pool survives across sessions."""
    try:
        from langgraph.checkpoint.postgres import PostgresSaver
    except ImportError as exc:
        raise RuntimeError(_POSTGRES_DEPS_ERROR) from exc

    if postgres_pool is None:
        raise RuntimeError(
            "postgres backend requested but no postgres_pool was "
            "passed to make_checkpointer; build the pool with "
            "make_postgres_pool(cfg) at app startup and thread it "
            "through."
        )

    saver = PostgresSaver(postgres_pool)
    # setup() is idempotent; safe to call every session start.
    saver.setup()
    return CheckpointerHandle(
        saver, lambda: None,            # pool lifecycle is app-wide
        backend="postgres",
        label=cfg.url or "postgres",
    )


# ----------------------- factory --------------------------------- #

def make_checkpointer(cfg: CheckpointerConfig, cwd: Path, sid: str,
                      *, postgres_pool: Optional[Any] = None,
                      ) -> CheckpointerHandle:
    """Return a ``CheckpointerHandle`` for the session.

    Parameters
    ----------
    cfg : CheckpointerConfig
        Backend selection + per-backend tuning. Comes from
        ``ConsultantsConfig.checkpointer``.
    cwd : Path
        Project root. SQLite path is rooted under
        ``<cwd>/.claude-hooks/consultants/<sid>/``.
    sid : str
        Session id (``csl-...``). Used to build the per-session
        SQLite path and for log labels.
    postgres_pool : Any | None
        Shared ``ConnectionPool`` for the Postgres backend. Ignored
        when ``cfg.backend == "sqlite"``. **Required** when
        ``cfg.backend == "postgres"``; build with
        ``make_postgres_pool(cfg)`` at app startup.

    Raises
    ------
    ValueError
        Unknown backend.
    RuntimeError
        Postgres requested but ``[postgres]`` extra missing or no
        pool provided.
    """
    backend = (cfg.backend or "sqlite").lower()
    if backend == "sqlite":
        return _make_sqlite_handle(cwd=Path(cwd), sid=sid)
    if backend == "postgres":
        return _make_postgres_handle(cfg=cfg, postgres_pool=postgres_pool)
    raise ValueError(
        f"unknown checkpointer backend: {backend!r}. "
        f"Valid: 'sqlite' | 'postgres'"
    )
