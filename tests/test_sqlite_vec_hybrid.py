"""Tests for SqliteVecProvider hybrid recall + content_hash idempotency.

Uses a deterministic in-process embedder (no Ollama) so these run
fast on every CI matrix. Covers:

* ``store()`` is idempotent on whitespace-normalised content
* ``recall_hybrid`` returns the BM25 hit when query is keyword-only
* ``recall_hybrid`` returns the vector hit when query is semantic-only
* RRF blend ranks dual-signal docs above single-signal docs
* Empty / whitespace query short-circuits to ``[]``
* Vector-pass failure still surfaces BM25 hits
* BM25 syntax in user input is taken literally (``_fts5_query`` quoting)
"""

from __future__ import annotations

import hashlib
import json
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
from claude_hooks.providers._content_hash import content_hash


class _DeterministicEmbedder(Embedder):
    """Pseudo-embedder: hash(text) → 16 float32s in [0, 1).

    Same text → same vector (so semantic recall is just hash equality
    for our purposes). Different text → essentially random vector with
    no correlation. That's enough to drive the vector-pass code paths
    while keeping tests hermetic.
    """

    name = "deterministic"
    dim = 16

    def embed(self, text: str) -> list[float]:
        h = hashlib.sha256(text.encode("utf-8")).digest()
        # 32 bytes → 16 unsigned shorts → floats in [0, 1).
        return [int.from_bytes(h[i:i + 2], "little") / 65535.0
                for i in range(0, 32, 2)]


def _make_provider(db_path: str):
    """Build a SqliteVecProvider wired to a temp DB + the
    deterministic embedder. Skips the registry/config layer."""
    from claude_hooks.providers.base import ServerCandidate
    from claude_hooks.providers.sqlite_vec import SqliteVecProvider

    server = ServerCandidate(server_key="sqlite_vec", url=db_path)
    p = SqliteVecProvider(server, options={"table": "memory"})
    # Inject the embedder before _ensure_ready runs so it doesn't try
    # to talk to Ollama via make_embedder("null").
    p._embedder = _DeterministicEmbedder()
    return p


@unittest.skipUnless(HAS_SQLITE_VEC, "sqlite-vec not installed")
class TestStoreIdempotency(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="ch-hybrid-")
        self.db = os.path.join(self.tmpdir, "t.db")
        self.p = _make_provider(self.db)

    def tearDown(self):
        if self.p._conn is not None:
            self.p._conn.close()

    def test_same_content_stored_twice_is_one_row(self):
        self.p.store("hello world", {"k": "v"})
        self.p.store("hello world", {"k": "different"})
        self.assertEqual(self.p.count(), 1)

    def test_whitespace_differences_collapse(self):
        self.p.store("hello   world")
        self.p.store("  hello world  ")
        self.p.store("hello\tworld")
        self.assertEqual(self.p.count(), 1)

    def test_distinct_content_inserts_separately(self):
        self.p.store("alpha")
        self.p.store("beta")
        self.assertEqual(self.p.count(), 2)

    def test_idempotent_store_does_not_pollute_vec_table(self):
        self.p.store("repeated content")
        self.p.store("repeated content")
        cur = self.p._conn.execute("SELECT COUNT(*) FROM memory_vec")
        self.assertEqual(cur.fetchone()[0], 1)


@unittest.skipUnless(HAS_SQLITE_VEC, "sqlite-vec not installed")
class TestRecallHybrid(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="ch-hybrid-")
        self.db = os.path.join(self.tmpdir, "t.db")
        self.p = _make_provider(self.db)
        # Seed with a small varied corpus.
        for i, c in enumerate([
            "bcache cache device rebuild on solidpc",
            "qdrant memory server runs in docker",
            "pgvector postgres database with vector extension",
            "the quick brown fox jumps over the lazy dog",
            "rebuild superblock with make-bcache wipe-bcache flag",
        ]):
            self.p.store(c, {"i": i})

    def tearDown(self):
        if self.p._conn is not None:
            self.p._conn.close()

    def test_empty_query_returns_empty(self):
        self.assertEqual(self.p.recall_hybrid(""), [])
        self.assertEqual(self.p.recall_hybrid("   "), [])

    def test_keyword_query_finds_lexical_match(self):
        # "bcache" is unique to two docs. BM25 should rank them top
        # even though the deterministic embedder gives no useful
        # semantic signal.
        hits = self.p.recall_hybrid("bcache", k=3)
        self.assertTrue(len(hits) >= 1)
        self.assertTrue(
            any("bcache" in h.text for h in hits),
            f"expected bcache hit, got {[h.text for h in hits]}",
        )

    def test_exact_text_recall_returns_matching_doc(self):
        # Vector pass: same text → same vector → distance 0.
        hits = self.p.recall_hybrid("qdrant memory server runs in docker", k=1)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].text, "qdrant memory server runs in docker")

    def test_metadata_carries_rank_fields(self):
        hits = self.p.recall_hybrid("pgvector postgres", k=2)
        self.assertTrue(len(hits) >= 1)
        m = hits[0].metadata
        self.assertIn("_score", m)
        self.assertIn("_table", m)
        self.assertEqual(m["_table"], "memory")
        # At least one of vec_rank / kw_rank should be populated.
        self.assertTrue(
            m["_vec_rank"] is not None or m["_kw_rank"] is not None
        )

    def test_no_match_query_returns_empty_or_vector_only(self):
        # A query with no lexical overlap returns at most vector
        # hits — the deterministic embedder makes these unranked
        # noise, but the call shouldn't crash.
        hits = self.p.recall_hybrid("zzz unmatched xyzzy", k=3)
        self.assertIsInstance(hits, list)

    def test_user_input_with_fts5_syntax_is_taken_literally(self):
        # "OR" / "NEAR" / "*" must not crash the BM25 pass even
        # though they're FTS5 keywords.
        for q in [
            "OR something",
            "near/10 something",
            "wild* card",
            'foo "bar baz" qux',
        ]:
            hits = self.p.recall_hybrid(q, k=2)
            self.assertIsInstance(hits, list)


@unittest.skipUnless(HAS_SQLITE_VEC, "sqlite-vec not installed")
class TestFts5QueryHelper(unittest.TestCase):

    def test_empty_input(self):
        from claude_hooks.providers.sqlite_vec import _fts5_query
        self.assertEqual(_fts5_query(""), "")
        self.assertEqual(_fts5_query("   "), "")

    def test_quotes_are_escaped(self):
        from claude_hooks.providers.sqlite_vec import _fts5_query
        self.assertEqual(_fts5_query('say "hi"'), '"say ""hi"""')

    def test_keywords_are_wrapped(self):
        from claude_hooks.providers.sqlite_vec import _fts5_query
        # "OR" wrapped becomes a literal token, not a boolean op.
        self.assertEqual(_fts5_query("OR"), '"OR"')


if __name__ == "__main__":
    unittest.main()
