"""Tests for the optional batch_recall / batch_store Provider API (Tier 2.6)."""
from __future__ import annotations

from typing import Optional
from unittest.mock import MagicMock

import pytest

from claude_hooks.providers.base import Memory, Provider, ServerCandidate


class _StubProvider(Provider):
    """Minimal Provider impl that tracks single-shot calls."""

    name = "stub"

    @classmethod
    def detect(cls, claude_config: dict) -> list[ServerCandidate]:
        return []

    @classmethod
    def signature_tools(cls) -> set[str]:
        return set()

    def __init__(self):
        # Skip the parent __init__ so we don't need a ServerCandidate.
        self.server = None  # type: ignore[assignment]
        self.options = {}
        self.recall_calls: list[tuple[str, int]] = []
        self.store_calls: list[tuple[str, Optional[dict]]] = []
        self._recall_results: dict[str, list[Memory]] = {}

    def set_recall(self, query: str, mems: list[Memory]) -> None:
        self._recall_results[query] = mems

    def recall(self, query: str, k: int = 5) -> list[Memory]:
        self.recall_calls.append((query, k))
        return self._recall_results.get(query, [])

    def store(self, content: str, metadata: Optional[dict] = None) -> None:
        self.store_calls.append((content, metadata))


# ===================================================================== #
# batch_recall — default implementation
# ===================================================================== #
class TestBatchRecallDefault:
    def test_empty_queries_returns_empty(self):
        p = _StubProvider()
        assert p.batch_recall([]) == []
        assert p.recall_calls == []

    def test_single_query_routes_to_single_shot(self):
        p = _StubProvider()
        p.set_recall("q", [Memory(text="m1")])
        out = p.batch_recall(["q"], k=3)
        assert len(out) == 1
        assert out[0][0].text == "m1"
        assert p.recall_calls == [("q", 3)]

    def test_multiple_queries_each_get_recall(self):
        p = _StubProvider()
        p.set_recall("a", [Memory(text="A")])
        p.set_recall("b", [Memory(text="B")])
        p.set_recall("c", [Memory(text="C")])
        out = p.batch_recall(["a", "b", "c"], k=5)
        assert len(out) == 3
        # Order preserved.
        assert out[0][0].text == "A"
        assert out[1][0].text == "B"
        assert out[2][0].text == "C"
        # All three queries hit recall.
        called_queries = [q for q, _ in p.recall_calls]
        assert sorted(called_queries) == ["a", "b", "c"]
        # k forwarded.
        for _, k in p.recall_calls:
            assert k == 5

    def test_recall_failure_yields_empty_list_not_none(self):
        p = _StubProvider()
        # First two recalls succeed; the third raises. parallel_map's
        # on_error swallows; default impl normalises None → [].
        p.set_recall("a", [Memory(text="A")])
        p.set_recall("b", [Memory(text="B")])
        original_recall = p.recall

        def flaky(query: str, k: int = 5):
            if query == "c":
                raise RuntimeError("backend down")
            return original_recall(query, k=k)

        p.recall = flaky  # type: ignore[assignment]
        out = p.batch_recall(["a", "b", "c"], k=2)
        assert len(out) == 3
        assert out[0][0].text == "A"
        assert out[1][0].text == "B"
        assert out[2] == []  # failed query → empty list, not None


# ===================================================================== #
# batch_store — default implementation
# ===================================================================== #
class TestBatchStoreDefault:
    def test_empty_items_is_noop(self):
        p = _StubProvider()
        p.batch_store([])
        assert p.store_calls == []

    def test_single_item_routes_to_single_shot(self):
        p = _StubProvider()
        p.batch_store([("content", {"k": "v"})])
        assert p.store_calls == [("content", {"k": "v"})]

    def test_multiple_items_each_stored(self):
        p = _StubProvider()
        items = [
            ("c1", {"i": 1}),
            ("c2", {"i": 2}),
            ("c3", {"i": 3}),
        ]
        p.batch_store(items)
        assert len(p.store_calls) == 3
        # Items may complete in any order via parallel_map — sort to compare.
        stored = sorted(p.store_calls, key=lambda x: x[1]["i"])  # type: ignore[index]
        assert stored == items

    def test_metadata_none_passes_through(self):
        p = _StubProvider()
        p.batch_store([("c", None), ("d", None)])
        for _, md in p.store_calls:
            assert md is None


# ===================================================================== #
# Subclass override — verify the ABC defers correctly
# ===================================================================== #
class TestSubclassOverride:
    def test_override_used_instead_of_default(self):
        called = {"native": 0, "single": 0}

        class _Native(_StubProvider):
            def batch_recall(self, queries, k=5):
                called["native"] += 1
                return [[Memory(text=f"native:{q}")] for q in queries]

            def recall(self, query, k=5):
                called["single"] += 1
                return [Memory(text=f"single:{query}")]

        p = _Native()
        out = p.batch_recall(["x", "y"], k=2)
        assert called["native"] == 1
        assert called["single"] == 0
        assert out[0][0].text == "native:x"
        assert out[1][0].text == "native:y"
