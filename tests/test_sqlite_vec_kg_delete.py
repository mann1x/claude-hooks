"""sqlite_vec KG removal + enumeration, against a real SQLite database.

The fakes in ``test_mcp_admin_surface`` prove the MCP dispatch calls the
right provider method. These prove the SQL underneath actually removes
what it claims, which on this backend rests on two things that were
quietly untrue until v1.14.1:

* ``PRAGMA foreign_keys`` is connection-scoped and defaults to OFF, so
  ``ON DELETE CASCADE`` was inert on every live provider connection.
* ``<table>_vec`` had no delete trigger, so every removal kept its
  embedding — invisible behind an inner join until SQLite re-issued the
  freed rowid.
"""
import hashlib
import os
import shutil
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
class _Base(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="ch-kgdel-")
        self.db = os.path.join(self.tmpdir, "t.db")
        self.p = _make_provider(self.db)

    def tearDown(self):
        try:
            if self.p._conn is not None:
                self.p._conn.close()
        except Exception:
            pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _seed(self):
        self.p.kg_create_entities([
            {"name": "alpha", "entity_type": "host"},
            {"name": "beta", "entity_type": "host"},
        ])
        self.p.kg_add_observations([
            {"entity_name": "alpha", "content": "alpha has twelve cores"},
            {"entity_name": "alpha", "content": "alpha runs the proxy"},
            {"entity_name": "beta", "content": "beta is the windows canary"},
        ])
        self.p.kg_create_relations([
            {"from": "alpha", "to": "beta", "relation_type": "replicates_to"},
        ])

    def _counts(self):
        c = self.p._conn
        return (
            c.execute("SELECT COUNT(*) FROM kg_entities").fetchone()[0],
            c.execute("SELECT COUNT(*) FROM kg_observations").fetchone()[0],
            c.execute("SELECT COUNT(*) FROM kg_relations").fetchone()[0],
            c.execute("SELECT COUNT(*) FROM kg_observations_vec").fetchone()[0],
        )


class TestForeignKeysAreOn(_Base):
    def test_pragma_is_enabled_on_the_live_connection(self):
        """Without this the cascade below is silently a no-op."""
        self.p._ensure_ready()
        on = self.p._conn.execute("PRAGMA foreign_keys").fetchone()[0]
        self.assertEqual(on, 1)


class TestEntityDeletion(_Base):
    def test_cascade_removes_observations_and_relations(self):
        self._seed()
        self.assertEqual(self._counts()[:3], (2, 3, 1))
        res = self.p.kg_delete_entities(["alpha"])
        self.assertEqual(res["entities"], 1)
        self.assertEqual(res["observations"], 2)
        self.assertEqual(res["relations"], 1)
        ents, obs, rels, _ = self._counts()
        self.assertEqual((ents, obs, rels), (1, 1, 0))

    def test_cascade_also_clears_the_vector_mirror(self):
        """An orphaned embedding is invisible behind the join, then
        answers for whatever inherits its rowid."""
        self._seed()
        self.assertEqual(self._counts()[3], 3)
        self.p.kg_delete_entities(["alpha", "beta"])
        self.assertEqual(self._counts()[3], 0)

    def test_missing_names_are_reported_not_swallowed(self):
        self._seed()
        res = self.p.kg_delete_entities(["alpha", "ghost"])
        self.assertEqual(res["missing"], ["ghost"])
        self.assertEqual(res["entities"], 1)

    def test_deleting_nothing_is_a_no_op(self):
        self._seed()
        before = self._counts()
        res = self.p.kg_delete_entities([])
        self.assertEqual(res["entities"], 0)
        self.assertEqual(self._counts(), before)

    def test_unknown_name_alone_changes_nothing(self):
        self._seed()
        before = self._counts()
        res = self.p.kg_delete_entities(["nope"])
        self.assertEqual(res, {"entities": 0, "observations": 0,
                               "relations": 0, "missing": ["nope"]})
        self.assertEqual(self._counts(), before)


class TestObservationDeletion(_Base):
    def test_deletes_by_entity_and_content(self):
        self._seed()
        n = self.p.kg_delete_observations([
            {"entity_name": "alpha", "content": "alpha runs the proxy"}])
        self.assertEqual(n, 1)
        self.assertEqual(self._counts()[1], 2)

    def test_matches_on_normalised_hash_not_raw_text(self):
        """Same shape as kg_add_observations, so the call that created
        an observation removes it — whitespace included."""
        self._seed()
        n = self.p.kg_delete_observations([
            {"entity_name": "alpha", "content": "  alpha runs the proxy  "}])
        self.assertEqual(n, 1)

    def test_wrong_entity_does_not_delete_a_matching_body(self):
        self._seed()
        n = self.p.kg_delete_observations([
            {"entity_name": "beta", "content": "alpha runs the proxy"}])
        self.assertEqual(n, 0)
        self.assertEqual(self._counts()[1], 3)

    def test_entity_survives_its_observation(self):
        self._seed()
        self.p.kg_delete_observations([
            {"entity_name": "alpha", "content": "alpha runs the proxy"}])
        self.assertEqual(self._counts()[0], 2)


class TestRelationDeletion(_Base):
    def test_deletes_the_named_relation(self):
        self._seed()
        n = self.p.kg_delete_relations([
            {"from": "alpha", "to": "beta", "relation_type": "replicates_to"}])
        self.assertEqual(n, 1)
        self.assertEqual(self._counts()[2], 0)

    def test_entities_are_untouched(self):
        self._seed()
        self.p.kg_delete_relations([
            {"from": "alpha", "to": "beta", "relation_type": "replicates_to"}])
        self.assertEqual(self._counts()[0], 2)

    def test_wrong_direction_is_not_a_match(self):
        self._seed()
        n = self.p.kg_delete_relations([
            {"from": "beta", "to": "alpha", "relation_type": "replicates_to"}])
        self.assertEqual(n, 0)


class TestEnumeration(_Base):
    def test_read_graph_counts_observations_and_relations(self):
        self._seed()
        g = {n["name"]: n for n in self.p.kg_read_graph()}
        self.assertEqual(g["alpha"]["observation_count"], 2)
        self.assertEqual(g["alpha"]["relation_count"], 1)
        self.assertEqual(g["beta"]["observation_count"], 1)

    def test_read_graph_on_an_empty_graph(self):
        self.assertEqual(self.p.kg_read_graph(), [])

    def test_open_nodes_returns_full_detail(self):
        self._seed()
        nodes = self.p.kg_open_nodes(["alpha"])
        self.assertEqual(len(nodes), 1)
        self.assertEqual(len(nodes[0]["observations"]), 2)
        self.assertEqual(nodes[0]["relations"][0]["relation_type"],
                         "replicates_to")

    def test_open_nodes_is_exact_not_fuzzy(self):
        """A near-match returning a neighbour is how the wrong node
        gets edited."""
        self._seed()
        self.assertEqual(self.p.kg_open_nodes(["alph"]), [])

    def test_open_nodes_skips_names_that_do_not_exist(self):
        self._seed()
        names = [n["name"] for n in self.p.kg_open_nodes(["alpha", "ghost"])]
        self.assertEqual(names, ["alpha"])


class TestMemoryVectorMirror(_Base):
    def test_delete_by_hashes_clears_the_vector(self):
        self.p.store("a throwaway memory for the mirror test")
        c = self.p._conn
        self.assertEqual(
            c.execute("SELECT COUNT(*) FROM memory_vec").fetchone()[0], 1)
        row = c.execute("SELECT content_hash FROM memory").fetchone()
        self.p.delete_by_hashes([bytes(row[0])])
        self.assertEqual(
            c.execute("SELECT COUNT(*) FROM memory_vec").fetchone()[0], 0)

    def test_list_memories_returns_ids(self):
        self.p.store("first memory")
        self.p.store("second memory")
        rows = self.p.list_memories(limit=10)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(m.metadata.get("_hash") for m in rows))

    def test_list_paging(self):
        for i in range(5):
            self.p.store(f"memory number {i}")
        first = self.p.list_memories(limit=2)
        second = self.p.list_memories(limit=2, offset=2)
        ids = {m.metadata["_hash"] for m in first}
        self.assertTrue(ids.isdisjoint({m.metadata["_hash"] for m in second}))


if __name__ == "__main__":
    unittest.main()
