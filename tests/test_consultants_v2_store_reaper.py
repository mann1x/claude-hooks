"""M14 — :class:`StoreReaperThread` unit tests.

Covers the daemon-side sweep:

- start / stop lifecycle is idempotent and shutdown is responsive
  (≤ 0.5 s slice deadline).
- ``sweep_once`` happy path: research above threshold → distill →
  write summary → delete originals.
- Tool-results bypass: no distillation, just delete.
- Cost gate: research below ``min_entries_per_distillation`` →
  delete without distillation, no LLM call.
- Critical invariant: distillation failure keeps originals in
  place. The reaper does NOT call ``delete_by_hashes`` for that
  group; it does for every other group.
- Per-group failure isolation: one bad group doesn't poison the
  rest of the sweep.

The provider, distiller, and store are all duck-typed test
doubles so this test runs in either env (no LangGraph required).
"""
from __future__ import annotations

import threading
import time
import unittest
from dataclasses import dataclass, field
from typing import Any, Optional

from consultants.config import StoreConfig, StoreDistillationConfig
from consultants.engine.distillation import DistillationFailed
from consultants.engine.store_reaper import (
    KIND_RESEARCH,
    KIND_TOOL_RESULTS,
    KIND_UNKNOWN,
    StoreReaperThread,
    _group_by_sid_and_kind,
)


# ============================================================== #
# Fakes
# ============================================================== #


@dataclass
class _FakeRow:
    content: str
    metadata: dict = field(default_factory=dict)
    content_hash: bytes = b""
    expires_at: str = ""


def _row(text: str, *, ns: tuple[str, str], **meta: Any) -> _FakeRow:
    md = {"namespace": list(ns)}
    md.update(meta)
    return _FakeRow(
        content=text,
        metadata=md,
        content_hash=text.encode(),
        expires_at="2026-05-01T00:00:00+00:00",
    )


class _FakeProvider:
    """Records expire_before / delete_by_hashes calls; returns a
    queued list of rows from each ``expire_before`` invocation."""

    def __init__(self, expiring_batches: Optional[list[list[_FakeRow]]] = None) -> None:
        self._batches = list(expiring_batches or [])
        self.expire_calls: list[dict] = []
        self.delete_calls: list[list[bytes]] = []
        # Optional failure modes for negative tests.
        self.expire_raises: Optional[Exception] = None
        self.delete_raises_for: set[bytes] = set()

    def expire_before(self, *, before_iso: str, limit: int = 1000) -> list[_FakeRow]:
        self.expire_calls.append({"before_iso": before_iso, "limit": limit})
        if self.expire_raises is not None:
            raise self.expire_raises
        if not self._batches:
            return []
        return self._batches.pop(0)

    def delete_by_hashes(self, hashes: list[bytes]) -> int:
        self.delete_calls.append(list(hashes))
        if any(h in self.delete_raises_for for h in hashes):
            raise RuntimeError("simulated delete failure")
        return len(hashes)


class _FakeDistiller:
    """Stand-in for :class:`Distiller`. Records calls + returns a
    canned summary; can be configured to raise per-sid."""

    def __init__(
        self,
        summary: str = "DISTILLED.",
        raise_for_sids: Optional[set[str]] = None,
        model: str = "fake-distill-model",
    ) -> None:
        self.summary = summary
        self.raise_for = raise_for_sids or set()
        self.calls: list[dict] = []
        # The reaper reads .cfg.model for provenance, so expose a
        # tiny config stand-in.
        self.cfg = StoreDistillationConfig(
            enabled=True, model=model, fallback_models=(),
        )

    def distill_session(
        self,
        sid: str,
        rows: Any,
        *,
        cwd_hint: Optional[str] = None,
        question_hint: Optional[str] = None,
    ) -> str:
        self.calls.append({
            "sid": sid,
            "rows": list(rows),
            "cwd_hint": cwd_hint,
            "question_hint": question_hint,
        })
        if sid in self.raise_for:
            raise DistillationFailed(f"forced failure for sid={sid}")
        return self.summary


class _FakeStore:
    """Records ``put`` calls — the write target of
    :func:`write_distilled_summary`. Optional ``fail_put`` lets
    tests simulate the M14 silent-durable-write failure mode that
    csl-2026-05-18-1554-c8dc surfaced (#212)."""

    def __init__(self) -> None:
        self.puts: list[tuple[tuple[str, ...], str, dict, dict]] = []
        # When set to an exception instance/factory, every put()
        # raises it. Mirrors the post-#212 contract where
        # ProviderBackedStore._do_put propagates durable-write
        # failures to its caller.
        self.fail_put: Optional[BaseException] = None

    def put(self, ns: tuple[str, ...], key: str, value: dict, **kwargs: Any) -> None:
        if self.fail_put is not None:
            raise self.fail_put
        self.puts.append((ns, key, value, kwargs))


def _store_cfg(
    *,
    distill_enabled: bool = True,
    min_entries: int = 3,
    interval: float = 3600.0,
) -> StoreConfig:
    cfg = StoreConfig()
    cfg.distillation = StoreDistillationConfig(
        enabled=distill_enabled,
        model="gemma4:31b-cloud",
        fallback_models=("glm-5.1:cloud",),
        sweep_interval_seconds=interval,
        min_entries_per_distillation=min_entries,
        max_session_entries=50,
    )
    return cfg


# ============================================================== #
# Grouping
# ============================================================== #


class TestGroupBySidAndKind(unittest.TestCase):

    def test_groups_research_by_sid(self) -> None:
        rows = [
            _row("a", ns=("csl-1", "research")),
            _row("b", ns=("csl-1", "research")),
            _row("c", ns=("csl-2", "research")),
        ]
        g = _group_by_sid_and_kind(rows)
        self.assertEqual(set(g.keys()), {("csl-1", KIND_RESEARCH), ("csl-2", KIND_RESEARCH)})
        self.assertEqual(len(g[("csl-1", KIND_RESEARCH)]), 2)
        self.assertEqual(len(g[("csl-2", KIND_RESEARCH)]), 1)

    def test_groups_tool_results_by_sid(self) -> None:
        rows = [
            _row("t1", ns=("csl-9", "tool_results")),
            _row("t2", ns=("csl-9", "tool_results")),
        ]
        g = _group_by_sid_and_kind(rows)
        self.assertEqual(set(g.keys()), {("csl-9", KIND_TOOL_RESULTS)})

    def test_separates_kinds_within_same_sid(self) -> None:
        rows = [
            _row("r", ns=("csl-7", "research")),
            _row("t", ns=("csl-7", "tool_results")),
        ]
        g = _group_by_sid_and_kind(rows)
        self.assertEqual(
            set(g.keys()),
            {("csl-7", KIND_RESEARCH), ("csl-7", KIND_TOOL_RESULTS)},
        )

    def test_unknown_namespace_buckets_unknown(self) -> None:
        """Bad / missing metadata still gets a bucket so the row
        gets deleted by the sweep."""
        bad = _FakeRow(content="x", metadata={}, content_hash=b"x")
        weird = _FakeRow(
            content="y", metadata={"namespace": ["onlyone"]}, content_hash=b"y",
        )
        g = _group_by_sid_and_kind([bad, weird])
        self.assertTrue(any(k[1] == KIND_UNKNOWN for k in g.keys()))


# ============================================================== #
# Lifecycle
# ============================================================== #


class TestReaperLifecycle(unittest.TestCase):

    def test_start_and_stop_cleanly(self) -> None:
        prov = _FakeProvider()
        r = StoreReaperThread(
            store_cfg=_store_cfg(interval=3600.0),
            provider=prov, distiller=None, store=None,
        )
        r.start()
        self.assertTrue(r.is_alive)
        r.stop(timeout=2.0)
        self.assertFalse(r.is_alive)

    def test_start_is_idempotent(self) -> None:
        """Two consecutive starts → one live thread."""
        prov = _FakeProvider()
        r = StoreReaperThread(
            store_cfg=_store_cfg(interval=3600.0),
            provider=prov, distiller=None, store=None,
        )
        r.start()
        first = r._thread
        r.start()
        self.assertIs(r._thread, first)
        r.stop(timeout=2.0)

    def test_stop_event_responsive_within_one_slice(self) -> None:
        """The thread leaves within ~0.5 s of the stop event being
        set. We tolerate up to 2 s for slow CI."""
        prov = _FakeProvider()
        r = StoreReaperThread(
            store_cfg=_store_cfg(interval=3600.0),
            provider=prov, distiller=None, store=None,
        )
        r.start()
        t0 = time.time()
        r.stop(timeout=2.0)
        elapsed = time.time() - t0
        self.assertLess(elapsed, 2.0)
        self.assertFalse(r.is_alive)


# ============================================================== #
# sweep_once — happy paths
# ============================================================== #


class TestSweepHappyPaths(unittest.TestCase):

    def test_no_expired_rows_returns_zero_stats(self) -> None:
        prov = _FakeProvider([])
        r = StoreReaperThread(
            store_cfg=_store_cfg(),
            provider=prov, distiller=None, store=None,
        )
        result = r.sweep_once()
        self.assertEqual(result["expired"], 0)
        self.assertEqual(result["distilled"], 0)
        self.assertEqual(result["deleted"], 0)
        # No delete calls.
        self.assertEqual(prov.delete_calls, [])

    def test_research_above_threshold_distills_then_deletes(self) -> None:
        rows = [
            _row(f"r{i}", ns=("csl-A", "research"), cwd="/proj/a", lane_idx=i)
            for i in range(4)
        ]
        prov = _FakeProvider([rows])
        distiller = _FakeDistiller(summary="THE DISTILLED.")
        store = _FakeStore()
        r = StoreReaperThread(
            store_cfg=_store_cfg(min_entries=3),
            provider=prov, distiller=distiller, store=store,
        )
        result = r.sweep_once()
        self.assertEqual(result["expired"], 4)
        self.assertEqual(result["distilled"], 1)
        self.assertEqual(result["deleted"], 4)
        # Distiller was invoked exactly once with all 4 rows.
        self.assertEqual(len(distiller.calls), 1)
        self.assertEqual(distiller.calls[0]["sid"], "csl-A")
        self.assertEqual(len(distiller.calls[0]["rows"]), 4)
        self.assertEqual(distiller.calls[0]["cwd_hint"], "/proj/a")
        # Summary was written into ("project", pid).
        self.assertEqual(len(store.puts), 1)
        ns, _, value, _ = store.puts[0]
        self.assertEqual(ns[0], "project")
        self.assertEqual(len(ns[1]), 12)
        self.assertEqual(value["text"], "THE DISTILLED.")
        self.assertEqual(value["distilled_from_sid"], "csl-A")
        self.assertEqual(value["original_count"], 4)
        # Originals were deleted.
        self.assertEqual(len(prov.delete_calls), 1)
        self.assertEqual(len(prov.delete_calls[0]), 4)

    def test_tool_results_bypass_distillation(self) -> None:
        """tool_results rows go straight to delete — no LLM call,
        no project-namespace write."""
        rows = [
            _row(f"t{i}", ns=("csl-T", "tool_results"))
            for i in range(5)
        ]
        prov = _FakeProvider([rows])
        distiller = _FakeDistiller()
        store = _FakeStore()
        r = StoreReaperThread(
            store_cfg=_store_cfg(),
            provider=prov, distiller=distiller, store=store,
        )
        result = r.sweep_once()
        self.assertEqual(result["expired"], 5)
        self.assertEqual(result["distilled"], 0)
        self.assertEqual(result["deleted"], 5)
        # Distiller never called.
        self.assertEqual(distiller.calls, [])
        # No project-namespace write.
        self.assertEqual(store.puts, [])
        # Delete happened.
        self.assertEqual(len(prov.delete_calls), 1)
        self.assertEqual(len(prov.delete_calls[0]), 5)

    def test_mixed_groups_each_handled_independently(self) -> None:
        """One research group above threshold, one tool_results
        group, one research group below threshold — sweep handles
        all three correctly in a single tick."""
        rows = [
            _row("r1", ns=("csl-X", "research"), cwd="/p/x", lane_idx=0),
            _row("r2", ns=("csl-X", "research"), cwd="/p/x", lane_idx=1),
            _row("r3", ns=("csl-X", "research"), cwd="/p/x", lane_idx=2),
            _row("t1", ns=("csl-X", "tool_results")),
            _row("only", ns=("csl-Y", "research"), cwd="/p/y", lane_idx=0),
        ]
        prov = _FakeProvider([rows])
        distiller = _FakeDistiller()
        store = _FakeStore()
        r = StoreReaperThread(
            store_cfg=_store_cfg(min_entries=3),
            provider=prov, distiller=distiller, store=store,
        )
        result = r.sweep_once()
        self.assertEqual(result["expired"], 5)
        # csl-X research (3) → distilled. csl-Y research (1) →
        # below threshold, skipped + deleted.
        self.assertEqual(result["distilled"], 1)
        self.assertEqual(result["groups_skipped_below_threshold"], 1)
        # All 5 rows ultimately deleted.
        total_deleted = sum(len(call) for call in prov.delete_calls)
        self.assertEqual(total_deleted, 5)
        # Only one summary written.
        self.assertEqual(len(store.puts), 1)
        # Distiller invoked exactly once (for the qualifying group).
        self.assertEqual(len(distiller.calls), 1)
        self.assertEqual(distiller.calls[0]["sid"], "csl-X")


# ============================================================== #
# sweep_once — cost gate
# ============================================================== #


class TestSweepCostGate(unittest.TestCase):

    def test_below_min_entries_skips_distillation(self) -> None:
        """Single-finding session → no LLM call, just delete."""
        rows = [
            _row("only", ns=("csl-Q", "research"), cwd="/p"),
        ]
        prov = _FakeProvider([rows])
        distiller = _FakeDistiller()
        store = _FakeStore()
        r = StoreReaperThread(
            store_cfg=_store_cfg(min_entries=3),
            provider=prov, distiller=distiller, store=store,
        )
        result = r.sweep_once()
        self.assertEqual(result["distilled"], 0)
        self.assertEqual(result["groups_skipped_below_threshold"], 1)
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(distiller.calls, [])
        self.assertEqual(store.puts, [])


# ============================================================== #
# sweep_once — failure isolation (CRITICAL INVARIANTS)
# ============================================================== #


class TestSweepFailureIsolation(unittest.TestCase):

    def test_distillation_failure_keeps_originals(self) -> None:
        """CRITICAL: failure → no delete for that group. The next
        sweep will retry. This is the invariant M14 was designed
        around — a silent delete here would be data loss."""
        rows = [
            _row(f"r{i}", ns=("csl-FAIL", "research"), cwd="/p", lane_idx=i)
            for i in range(3)
        ]
        prov = _FakeProvider([rows])
        distiller = _FakeDistiller(raise_for_sids={"csl-FAIL"})
        store = _FakeStore()
        r = StoreReaperThread(
            store_cfg=_store_cfg(min_entries=3),
            provider=prov, distiller=distiller, store=store,
        )
        result = r.sweep_once()
        self.assertEqual(result["expired"], 3)
        self.assertEqual(result["distilled"], 0)
        self.assertEqual(result["groups_distill_failed"], 1)
        # No delete happened for the failed group.
        self.assertEqual(result["deleted"], 0)
        self.assertEqual(prov.delete_calls, [])
        # No project-namespace write either.
        self.assertEqual(store.puts, [])

    def test_one_group_failure_does_not_poison_other_groups(self) -> None:
        """One sid fails distillation; another sid succeeds. The
        sweep keeps the failed group's originals AND completes the
        successful group's distill+delete."""
        rows = [
            # Failing group.
            _row("a1", ns=("csl-BAD", "research"), cwd="/p/bad", lane_idx=0),
            _row("a2", ns=("csl-BAD", "research"), cwd="/p/bad", lane_idx=1),
            _row("a3", ns=("csl-BAD", "research"), cwd="/p/bad", lane_idx=2),
            # Succeeding group.
            _row("b1", ns=("csl-OK", "research"), cwd="/p/ok", lane_idx=0),
            _row("b2", ns=("csl-OK", "research"), cwd="/p/ok", lane_idx=1),
            _row("b3", ns=("csl-OK", "research"), cwd="/p/ok", lane_idx=2),
        ]
        prov = _FakeProvider([rows])
        distiller = _FakeDistiller(raise_for_sids={"csl-BAD"})
        store = _FakeStore()
        r = StoreReaperThread(
            store_cfg=_store_cfg(min_entries=3),
            provider=prov, distiller=distiller, store=store,
        )
        result = r.sweep_once()
        self.assertEqual(result["expired"], 6)
        self.assertEqual(result["distilled"], 1)
        self.assertEqual(result["groups_distill_failed"], 1)
        # Only 3 rows (the OK group) deleted.
        total_deleted = sum(len(c) for c in prov.delete_calls)
        self.assertEqual(total_deleted, 3)
        # The successful group's summary was written.
        self.assertEqual(len(store.puts), 1)
        _, _, value, _ = store.puts[0]
        self.assertEqual(value["distilled_from_sid"], "csl-OK")
        # Distiller called for both groups (it tried BAD first or
        # second; order is irrelevant).
        sids = {c["sid"] for c in distiller.calls}
        self.assertEqual(sids, {"csl-BAD", "csl-OK"})

    def test_durable_write_failure_keeps_originals(self) -> None:
        """Regression for the M14 critical invariant — surfaced by
        csl-2026-05-18-1554-c8dc (#212).

        Pre-#212 ``ProviderBackedStore._do_put`` swallowed
        durable-write failures and returned silently. The reaper
        then saw ``_write_summary`` succeed and proceeded to delete
        the originals, even though the project-namespace summary
        never landed. Result: distilled originals gone, summary
        missing, data loss.

        Post-#212 ``_do_put`` propagates the failure and
        ``write_distilled_summary`` wraps it as
        :class:`DistillationFailed`, which the reaper's existing
        ``except DistillationFailed`` block at ``sweep_once``
        catches. Originals stay, next tick retries.

        This test exercises the end-to-end path: distillation
        succeeds, the store-side put fails (durable write down),
        the reaper must NOT call ``delete_by_hashes`` for the
        group."""
        rows = [
            _row(f"r{i}", ns=("csl-WRITE-DOWN", "research"),
                 cwd="/p", lane_idx=i)
            for i in range(3)
        ]
        prov = _FakeProvider([rows])
        # Distillation itself returns fine — the LLM is healthy.
        distiller = _FakeDistiller()
        # Store-side put is what's broken. Simulates the post-#212
        # contract: durable provider write fails and the exception
        # propagates out of ProviderBackedStore.put.
        store = _FakeStore()
        store.fail_put = RuntimeError("durable provider down")
        r = StoreReaperThread(
            store_cfg=_store_cfg(min_entries=3),
            provider=prov, distiller=distiller, store=store,
        )

        result = r.sweep_once()

        self.assertEqual(result["expired"], 3)
        # Distillation ran, but the write failed — so distilled
        # counter does NOT advance.
        self.assertEqual(result["distilled"], 0)
        # The reaper accounted this as a distill failure (its
        # ``except DistillationFailed`` block at sweep_once is what
        # catches the wrapped store error).
        self.assertEqual(result["groups_distill_failed"], 1)
        # CRITICAL: originals stayed. The next sweep tick will
        # retry.
        self.assertEqual(result["deleted"], 0)
        self.assertEqual(prov.delete_calls, [])
        # Distiller was invoked once (the failure is store-side).
        self.assertEqual(len(distiller.calls), 1)

    def test_expire_before_provider_failure_returns_zero_stats(self) -> None:
        """Provider blow-up → sweep is a no-op tick, not a thread
        crash. The reaper logs and moves on."""
        prov = _FakeProvider()
        prov.expire_raises = RuntimeError("DB exploded")
        r = StoreReaperThread(
            store_cfg=_store_cfg(),
            provider=prov, distiller=None, store=None,
        )
        result = r.sweep_once()
        self.assertEqual(result["expired"], 0)
        self.assertEqual(result["deleted"], 0)

    def test_distillation_disabled_deletes_research_without_llm(self) -> None:
        """If distillation is disabled in config, the reaper falls
        back to "delete research like tool_results" — never holds
        rows hostage."""
        rows = [
            _row(f"r{i}", ns=("csl-D", "research"), cwd="/p", lane_idx=i)
            for i in range(5)
        ]
        prov = _FakeProvider([rows])
        distiller = _FakeDistiller()
        store = _FakeStore()
        r = StoreReaperThread(
            store_cfg=_store_cfg(distill_enabled=False),
            provider=prov, distiller=distiller, store=store,
        )
        result = r.sweep_once()
        self.assertEqual(result["expired"], 5)
        self.assertEqual(result["distilled"], 0)
        self.assertEqual(result["deleted"], 5)
        # Distiller never called.
        self.assertEqual(distiller.calls, [])

    def test_no_distiller_or_no_store_skips_distillation(self) -> None:
        """Reaper instantiated without a distiller / store (the
        ``cfg.store.distillation.enabled = False`` short-circuit at
        the app-factory level) still cleanly deletes."""
        rows = [
            _row(f"r{i}", ns=("csl-N", "research"), cwd="/p")
            for i in range(3)
        ]
        prov = _FakeProvider([rows])
        # No distiller, no store.
        r = StoreReaperThread(
            store_cfg=_store_cfg(),
            provider=prov, distiller=None, store=None,
        )
        result = r.sweep_once()
        self.assertEqual(result["expired"], 3)
        self.assertEqual(result["distilled"], 0)
        self.assertEqual(result["deleted"], 3)


# ============================================================== #
# Recorded result + last_sweep_at
# ============================================================== #


class TestSweepObservability(unittest.TestCase):

    def test_last_sweep_result_records_stats(self) -> None:
        rows = [_row("t1", ns=("csl-A", "tool_results"))]
        prov = _FakeProvider([rows])
        r = StoreReaperThread(
            store_cfg=_store_cfg(),
            provider=prov, distiller=None, store=None,
        )
        self.assertIsNone(r.last_sweep_result)
        result = r.sweep_once()
        self.assertEqual(r.last_sweep_result, result)
        self.assertIsNotNone(r.last_sweep_at)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
