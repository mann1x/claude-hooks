"""
Integration tests for the SQLite + sqlite-vec provider.

Requires: pip install sqlite-vec
Requires: Ollama running with nomic-embed-text model

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

# Ollama embeddings URL. Override with OLLAMA_EMBEDDINGS_URL env var to
# point at a remote Ollama (CI often runs one on a LAN host). Default
# assumes a local Ollama on the stdlib port.
OLLAMA_URL = os.environ.get(
    "OLLAMA_EMBEDDINGS_URL", "http://localhost:11434/api/embeddings"
)


def _skip_if_no_deps():
    try:
        import sqlite_vec  # noqa: F401
    except ImportError:
        raise unittest.SkipTest("sqlite-vec not installed")
    # Check that Ollama is reachable with the embedding model.
    import urllib.request
    import json

    try:
        body = json.dumps({"model": "nomic-embed-text", "prompt": "test"}).encode()
        req = urllib.request.Request(
            OLLAMA_URL,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            if "embedding" not in data:
                raise unittest.SkipTest("Ollama nomic-embed-text not available")
    except Exception as e:
        raise unittest.SkipTest(f"Ollama not reachable at {OLLAMA_URL}: {e}")


class TestSqliteVecIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _skip_if_no_deps()
        cls._tmpdir = tempfile.mkdtemp(prefix="claude-hooks-test-")
        cls._db_path = os.path.join(cls._tmpdir, "test_memory.db")

    def _make_provider(self, table: str = "test_mem"):
        from claude_hooks.providers.base import ServerCandidate
        from claude_hooks.providers.sqlite_vec import SqliteVecProvider

        server = ServerCandidate(
            server_key="sqlite_vec",
            url=self._db_path,
            source="test",
            confidence="high",
        )
        options = {
            "table": table,
            "embedder": "ollama",
            "embedder_options": {
                "url": OLLAMA_URL,
                "model": "nomic-embed-text",
                "timeout": 30.0,
            },
        }
        return SqliteVecProvider(server, options)

    def test_01_auto_create_tables(self):
        """First access should create tables automatically."""
        prov = self._make_provider("autocreate")
        prov.store("The bcache subsystem provides SSD caching for HDDs on Linux.")
        self.assertEqual(prov.count(), 1)

    def test_02_store_and_recall(self):
        """Store several memories, recall the most relevant."""
        prov = self._make_provider("recall_test")

        memories = [
            "bcache fix: rebuild superblock with make-bcache --wipe-bcache",
            "nginx proxy config: upstream with fail_timeout=3s for Ollama failover",
            "Docker compose uses network_mode: host to avoid port mapping issues",
            "Python 3.9+ required for dict[str, Any] type hints",
            "Qdrant MCP server runs on port 32775 with streamable HTTP",
        ]
        for m in memories:
            prov.store(m)
        self.assertEqual(prov.count(), 5)

        # Recall should find bcache-related content first.
        results = prov.recall("bcache SSD caching", k=3)
        self.assertGreater(len(results), 0)
        self.assertIn("bcache", results[0].text.lower())

    def test_03_recall_returns_distance(self):
        """Recall should include distance in metadata."""
        prov = self._make_provider("recall_test")
        results = prov.recall("nginx proxy", k=2)
        self.assertGreater(len(results), 0)
        self.assertIn("_distance", results[0].metadata)

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
