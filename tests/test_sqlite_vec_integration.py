"""
Integration tests for the SQLite + sqlite-vec provider.

Requires: pip install sqlite-vec
Requires: the claude-hooks embedder (llamafile, qwen3-embedding-0.6b) on
127.0.0.1:38092 — the embedder every provider uses — or, with
OLLAMA_EMBEDDINGS_URL set, an Ollama serving nomic-embed-text (CI hosts
without the llamafile). Skips when neither answers; never spawns one.

Skip with: pytest -k "not integration"
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

# Default: the claude-hooks embedder (2026-11-27: the test used to embed
# with nomic-embed-text on the host's Ollama, ~27 GPU calls per suite run).
# OLLAMA_EMBEDDINGS_URL selects Ollama + nomic-embed-text instead.
OLLAMA_URL = os.environ.get("OLLAMA_EMBEDDINGS_URL", "")
LLAMAFILE_URL = os.environ.get(
    "CLAUDE_HOOKS_TEST_EMBED_URL", "http://127.0.0.1:38092/embedding"
)


def _embedder_options() -> tuple[str, dict]:
    if OLLAMA_URL:
        return "ollama", {"url": OLLAMA_URL, "model": "nomic-embed-text",
                          "timeout": 30.0}
    return "llamafile", {"url": LLAMAFILE_URL, "model": "qwen3-embedding:0.6b",
                         "timeout": 60.0, "daemon_ensure": False}


def _skip_if_no_deps():
    try:
        import sqlite_vec  # noqa: F401
    except ImportError:
        raise unittest.SkipTest("sqlite-vec not installed")
    # Check that the embedder answers (one real embed).
    from claude_hooks.embedders import make_embedder

    name, opts = _embedder_options()
    try:
        if not make_embedder(name, opts).embed("test"):
            raise unittest.SkipTest(f"{name} embedder returned no vector")
    except unittest.SkipTest:
        raise
    except Exception as e:
        raise unittest.SkipTest(f"{name} embedder not reachable at {opts['url']}: {e}")


class TestSqliteVecIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _skip_if_no_deps()
        cls._tmpdir = tempfile.mkdtemp(prefix="claude-hooks-test-")
        cls._db_path = os.path.join(cls._tmpdir, "test_memory.db")

    # Shared fixture corpus. Every recall test seeds its OWN table with
    # this (store is idempotent on content_hash, so re-seeding a table is
    # a no-op) — tests must not depend on another test having populated a
    # table first. They share one db file (``cls._db_path``) but each
    # uses a distinct table, and table creation in a pre-migrated db is
    # exercised by that very sharing.
    MEMORIES = [
        "bcache fix: rebuild superblock with make-bcache --wipe-bcache",
        "nginx proxy config: upstream with fail_timeout=3s for Ollama failover",
        "Docker compose uses network_mode: host to avoid port mapping issues",
        "Python 3.9+ required for dict[str, Any] type hints",
        "Qdrant MCP server runs on port 32775 with streamable HTTP",
    ]

    def _make_provider(self, table: str = "test_mem"):
        from claude_hooks.providers.base import ServerCandidate
        from claude_hooks.providers.sqlite_vec import SqliteVecProvider

        server = ServerCandidate(
            server_key="sqlite_vec",
            url=self._db_path,
            source="test",
            confidence="high",
        )
        name, opts = _embedder_options()
        options = {"table": table, "embedder": name, "embedder_options": opts}
        return SqliteVecProvider(server, options)

    def _seed(self, table: str):
        """Create a provider on ``table`` and populate it with the shared
        corpus. Returns the provider. Self-contained: no test relies on
        another test's stores."""
        prov = self._make_provider(table)
        for m in self.MEMORIES:
            prov.store(m)
        return prov

    def test_01_auto_create_tables(self):
        """First access should create tables automatically."""
        prov = self._make_provider("autocreate")
        prov.store("The bcache subsystem provides SSD caching for HDDs on Linux.")
        self.assertEqual(prov.count(), 1)

    def test_02_store_and_recall(self):
        """Store several memories, recall the most relevant."""
        prov = self._seed("recall_test")
        self.assertEqual(prov.count(), 5)

        # Recall should find bcache-related content first.
        results = prov.recall("bcache SSD caching", k=3)
        self.assertGreater(len(results), 0)
        self.assertIn("bcache", results[0].text.lower())

    def test_03_recall_returns_distance(self):
        """Recall should include distance in metadata."""
        prov = self._seed("recall_test")
        results = prov.recall("nginx proxy", k=2)
        self.assertGreater(len(results), 0)
        self.assertIn("_distance", results[0].metadata)

    def test_03b_recall_returns_table_metadata(self):
        """Recall should surface the source table name so the MCP
        formatter renders ``[<table> dist=X]`` instead of ``[? dist=X]``.
        Regression guard for the cosmetic v1.6.0 ship.
        """
        prov = self._seed("table_meta_test")
        results = prov.recall("nginx proxy", k=2)
        self.assertGreater(len(results), 0)
        self.assertEqual(results[0].metadata.get("_table"), "table_meta_test")

    def test_04_empty_query_returns_empty(self):
        prov = self._make_provider("recall_test")
        self.assertEqual(prov.recall("", k=5), [])
        self.assertEqual(prov.recall("   ", k=5), [])

    def test_05_empty_content_not_stored(self):
        prov = self._make_provider("empty_test")
        prov.store("")
        prov.store("   ")
        self.assertEqual(prov.count(), 0)

    def test_06_metadata_preserved(self):
        """Stored metadata should survive roundtrip."""
        prov = self._make_provider("meta_test")
        prov.store(
            "test memory with metadata",
            metadata={"type": "fix", "session_id": "test-123"},
        )
        results = prov.recall("test memory", k=1)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].metadata.get("type"), "fix")
        self.assertEqual(results[0].metadata.get("session_id"), "test-123")

    def test_07_verify_works(self):
        """verify() should return True for a valid db path."""
        from claude_hooks.providers.base import ServerCandidate
        from claude_hooks.providers.sqlite_vec import SqliteVecProvider

        server = ServerCandidate(
            server_key="sqlite_vec",
            url=self._db_path,
            source="test",
            confidence="high",
        )
        self.assertTrue(SqliteVecProvider.verify(server))

    def test_08_unsafe_table_name_rejected(self):
        """SQL injection via table name should be blocked."""
        from claude_hooks.providers.sqlite_vec import _safe_table

        with self.assertRaises(ValueError):
            _safe_table("memory; DROP TABLE users")
        with self.assertRaises(ValueError):
            _safe_table("Robert'); DROP TABLE--")
        # Valid names should pass.
        self.assertEqual(_safe_table("claude_hooks_memory"), "claude_hooks_memory")

    @classmethod
    def tearDownClass(cls):
        import shutil

        shutil.rmtree(cls._tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
