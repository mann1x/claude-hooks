"""Tests for ``consultants.engine.checkpointer.make_checkpointer``.

These tests need langgraph + langgraph-checkpoint-sqlite installed, so
they live in the consultants test env. Skip cleanly when imports fail
(matches the convention in ``test_langgraph_smoke.py``).

The companion smoke at ``tests/test_langgraph_smoke.py`` already pins
the SqliteSaver + InMemorySaver + PostgresSaver-import surface. Here
we test the *factory* glue:

- `make_checkpointer(cfg, cwd, sid)` returns a working handle.
- SQLite path lives under
  ``<cwd>/.claude-hooks/consultants/<sid>/checkpoints.db`` exactly.
- `close()` is idempotent.
- Postgres path raises a useful error when the [postgres] extra is
  missing or when no pool is provided.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


try:
    from langgraph.graph import StateGraph, START, END
    from langgraph.checkpoint.sqlite import SqliteSaver  # noqa: F401
    HAVE_LANGGRAPH = True
except ImportError:
    HAVE_LANGGRAPH = False


@unittest.skipUnless(HAVE_LANGGRAPH, "langgraph not installed")
class TestSqliteCheckpointer(unittest.TestCase):

    def test_default_backend_is_sqlite(self):
        from consultants.engine.checkpointer import CheckpointerConfig
        cfg = CheckpointerConfig()
        self.assertEqual(cfg.backend, "sqlite")
        self.assertIsNone(cfg.url)

    def test_make_creates_db_file_under_session_dir(self):
        from consultants.engine.checkpointer import (
            CheckpointerConfig, make_checkpointer,
        )
        with tempfile.TemporaryDirectory() as d:
            cwd = Path(d)
            sid = "csl-2026-05-16-1200-abcd"
            cfg = CheckpointerConfig(backend="sqlite")
            handle = make_checkpointer(cfg, cwd, sid)
            try:
                self.assertEqual(handle.backend, "sqlite")
                # Path is deterministic.
                expected = (cwd / ".claude-hooks" / "consultants" /
                            sid / "checkpoints.db")
                self.assertTrue(expected.exists(),
                                f"expected {expected} to be created")
                self.assertTrue(expected.is_file())
            finally:
                handle.close()

    def test_compile_and_invoke_against_handle(self):
        """End-to-end: a tiny graph compiled with the handle's saver
        actually persists checkpoints to the SQLite file."""
        from consultants.engine.checkpointer import (
            CheckpointerConfig, make_checkpointer,
        )
        from typing import TypedDict

        class S(TypedDict, total=False):
            counter: int

        def inc(state):
            return {"counter": (state.get("counter", 0) or 0) + 1}

        with tempfile.TemporaryDirectory() as d:
            cwd = Path(d)
            sid = "csl-test-cp"
            handle = make_checkpointer(CheckpointerConfig(), cwd, sid)
            try:
                sg = StateGraph(S)
                sg.add_node("inc", inc)
                sg.add_edge(START, "inc")
                sg.add_edge("inc", END)
                g = sg.compile(checkpointer=handle.saver)
                cfg = {"configurable": {"thread_id": "t-1"}}
                out = g.invoke({"counter": 0}, config=cfg)
                self.assertEqual(out["counter"], 1)
                # Snapshot is recoverable.
                snap = g.get_state(cfg)
                self.assertEqual(snap.values["counter"], 1)
            finally:
                handle.close()

    def test_close_is_idempotent(self):
        from consultants.engine.checkpointer import (
            CheckpointerConfig, make_checkpointer,
        )
        with tempfile.TemporaryDirectory() as d:
            handle = make_checkpointer(CheckpointerConfig(), Path(d), "csl-x")
            handle.close()
            # Second close must not raise.
            handle.close()

    def test_context_manager_protocol(self):
        from consultants.engine.checkpointer import (
            CheckpointerConfig, make_checkpointer,
        )
        with tempfile.TemporaryDirectory() as d:
            handle = make_checkpointer(CheckpointerConfig(), Path(d), "csl-cm")
            with handle as saver:
                # The yielded value is the underlying saver — the
                # type LangGraph's compile() takes.
                self.assertIsNotNone(saver)
            # Exit closes the handle.
            self.assertTrue(handle._closed)

    def test_separate_sessions_get_separate_files(self):
        from consultants.engine.checkpointer import (
            CheckpointerConfig, make_checkpointer,
        )
        with tempfile.TemporaryDirectory() as d:
            cwd = Path(d)
            h1 = make_checkpointer(CheckpointerConfig(), cwd, "csl-1")
            h2 = make_checkpointer(CheckpointerConfig(), cwd, "csl-2")
            try:
                # Two distinct files.
                base = cwd / ".claude-hooks" / "consultants"
                self.assertTrue((base / "csl-1" / "checkpoints.db").exists())
                self.assertTrue((base / "csl-2" / "checkpoints.db").exists())
            finally:
                h1.close()
                h2.close()


class TestUnknownBackend(unittest.TestCase):

    def test_unknown_backend_raises_valueerror(self):
        from consultants.engine.checkpointer import (
            CheckpointerConfig, make_checkpointer,
        )
        cfg = CheckpointerConfig(backend="redis")
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError) as cm:
                make_checkpointer(cfg, Path(d), "csl-x")
        self.assertIn("redis", str(cm.exception))
        self.assertIn("sqlite", str(cm.exception))  # mentions valid options


@unittest.skipUnless(HAVE_LANGGRAPH, "langgraph not installed")
class TestPostgresPath(unittest.TestCase):
    """Postgres errors clearly when no pool is provided OR when the
    [postgres] extra is missing. We don't drive a real Postgres here
    — that's the M1 follow-up integration smoke; here we just pin
    the error paths."""

    def test_postgres_without_url_raises_valueerror(self):
        from consultants.engine.checkpointer import (
            CheckpointerConfig, make_postgres_pool,
        )
        cfg = CheckpointerConfig(backend="postgres", url=None)
        with self.assertRaises(ValueError) as cm:
            make_postgres_pool(cfg)
        self.assertIn("url is empty", str(cm.exception))

    def test_postgres_no_pool_raises_runtime_error(self):
        from consultants.engine.checkpointer import (
            CheckpointerConfig, make_checkpointer,
        )
        cfg = CheckpointerConfig(backend="postgres",
                                 url="postgresql://x@localhost/db")
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(RuntimeError) as cm:
                make_checkpointer(cfg, Path(d), "csl-x",
                                  postgres_pool=None)
        # The error names the install hint OR the missing-pool case.
        msg = str(cm.exception)
        self.assertTrue(
            "no postgres_pool" in msg
            or "psycopg" in msg
            or "[postgres]" in msg,
            f"unexpected error message: {msg!r}",
        )

    def test_postgres_missing_extras_raises_runtime_error(self):
        """When psycopg_pool isn't installed, make_postgres_pool
        surfaces the install hint cleanly. We simulate the absence
        by patching the import to fail."""
        from consultants.engine.checkpointer import (
            CheckpointerConfig, make_postgres_pool,
        )
        cfg = CheckpointerConfig(
            backend="postgres",
            url="postgresql://x@localhost/db",
        )
        # Force ImportError on psycopg_pool to simulate missing extra.
        import sys
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "psycopg_pool":
                raise ImportError(f"No module named '{name}'")
            return real_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", side_effect=fake_import):
            # Also strip any already-cached module so the import
            # actually re-runs.
            sys.modules.pop("psycopg_pool", None)
            with self.assertRaises(RuntimeError) as cm:
                make_postgres_pool(cfg)
        self.assertIn("[postgres]", str(cm.exception))


class TestConfigIntegration(unittest.TestCase):
    """Verify the checkpointer block round-trips through the config
    file loader + emitter."""

    def test_default_config_has_sqlite_checkpointer(self):
        from consultants.config import ConsultantsConfig
        cfg = ConsultantsConfig()
        self.assertEqual(cfg.checkpointer.backend, "sqlite")
        self.assertIsNone(cfg.checkpointer.url)

    def test_toml_round_trip_sqlite(self):
        """Save a config with the default SQLite backend; reload it;
        the checkpointer block is unchanged."""
        from consultants import config as cc
        with tempfile.TemporaryDirectory() as d:
            cwd = Path(d)
            cfg = cc.ConsultantsConfig()
            path = cc.save_config(cfg, scope="project", cwd=cwd)
            self.assertTrue(path.exists())
            text = path.read_text(encoding="utf-8")
            self.assertIn("[checkpointer]", text)
            self.assertIn('backend = "sqlite"', text)
            reloaded = cc.load_config(cwd=cwd)
            self.assertEqual(reloaded.checkpointer.backend, "sqlite")
            self.assertIsNone(reloaded.checkpointer.url)

    def test_toml_round_trip_postgres(self):
        from consultants import config as cc
        with tempfile.TemporaryDirectory() as d:
            cwd = Path(d)
            cfg = cc.ConsultantsConfig()
            cfg.checkpointer.backend = "postgres"
            cfg.checkpointer.url = (
                "postgresql://claude_hooks@192.168.178.2:5433/db"
            )
            cfg.checkpointer.postgres_pool_max = 20
            path = cc.save_config(cfg, scope="project", cwd=cwd)
            text = path.read_text(encoding="utf-8")
            self.assertIn('backend = "postgres"', text)
            self.assertIn("postgresql://claude_hooks", text)
            reloaded = cc.load_config(cwd=cwd)
            self.assertEqual(reloaded.checkpointer.backend, "postgres")
            self.assertEqual(
                reloaded.checkpointer.url,
                "postgresql://claude_hooks@192.168.178.2:5433/db",
            )
            self.assertEqual(reloaded.checkpointer.postgres_pool_max, 20)

    def test_invalid_backend_in_toml_falls_back_to_default(self):
        """A bad value in the file shouldn't crash the loader — it's
        a graceful fallback, same as every other config key."""
        from consultants import config as cc
        with tempfile.TemporaryDirectory() as d:
            cwd = Path(d)
            project_path = cc.project_config_path(cwd)
            project_path.parent.mkdir(parents=True, exist_ok=True)
            project_path.write_text(
                'topology = "council"\n'
                'effort = "medium"\n'
                '[checkpointer]\n'
                'backend = "redis"\n',
                encoding="utf-8",
            )
            reloaded = cc.load_config(cwd=cwd)
            self.assertEqual(reloaded.checkpointer.backend, "sqlite")


if __name__ == "__main__":
    unittest.main()
