"""Tests for SqliteVecProvider knowledge-graph methods.

Mirror of test_pgvector.py's KG coverage, translated to SQLite:
* kg_create_entities + idempotency
* kg_add_observations + dedup + missing-entity skip
* kg_create_relations + dedup
* kg_search_nodes — name fuzzy + observation hybrid + obs-fill
* FK cascade: deleting an entity removes its observations + relations
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

try:
    import sqlite_vec  # noqa: F401
    HAS_SQLITE_VEC = True
except ImportError:
    HAS_SQLITE_VEC = False

from claude_hooks.embedders import Embedder


class _DeterministicEmbedder(Embedder):
    name = "deterministic"
    dim = 16

    def embed(self, text: str) -> list[float]:
        h = hashlib.sha256(text.encode("utf-8")).digest()
        return [int.from_bytes(h[i:i + 2], "little") / 65535.0
                for i in range(0, 32, 2)]


def _make_provider(db_path: str):
    from claude_hooks.providers.base import ServerCandidate
    from claude_hooks.providers.sqlite_vec import SqliteVecProvider

    server = ServerCandidate(server_key="sqlite_vec", url=db_path)
    p = SqliteVecProvider(server, options={"table": "memory"})
    p._embedder = _DeterministicEmbedder()
    return p


@unittest.skipUnless(HAS_SQLITE_VEC, "sqlite-vec not installed")
class TestKgCreateEntities(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="ch-kg-")
        self.db = os.path.join(self.tmpdir, "t.db")
        self.p = _make_provider(self.db)

    def tearDown(self):
        if self.p._conn is not None:
            self.p._conn.close()

    def test_inserts_new_entities(self):
        n = self.p.kg_create_entities([
            {"name": "solidpc", "entity_type": "server"},
            {"name": "ollama", "entity_type": "service",
             "metadata": {"port": 11434}},
        ])
        self.assertEqual(n, 2)

    def test_idempotent_on_name(self):
        self.p.kg_create_entities([{"name": "x", "entity_type": "t"}])
        n = self.p.kg_create_entities([{"name": "x", "entity_type": "t"}])
        self.assertEqual(n, 0)

    def test_skips_missing_name_or_type(self):
        n = self.p.kg_create_entities([
            {"name": "", "entity_type": "t"},
            {"name": "ok", "entity_type": ""},
            {"name": "good", "entity_type": "t"},
        ])
        self.assertEqual(n, 1)

    def test_metadata_persists(self):
        self.p.kg_create_entities([
            {"name": "m1", "entity_type": "x", "metadata": {"k": "v"}},
        ])
        row = self.p._conn.execute(
            "SELECT metadata FROM kg_entities WHERE name='m1'"
        ).fetchone()
        import json
        self.assertEqual(json.loads(row[0])["k"], "v")


@unittest.skipUnless(HAS_SQLITE_VEC, "sqlite-vec not installed")
class TestKgAddObservations(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="ch-kg-")
        self.db = os.path.join(self.tmpdir, "t.db")
        self.p = _make_provider(self.db)
        self.p.kg_create_entities([
            {"name": "solidpc", "entity_type": "server"},
        ])

    def tearDown(self):
        if self.p._conn is not None:
            self.p._conn.close()

    def test_adds_observation(self):
        n = self.p.kg_add_observations([
            {"entity_name": "solidpc", "content": "runs Ollama on port 11434"},
        ])
        self.assertEqual(n, 1)

    def test_idempotent_on_entity_and_hash(self):
        self.p.kg_add_observations([
            {"entity_name": "solidpc", "content": "fact 1"},
        ])
        n = self.p.kg_add_observations([
            {"entity_name": "solidpc", "content": "fact 1"},
            {"entity_name": "solidpc", "content": "  fact   1  "},  # normalised dup
        ])
        # Hash is whitespace-normalised, so both are dupes.
        self.assertEqual(n, 0)

    def test_skips_missing_entity(self):
        n = self.p.kg_add_observations([
            {"entity_name": "no-such-host", "content": "ignored"},
            {"entity_name": "solidpc", "content": "kept"},
        ])
        self.assertEqual(n, 1)

    def test_observation_embedding_lands_in_vec_table(self):
        self.p.kg_add_observations([
            {"entity_name": "solidpc", "content": "with embedding"},
        ])
        cnt = self.p._conn.execute(
            "SELECT COUNT(*) FROM kg_observations_vec"
        ).fetchone()[0]
        self.assertEqual(cnt, 1)


@unittest.skipUnless(HAS_SQLITE_VEC, "sqlite-vec not installed")
class TestKgCreateRelations(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="ch-kg-")
        self.db = os.path.join(self.tmpdir, "t.db")
        self.p = _make_provider(self.db)
        self.p.kg_create_entities([
            {"name": "solidpc", "entity_type": "server"},
            {"name": "ollama", "entity_type": "service"},
        ])

    def tearDown(self):
        if self.p._conn is not None:
            self.p._conn.close()

    def test_creates_relation(self):
        n = self.p.kg_create_relations([
            {"from": "ollama", "to": "solidpc", "relation_type": "runs_on"},
        ])
        self.assertEqual(n, 1)

    def test_idempotent_on_triple(self):
        rel = {"from": "ollama", "to": "solidpc", "relation_type": "runs_on"}
        self.p.kg_create_relations([rel])
        n = self.p.kg_create_relations([rel])
        self.assertEqual(n, 0)

    def test_skips_unresolvable(self):
        n = self.p.kg_create_relations([
            {"from": "no-host", "to": "ollama", "relation_type": "x"},
        ])
        self.assertEqual(n, 0)


@unittest.skipUnless(HAS_SQLITE_VEC, "sqlite-vec not installed")
class TestKgSearchNodes(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="ch-kg-")
        self.db = os.path.join(self.tmpdir, "t.db")
        self.p = _make_provider(self.db)
        self.p.kg_create_entities([
            {"name": "solidpc", "entity_type": "server"},
            {"name": "pandorum", "entity_type": "server"},
            {"name": "ollama", "entity_type": "service"},
        ])
        self.p.kg_add_observations([
            {"entity_name": "solidpc",
             "content": "bcache cache device on md0 array"},
            {"entity_name": "solidpc",
             "content": "RTX 3090 GPU runs nomic-embed-text"},
            {"entity_name": "ollama",
             "content": "listens on port 11434 for embeddings"},
        ])

    def tearDown(self):
        if self.p._conn is not None:
            self.p._conn.close()

    def test_empty_query_returns_empty(self):
        self.assertEqual(self.p.kg_search_nodes(""), [])

    def test_name_match_surfaces_entity(self):
        hits = self.p.kg_search_nodes("solidpc", k=3)
        names = [h["name"] for h in hits]
        self.assertIn("solidpc", names)

    def test_observation_match_surfaces_entity(self):
        hits = self.p.kg_search_nodes("bcache cache device on md0 array", k=3)
        names = [h["name"] for h in hits]
        self.assertIn("solidpc", names)

    def test_returns_observations_for_name_match(self):
        hits = self.p.kg_search_nodes("solidpc", k=1)
        self.assertEqual(len(hits), 1)
        self.assertTrue(len(hits[0]["observations"]) > 0)

    def test_does_not_leak_internal_id(self):
        hits = self.p.kg_search_nodes("solidpc", k=1)
        self.assertTrue(hits)
        self.assertNotIn("id", hits[0])


@unittest.skipUnless(HAS_SQLITE_VEC, "sqlite-vec not installed")
class TestForeignKeyCascade(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="ch-kg-")
        self.db = os.path.join(self.tmpdir, "t.db")
        self.p = _make_provider(self.db)
        self.p.kg_create_entities([
            {"name": "a", "entity_type": "t"},
            {"name": "b", "entity_type": "t"},
        ])
        self.p.kg_add_observations([
            {"entity_name": "a", "content": "obs-a-1"},
            {"entity_name": "a", "content": "obs-a-2"},
        ])
        self.p.kg_create_relations([
            {"from": "a", "to": "b", "relation_type": "links_to"},
        ])

    def tearDown(self):
        if self.p._conn is not None:
            self.p._conn.close()

    def test_delete_entity_cascades(self):
        # delete entity 'a' → its observations + relations gone
        self.p._conn.execute("DELETE FROM kg_entities WHERE name='a'")
        self.p._conn.commit()

        obs = self.p._conn.execute(
            "SELECT COUNT(*) FROM kg_observations"
        ).fetchone()[0]
        rels = self.p._conn.execute(
            "SELECT COUNT(*) FROM kg_relations"
        ).fetchone()[0]
        self.assertEqual(obs, 0)
        self.assertEqual(rels, 0)


@unittest.skipUnless(HAS_SQLITE_VEC, "sqlite-vec not installed")
class TestProviderBaseDefaults(unittest.TestCase):

    def test_recall_provider_kg_methods_raise(self):
        # Verifies the Provider ABC defaults — providers that don't
        # implement KG still load cleanly but surface a clean error.
        from claude_hooks.providers.qdrant import QdrantProvider
        from claude_hooks.providers.base import ServerCandidate

        srv = ServerCandidate(server_key="qdrant", url="http://x/mcp")
        q = QdrantProvider(srv, options={})
        with self.assertRaises(NotImplementedError):
            q.kg_create_entities([])
        with self.assertRaises(NotImplementedError):
            q.kg_add_observations([])
        with self.assertRaises(NotImplementedError):
            q.kg_create_relations([])
        with self.assertRaises(NotImplementedError):
            q.kg_search_nodes("x")


if __name__ == "__main__":
    unittest.main()
