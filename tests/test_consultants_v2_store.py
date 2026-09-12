"""Unit tests for the M8 long-term-memory BaseStore adapter
(:mod:`consultants.engine.store`).

The tests split cleanly into two layers:

1. **Helpers** (Namespaces, recall_research, record_research,
   recall_for_follow_up, format_findings_block, make_consultants_store
   short-circuits) — these are pure-Python and exercise the safety
   net (None store, empty query, missing langgraph). Run in BOTH
   envs.
2. **ProviderBackedStore** — requires LangGraph for ``BaseStore`` /
   ``Item`` / ``SearchItem``. The test class skips when langgraph
   isn't available; on the consultants env it runs the full op
   surface against a fake provider.

The fake provider mirrors the duck-typed ``StoreProvider`` protocol
without touching Postgres / sqlite — same shape as
``PgvectorProvider`` / ``SqliteVecProvider``.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from types import SimpleNamespace


try:
    from langgraph.store.base import BaseStore  # noqa: F401
    HAVE_LANGGRAPH = True
except ImportError:
    HAVE_LANGGRAPH = False


from consultants.engine.store import (
    Namespaces,
    format_findings_block,
    make_consultants_store,
    recall_for_follow_up,
    recall_research,
    record_research,
)


# ============================================================== #
# Fakes used by helper + adapter tests.
# ============================================================== #


@dataclass
class _FakeMemory:
    """Shape matches ``claude_hooks.providers.base.Memory`` — text +
    metadata is the only contract the store cares about."""

    text: str
    metadata: dict


class _FakeProvider:
    """Duck-typed ``StoreProvider`` — keeps every store() call in
    a list so tests can assert on what was persisted, and serves
    them back from recall_hybrid in insertion order (no real
    semantic ranking; the marker filter is what we're testing).

    #216: also models per-content-hash deletion. The ProviderBackedStore
    uses ``delete_by_hashes`` on overwrite to keep one provider row
    per ``(namespace, key)`` pair; this fake mirrors that surface so
    the tests see the real post-overwrite row count.
    """

    def __init__(self):
        self.stored: list[tuple[str, dict]] = []
        self.recall_calls: list[tuple[str, int]] = []
        self.delete_calls: list[list[bytes]] = []
        self.fail_store = False
        self.fail_recall = False

    def store(self, content: str, metadata: dict) -> None:
        if self.fail_store:
            raise RuntimeError("provider.store boom")
        self.stored.append((content, dict(metadata)))

    def recall_hybrid(self, query: str, k: int = 5) -> list[_FakeMemory]:
        self.recall_calls.append((query, k))
        if self.fail_recall:
            raise RuntimeError("provider.recall boom")
        # Return every stored item, capped at k, in insertion order
        # so tests can predict ordering deterministically.
        out = []
        for text, meta in self.stored[:k]:
            out.append(_FakeMemory(text=text, metadata=dict(meta)))
        return out

    def delete_by_hashes(self, hashes: list[bytes]) -> int:
        """#216: remove rows whose content hashes to one of the
        given bytes. Mirrors the provider contract used by
        :class:`ProviderBackedStore._do_put` on overwrite."""
        from claude_hooks.providers._content_hash import content_hash
        self.delete_calls.append(list(hashes))
        wanted = set(hashes)
        kept: list[tuple[str, dict]] = []
        removed = 0
        for text, meta in self.stored:
            if content_hash(text) in wanted:
                removed += 1
                continue
            kept.append((text, meta))
        self.stored = kept
        return removed


# ============================================================== #
# Namespaces helpers (no langgraph dep — always runs)
# ============================================================== #


class TestNamespaces(unittest.TestCase):

    def test_research_tuple(self):
        self.assertEqual(
            Namespaces.research("csl-2026-05-16-abc"),
            ("csl-2026-05-16-abc", "research"),
        )

    def test_tool_results_tuple(self):
        self.assertEqual(
            Namespaces.tool_results("csl-x"),
            ("csl-x", "tool_results"),
        )

    def test_project_tuple(self):
        self.assertEqual(
            Namespaces.project("claude-hooks"),
            ("project", "claude-hooks"),
        )

    def test_user_tuple(self):
        self.assertEqual(
            Namespaces.user("alice"),
            ("user", "alice"),
        )

    def test_coerces_non_string_sid(self):
        # Defensive: an int sid (would never happen in practice
        # but is cheap insurance) becomes a string segment.
        self.assertEqual(
            Namespaces.research(42),  # type: ignore[arg-type]
            ("42", "research"),
        )


# ============================================================== #
# Helpers that no-op on a None store
# ============================================================== #


class TestRecallRecordNullStore(unittest.TestCase):

    def test_recall_with_none_store_returns_empty(self):
        self.assertEqual(recall_research(None, "sid", "anything"), [])

    def test_recall_with_empty_query_returns_empty(self):
        # Even with a "real" store, an empty query is a no-op.
        # The fake here would raise if we hit it.
        class _Boom:
            def search(self, *a, **kw):
                raise AssertionError("should not have been called")
        self.assertEqual(recall_research(_Boom(), "sid", ""), [])
        self.assertEqual(recall_research(_Boom(), "sid", "   "), [])

    def test_record_with_none_store_returns_none(self):
        self.assertIsNone(record_research(
            None, "sid", lane_idx=0, plan_item="x", finding="y",
        ))

    def test_record_with_empty_finding_returns_none(self):
        class _Boom:
            def put(self, *a, **kw):
                raise AssertionError("should not have been called")
        self.assertIsNone(record_research(
            _Boom(), "sid", lane_idx=0, plan_item="x", finding="",
        ))
        self.assertIsNone(record_research(
            _Boom(), "sid", lane_idx=0, plan_item="x", finding="   ",
        ))

    def test_recall_for_follow_up_delegates(self):
        # Just verifies the function exists and is a thin wrapper —
        # the heavy lifting lives in recall_research which has its
        # own tests.
        self.assertEqual(
            recall_for_follow_up(None, "parent", "Q", limit=20),
            [],
        )


# ============================================================== #
# format_findings_block
# ============================================================== #


class TestFormatFindingsBlock(unittest.TestCase):

    def _item(self, text, lane=0, plan_item="step"):
        return SimpleNamespace(
            value={"text": text, "plan_item": plan_item, "lane_idx": lane},
        )

    def test_empty_returns_empty_string(self):
        self.assertEqual(format_findings_block([]), "")

    def test_skips_empty_text(self):
        items = [SimpleNamespace(value={"text": "", "lane_idx": 0})]
        self.assertEqual(format_findings_block(items), "")

    def test_deduplicates_identical_text(self):
        items = [self._item("dup"), self._item("dup", lane=1)]
        block = format_findings_block(items)
        # Only one '- **finding**' bullet, even with two items.
        self.assertEqual(block.count("- **finding**"), 1)

    def test_truncates_long_text(self):
        long_text = "x" * 5000
        items = [self._item(long_text)]
        block = format_findings_block(items, max_chars_per_item=100)
        # Truncated body plus the ellipsis marker.
        self.assertIn("[…]", block)
        # Header line + one finding line is fine; no crash on big text.
        self.assertLess(len(block), 1500)

    def test_respects_max_items(self):
        items = [self._item(f"t{i}", lane=i) for i in range(20)]
        block = format_findings_block(items, max_items=3)
        self.assertEqual(block.count("- **finding**"), 3)

    def test_renders_lane_and_plan_metadata(self):
        items = [self._item("hit", lane=2, plan_item="audit auth")]
        block = format_findings_block(items)
        self.assertIn("lane 2", block)
        self.assertIn("plan: audit auth", block)
        self.assertIn("hit", block)


# ============================================================== #
# Factory short-circuits (also runs without langgraph — they just
# return None either way)
# ============================================================== #


class TestMakeConsultantsStoreShortCircuits(unittest.TestCase):

    def test_returns_none_when_no_cfg_store(self):
        cfg = SimpleNamespace()  # no .store attr
        self.assertIsNone(make_consultants_store(cfg, sid="x"))

    def test_returns_none_when_disabled(self):
        cfg = SimpleNamespace(store=SimpleNamespace(enabled=False))
        self.assertIsNone(make_consultants_store(cfg, sid="x"))

    def test_returns_none_below_effort_gate(self):
        cfg = SimpleNamespace(store=SimpleNamespace(
            enabled=True, backend="memory",
            enable_at_efforts=("high", "max", "xhigh"),
        ))
        # low/medium are below the gate -> None
        self.assertIsNone(make_consultants_store(
            cfg, sid="x", effort="low",
        ))
        self.assertIsNone(make_consultants_store(
            cfg, sid="x", effort="medium",
        ))

    @unittest.skipUnless(HAVE_LANGGRAPH, "langgraph not installed")
    def test_returns_inmemorystore_when_enabled_at_effort(self):
        from langgraph.store.memory import InMemoryStore
        cfg = SimpleNamespace(store=SimpleNamespace(
            enabled=True, backend="memory",
            enable_at_efforts=("high", "xhigh"),
        ))
        store = make_consultants_store(
            cfg, sid="x", effort="xhigh",
        )
        self.assertIsInstance(store, InMemoryStore)

    @unittest.skipUnless(HAVE_LANGGRAPH, "langgraph not installed")
    def test_unknown_backend_returns_none(self):
        cfg = SimpleNamespace(store=SimpleNamespace(
            enabled=True, backend="weaviate",
        ))
        # effort=None -> bypass gate
        self.assertIsNone(make_consultants_store(cfg, sid="x"))

    @unittest.skipUnless(HAVE_LANGGRAPH, "langgraph not installed")
    def test_provider_loader_used_when_supplied(self):
        cfg = SimpleNamespace(store=SimpleNamespace(
            enabled=True, backend="pgvector",
        ))
        sentinel = _FakeProvider()
        store = make_consultants_store(
            cfg, sid="x",
            provider_loader=lambda _cfg: sentinel,
        )
        self.assertIsNotNone(store)
        # ProviderBackedStore wraps the sentinel.
        self.assertIs(store._provider, sentinel)

    @unittest.skipUnless(HAVE_LANGGRAPH, "langgraph not installed")
    def test_provider_loader_returning_none_yields_none(self):
        cfg = SimpleNamespace(store=SimpleNamespace(
            enabled=True, backend="pgvector",
        ))
        self.assertIsNone(make_consultants_store(
            cfg, sid="x",
            provider_loader=lambda _cfg: None,
        ))


# ============================================================== #
# ProviderBackedStore — full op surface (langgraph required)
# ============================================================== #


@unittest.skipUnless(HAVE_LANGGRAPH, "langgraph not installed")
class TestProviderBackedStoreOps(unittest.TestCase):

    def setUp(self):
        from consultants.engine.store import ProviderBackedStore
        self.provider = _FakeProvider()
        self.store = ProviderBackedStore(self.provider)

    # ---- put + get ---- #

    def test_put_then_get_returns_item(self):
        ns = Namespaces.research("csl-1")
        self.store.put(ns, "k1", {"text": "hello"})
        item = self.store.get(ns, "k1")
        self.assertIsNotNone(item)
        self.assertEqual(item.value, {"text": "hello"})
        self.assertEqual(item.key, "k1")
        self.assertEqual(item.namespace, ns)

    def test_put_persists_to_provider_with_marker(self):
        ns = Namespaces.research("csl-1")
        self.store.put(ns, "k1", {"text": "indexable"}, index=["text"])
        self.assertEqual(len(self.provider.stored), 1)
        content, meta = self.provider.stored[0]
        self.assertEqual(content, "indexable")
        self.assertTrue(meta["_consultants_store"])
        self.assertEqual(meta["namespace"], list(ns))
        self.assertEqual(meta["key"], "k1")

    def test_put_index_false_skips_provider_write(self):
        ns = Namespaces.research("csl-1")
        self.store.put(ns, "k1", {"text": "x"}, index=False)
        self.assertEqual(self.provider.stored, [])
        # But the in-process index DOES contain it (still
        # gettable / deletable).
        self.assertIsNotNone(self.store.get(ns, "k1"))

    def test_put_with_default_index_writes_whole_value(self):
        ns = Namespaces.research("csl-1")
        self.store.put(ns, "k1", {"a": 1, "b": [2, 3]})
        # Default index path = "$" -> whole value JSON.
        content, _meta = self.provider.stored[0]
        self.assertIn('"a"', content)
        self.assertIn('"b"', content)

    def test_get_unknown_returns_none(self):
        self.assertIsNone(
            self.store.get(Namespaces.research("csl-1"), "missing"),
        )

    def test_put_none_deletes_existing(self):
        ns = Namespaces.research("csl-1")
        self.store.put(ns, "k1", {"text": "live"})
        self.assertIsNotNone(self.store.get(ns, "k1"))
        # PutOp(value=None) deletes.
        self.store.put(ns, "k1", None)
        self.assertIsNone(self.store.get(ns, "k1"))

    def test_overwrite_preserves_created_at(self):
        ns = Namespaces.research("csl-1")
        self.store.put(ns, "k1", {"text": "v1"})
        original_created = self.store.get(ns, "k1").created_at
        # Second put with a tiny pause.
        self.store.put(ns, "k1", {"text": "v2"})
        item2 = self.store.get(ns, "k1")
        self.assertEqual(item2.created_at, original_created)
        # updated_at is >= created_at (monotonic).
        self.assertGreaterEqual(item2.updated_at, item2.created_at)

    # ---- delete ---- #

    def test_delete_removes_from_index(self):
        ns = Namespaces.research("csl-1")
        self.store.put(ns, "k1", {"text": "x"})
        self.store.delete(ns, "k1")
        self.assertIsNone(self.store.get(ns, "k1"))

    # ---- search ---- #

    def test_search_filters_by_namespace_prefix(self):
        # Two namespaces; query hits both, filter narrows to one.
        ns_a = Namespaces.research("csl-A")
        ns_b = Namespaces.research("csl-B")
        self.store.put(ns_a, "k1", {"text": "alpha report"})
        self.store.put(ns_b, "k2", {"text": "alpha report"})  # same text
        results = self.store.search(ns_a, query="alpha")
        self.assertGreaterEqual(len(results), 1)
        for r in results:
            self.assertEqual(r.namespace, ns_a)

    def test_search_returns_search_items_with_value(self):
        from langgraph.store.base import SearchItem
        ns = Namespaces.research("csl-1")
        self.store.put(ns, "k1", {
            "text": "auth flow",
            "plan_item": "audit auth",
            "lane_idx": 0,
        })
        results = self.store.search(ns, query="auth")
        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertIsInstance(r, SearchItem)
        self.assertEqual(r.namespace, ns)
        self.assertEqual(r.key, "k1")
        self.assertEqual(r.value["text"], "auth flow")
        self.assertEqual(r.value["plan_item"], "audit auth")

    def test_search_ignores_rows_without_marker(self):
        # Simulate a row written by the general claude-hooks recall
        # pipeline that shares the same pgvector instance — no
        # marker means we must ignore it.
        ns = Namespaces.research("csl-1")
        self.store.put(ns, "k1", {"text": "marked"})
        # Inject a fake hit without the marker; it must be filtered
        # out even though it shares the namespace fingerprint.
        self.provider.stored.append((
            "unmarked", {
                "namespace": list(ns), "key": "kx",
                "value": {"text": "unmarked"},
            },
        ))
        results = self.store.search(ns, query="marked OR unmarked")
        keys = {r.key for r in results}
        self.assertIn("k1", keys)
        self.assertNotIn("kx", keys)

    def test_search_no_query_falls_back_to_index_scan(self):
        ns = Namespaces.research("csl-1")
        self.store.put(ns, "k1", {"text": "first"})
        self.store.put(ns, "k2", {"text": "second"})
        # No query -> in-process scan; provider.recall not called.
        before = list(self.provider.recall_calls)
        results = self.store.search(ns, query=None, limit=10)
        self.assertEqual(self.provider.recall_calls, before)
        keys = {r.key for r in results}
        self.assertEqual(keys, {"k1", "k2"})

    def test_search_recall_failure_returns_empty(self):
        ns = Namespaces.research("csl-1")
        self.store.put(ns, "k1", {"text": "x"})
        self.provider.fail_recall = True
        # Provider raises -> we return [], not propagate the error.
        self.assertEqual(self.store.search(ns, query="x"), [])

    def test_search_dedups_repeated_hits(self):
        ns = Namespaces.research("csl-1")
        self.store.put(ns, "k1", {"text": "same"})
        # Inject a duplicate provider row mimicking the same (ns,
        # key) — defensive against provider-side dup writes.
        _content, meta = self.provider.stored[0]
        self.provider.stored.append(("same", dict(meta)))
        results = self.store.search(ns, query="same")
        self.assertEqual(len(results), 1)

    # ---- list_namespaces ---- #

    def test_list_namespaces_returns_known_namespaces(self):
        self.store.put(Namespaces.research("a"), "k", {"text": "x"})
        self.store.put(Namespaces.research("b"), "k", {"text": "x"})
        self.store.put(Namespaces.tool_results("a"), "k", {"text": "x"})
        ns_list = self.store.list_namespaces()
        # Three distinct namespaces.
        self.assertEqual(len(ns_list), 3)
        self.assertIn(("a", "research"), ns_list)
        self.assertIn(("b", "research"), ns_list)
        self.assertIn(("a", "tool_results"), ns_list)

    def test_list_namespaces_max_depth_truncates_and_dedups(self):
        self.store.put(Namespaces.research("a"), "k", {"text": "x"})
        self.store.put(Namespaces.tool_results("a"), "k", {"text": "x"})
        # max_depth=1 collapses both ('a', 'research') and
        # ('a', 'tool_results') into ('a',).
        ns_list = self.store.list_namespaces(max_depth=1)
        self.assertEqual(ns_list, [("a",)])

    # ---- recall_research / record_research integration ---- #

    def test_record_research_then_recall(self):
        record_research(
            self.store, "csl-X",
            lane_idx=2, plan_item="audit auth",
            finding="The login flow has a CSRF gap on /logout.",
        )
        items = recall_research(self.store, "csl-X", query="CSRF")
        self.assertEqual(len(items), 1)
        self.assertIn("CSRF", items[0].value["text"])
        self.assertEqual(items[0].value["plan_item"], "audit auth")
        self.assertEqual(items[0].value["lane_idx"], 2)

    def test_record_research_key_deterministic(self):
        k1 = record_research(
            self.store, "csl-X",
            lane_idx=0, plan_item="p", finding="same finding",
        )
        k2 = record_research(
            self.store, "csl-X",
            lane_idx=0, plan_item="p", finding="same finding",
        )
        # Same content + same lane -> same key (idempotent overwrite).
        self.assertEqual(k1, k2)

    def test_record_research_key_is_stable_per_lane(self):
        """#216: same lane = same key regardless of content. Each
        successive round of the SAME researcher lane overwrites the
        prior round's in-process entry instead of fanning out."""
        k1 = record_research(
            self.store, "csl-X",
            lane_idx=0, plan_item="p", finding="round 1 finding",
        )
        k2 = record_research(
            self.store, "csl-X",
            lane_idx=0, plan_item="p", finding="round 2 finding",
        )
        # Same lane → same key, regardless of changed content.
        self.assertEqual(k1, k2)
        # Key follows the documented L{lane_idx} shape.
        self.assertEqual(k1, "L0")

    def test_record_research_keys_differ_across_lanes(self):
        """#216: different lanes get different keys so cross-lane
        findings don't clobber each other in the in-process index."""
        k0 = record_research(
            self.store, "csl-X",
            lane_idx=0, plan_item="p", finding="lane 0",
        )
        k1 = record_research(
            self.store, "csl-X",
            lane_idx=1, plan_item="p", finding="lane 1",
        )
        self.assertNotEqual(k0, k1)
        self.assertEqual(k0, "L0")
        self.assertEqual(k1, "L1")

    def test_record_research_missing_lane_idx_uses_question_mark(self):
        """#216: ``lane_idx=None`` falls back to ``L?`` (single-
        researcher non-fanout path). All single-researcher rounds
        of the same session land on the same key."""
        k = record_research(
            self.store, "csl-X",
            lane_idx=None, plan_item="p", finding="text",
        )
        self.assertEqual(k, "L?")

    def test_per_lane_overwrite_deletes_prior_provider_row(self):
        """#216: writing the SAME (namespace, key) pair with different
        content deletes the prior provider row before inserting the
        new one. After 3 rounds for one lane, the provider has exactly
        1 row — not 3."""
        ns = Namespaces.research("csl-X")
        # Round 1.
        self.store.put(ns, "L0", {"text": "round 1"}, index=["text"])
        self.assertEqual(len(self.provider.stored), 1)
        self.assertEqual(self.provider.stored[0][0], "round 1")
        # Round 2 — different content, same key.
        self.store.put(ns, "L0", {"text": "round 2"}, index=["text"])
        self.assertEqual(len(self.provider.stored), 1)
        self.assertEqual(self.provider.stored[0][0], "round 2")
        # Round 3 — and again.
        self.store.put(ns, "L0", {"text": "round 3"}, index=["text"])
        self.assertEqual(len(self.provider.stored), 1)
        self.assertEqual(self.provider.stored[0][0], "round 3")
        # Two delete calls fired (round 2 deleted round 1, round 3
        # deleted round 2) — that's the embedder load we're saving.
        self.assertEqual(len(self.provider.delete_calls), 2)

    def test_per_lane_overwrite_skips_delete_when_content_unchanged(self):
        """#216: re-storing the SAME content under the same key is
        a true no-op — no delete call (the prior row's content_hash
        equals the new one, so the provider's existing
        ``ON CONFLICT(content_hash) DO NOTHING`` upsert handles it)."""
        ns = Namespaces.research("csl-X")
        self.store.put(ns, "L0", {"text": "same"}, index=["text"])
        self.store.put(ns, "L0", {"text": "same"}, index=["text"])
        # Provider got two store() calls but its dedup leaves 1 row;
        # importantly NO delete fired because content didn't change.
        self.assertEqual(len(self.provider.delete_calls), 0)

    def test_per_lane_overwrite_with_record_research_smoke(self):
        """#216 end-to-end via record_research: a lane that writes 4
        round findings ends with 1 row in the provider, simulating
        the tool_executor PLAN→REPORT round loop."""
        for round_idx, txt in enumerate(
            ["r1", "r2", "r3", "r4"], start=1,
        ):
            record_research(
                self.store, "csl-X",
                lane_idx=2, plan_item="audit",
                finding=f"lane=2 round={round_idx}: {txt}",
            )
        # One row — the latest content.
        self.assertEqual(len(self.provider.stored), 1)
        latest_text, _ = self.provider.stored[0]
        self.assertIn("round=4", latest_text)

    def test_multi_lane_writes_keep_one_row_per_lane(self):
        """#216 cohort: 3 lanes × 4 rounds each end with 3 provider
        rows (one per lane), not 12. Cross-lane writes don't clobber
        each other."""
        for lane in range(3):
            for round_idx in range(1, 5):
                record_research(
                    self.store, "csl-X",
                    lane_idx=lane, plan_item="audit",
                    finding=f"L{lane} round {round_idx}",
                )
        # 3 rows, one per lane, each holding its lane's latest text.
        self.assertEqual(len(self.provider.stored), 3)
        latest_by_lane = {
            meta.get("key"): text
            for text, meta in self.provider.stored
        }
        for lane in range(3):
            self.assertIn(f"L{lane} round 4", latest_by_lane[f"L{lane}"])

    def test_provider_store_failure_propagates_to_caller(self):
        # Contract (post-#212): durable-write failures propagate so
        # callers that depend on persistence (M14 reaper →
        # write_distilled_summary) can react and skip the follow-up
        # delete step. The in-process index update still lands —
        # only the durable provider missed it — so the same-process
        # get() call below still finds the item. Callers that want
        # silent best-effort (researcher per-turn puts via
        # record_research) wrap store.put themselves; see
        # record_research at the bottom of store.py for the pattern.
        #
        # Pre-#212 this test asserted the *opposite* (silent swallow);
        # the consultation csl-2026-05-18-1554-c8dc surfaced this
        # contract gap against the M14 critical invariant.
        self.provider.fail_store = True
        ns = Namespaces.research("csl-1")
        with self.assertRaises(RuntimeError):
            self.store.put(ns, "k1", {"text": "x"})
        # In-process index still has it (we deliberately don't roll
        # back the in-memory update on durable-write failure).
        self.assertIsNotNone(self.store.get(ns, "k1"))


if __name__ == "__main__":
    unittest.main()
