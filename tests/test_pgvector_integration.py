"""
Integration tests for the Postgres + pgvector provider.

Requires:
  - pip install psycopg[binary]
  - Ollama running with nomic-embed-text model
  - Postgres with pgvector extension (e.g. docker run pgvector/pgvector:pg17)

Set PGVECTOR_DSN env var to override the default connection string.
Skip with: pytest -k "not integration"
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

# Connection defaults — override with env vars for CI.
PGVECTOR_DSN = os.environ.get(
    "PGVECTOR_DSN", "postgresql://claude:hooks@localhost:5433/memory"
)
OLLAMA_URL = os.environ.get(
    "OLLAMA_EMBEDDINGS_URL", "http://192.168.178.2:11433/api/embeddings"
)

# Use a unique table name per test run to avoid collisions.
import time

TEST_TABLE = f"test_mem_{int(time.time()) % 100000}"


def _skip_if_no_deps():
    try:
        import psycopg  # noqa: F401
    except ImportError:
        raise unittest.SkipTest("psycopg not installed")

    # Check Postgres is reachable.
    try:
        conn = psycopg.connect(PGVECTOR_DSN, connect_timeout=3)
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_extension WHERE extname='vector'")
            if cur.fetchone() is None:
                conn.close()
                raise unittest.SkipTest("pgvector extension not installed")
        conn.close()
    except psycopg.OperationalError as e:
        raise unittest.SkipTest(f"Postgres not reachable at {PGVECTOR_DSN}: {e}")

    # Check Ollama.
    import json
    import urllib.request

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


class TestPgvectorIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _skip_if_no_deps()

    def _make_provider(self, table: str = TEST_TABLE):
        from claude_hooks.providers.base import ServerCandidate
        from claude_hooks.providers.pgvector import PgvectorProvider

        server = ServerCandidate(
            server_key="pgvector",
            url=PGVECTOR_DSN,
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
        return PgvectorProvider(server, options)

    def test_01_auto_create_table(self):
        """First access should create the table + index automatically."""
        prov = self._make_provider()
        prov.store("The bcache subsystem provides SSD caching for HDDs on Linux.")
        self.assertEqual(prov.count(), 1)

    def test_02_store_and_recall(self):
        """Store several memories, recall the most relevant."""
        prov = self._make_provider()

        memories = [
            "nginx proxy config: upstream with fail_timeout=3s for Ollama failover",
            "Docker compose uses network_mode: host to avoid port mapping issues",
            "Python 3.9+ required for dict[str, Any] type hints",
            "Qdrant MCP server runs on port 32775 with streamable HTTP",
        ]
        for m in memories:
            prov.store(m)
        # 1 from test_01 + 4 = 5
        self.assertEqual(prov.count(), 5)

        # Recall should find bcache-related content first.
        results = prov.recall("bcache SSD caching", k=3)
        self.assertGreater(len(results), 0)
        self.assertIn("bcache", results[0].text.lower())

    def test_03_recall_returns_distance(self):
        """Recall should include distance in metadata."""
        prov = self._make_provider()
        results = prov.recall("nginx proxy", k=2)
        self.assertGreater(len(results), 0)
        self.assertIn("_distance", results[0].metadata)

    def test_04_empty_query_returns_empty(self):
        prov = self._make_provider()
        self.assertEqual(prov.recall("", k=5), [])
        self.assertEqual(prov.recall("   ", k=5), [])

    def test_05_empty_content_not_stored(self):
        count_before = self._make_provider().count()
        prov = self._make_provider()
        prov.store("")
        prov.store("   ")
        self.assertEqual(prov.count(), count_before)

    def test_06_metadata_preserved(self):
        """Stored JSONB metadata should survive roundtrip."""
        prov = self._make_provider()
        prov.store(
            "unique metadata test entry for pgvector roundtrip",
            metadata={"type": "fix", "session_id": "pg-test-123"},
        )
        results = prov.recall("unique metadata test entry", k=1)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].metadata.get("type"), "fix")
        self.assertEqual(results[0].metadata.get("session_id"), "pg-test-123")

    def test_07_verify_works(self):
        """verify() should return True for a valid DSN."""
        from claude_hooks.providers.base import ServerCandidate
        from claude_hooks.providers.pgvector import PgvectorProvider

        server = ServerCandidate(
            server_key="pgvector",
            url=PGVECTOR_DSN,
            source="test",
            confidence="high",
        )
        self.assertTrue(PgvectorProvider.verify(server))

    def test_08_unsafe_table_name_rejected(self):
        """SQL injection via table name should be blocked."""
        from claude_hooks.providers.pgvector import _safe_table

        with self.assertRaises(ValueError):
            _safe_table("memory; DROP TABLE users")
        self.assertEqual(_safe_table("claude_hooks_memory"), "claude_hooks_memory")

    @classmethod
    def tearDownClass(cls):
        """Clean up the test table."""
        try:
            import psycopg

            conn = psycopg.connect(PGVECTOR_DSN, connect_timeout=3)
            with conn.cursor() as cur:
                cur.execute(f"DROP TABLE IF EXISTS {TEST_TABLE} CASCADE")
            conn.commit()
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    unittest.main()
