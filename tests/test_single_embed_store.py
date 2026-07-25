"""Tests for the single-embed store path.

A dedup-then-store cycle used to embed twice: ``content[:500]`` for the
near-duplicate search, then the full content for the write. On a CPU
embedder the content embed *is* the cost of the turn -- 12.7 s for 5 KB
against ~10 ms for every surrounding DB operation, measured on solidpc
2026-07-25 -- so the vector is now computed once and spent twice.

Two properties matter here and neither is obvious from the diff:

1. **Zero-config negotiation.** A provider signals support by returning a
   vector from ``embed_for_store``. One that returns None (the base-class
   default, and what every server-side-embedding provider inherits) must
   never see the ``vec`` keyword at all -- not even ``vec=None`` -- so
   third-party ``store(content, metadata)`` implementations keep working.
   ``conftest.FakeProvider`` deliberately has no ``vec`` parameter and is
   the regression subject for this.

2. **The dedup search widens.** Reuse means the search now runs on a
   full-content vector instead of a 500-character one, so it must go
   through ``recall_vec`` and *not* fall back to the truncated text path.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from claude_hooks import dedup, store_async  # noqa: E402
from claude_hooks.hooks import stop  # noqa: E402
from claude_hooks.providers.base import Memory, Provider, ServerCandidate  # noqa: E402


class VecProvider(Provider):
    """Provider that supports the single-embed path, with call counters."""

    name = "vecfake"
    display_name = "VecFake"

    def __init__(self, *, name: str = "vecfake",
                 recall_returns: Optional[list[Memory]] = None,
                 vec_supported: bool = True,
                 recall_vec_supported: bool = True):
        self.name = name
        self.display_name = name.capitalize()
        self.server = ServerCandidate(server_key=name, url="fake://", source="test")
        self.options: dict[str, Any] = {}
        self._recall_returns = recall_returns or []
        self._vec_supported = vec_supported
        self._recall_vec_supported = recall_vec_supported
        self.embed_calls: list[str] = []
        self.recall_calls: list[str] = []
        self.recall_vec_calls: list[list[float]] = []
        self.stored: list[tuple[str, dict, Optional[list[float]]]] = []

    @classmethod
    def detect(cls, claude_config: dict) -> list[ServerCandidate]:
        return []

    @classmethod
    def signature_tools(cls) -> set[str]:
        return set()

    def embed_for_store(self, content: str) -> Optional[list[float]]:
        if not self._vec_supported:
            return None
        self.embed_calls.append(content)
        return [0.5, 0.25, 0.125]

    def recall_vec(self, vec, k: int = 5):
        if not self._recall_vec_supported:
            return None
        self.recall_vec_calls.append(list(vec))
        return list(self._recall_returns)[:k]

    def recall(self, query: str, k: int = 5) -> list[Memory]:
        self.recall_calls.append(query)
        return list(self._recall_returns)[:k]

    def store(self, content: str, metadata: Optional[dict] = None,
              vec: Optional[list[float]] = None) -> None:
        self.stored.append((content, dict(metadata or {}), vec))


# ===================================================================== #
# Base-class defaults
# ===================================================================== #
class TestBaseDefaults:
    def test_embed_for_store_defaults_to_none(self, fake_provider):
        assert fake_provider().embed_for_store("anything") is None

    def test_recall_vec_defaults_to_none(self, fake_provider):
        assert fake_provider().recall_vec([0.1, 0.2], k=3) is None

    def test_none_means_unsupported_not_failure(self, fake_provider):
        """Both defaults must return, never raise -- the caller treats
        None as 'no reuse here' and carries on."""
        p = fake_provider()
        assert p.embed_for_store("") is None
        assert p.recall_vec([], k=1) is None


# ===================================================================== #
# dedup negotiation
# ===================================================================== #
class TestDedupNegotiation:
    def test_uses_recall_vec_when_vector_supplied(self):
        p = VecProvider(recall_returns=[Memory(text="unrelated text")])
        assert dedup.should_store("x" * 200, p, threshold=0.85,
                                  vec=[0.5, 0.25, 0.125]) is True
        assert p.recall_vec_calls == [[0.5, 0.25, 0.125]]
        assert p.recall_calls == [], "must not also run the text search"

    def test_falls_back_to_text_when_recall_vec_unsupported(self):
        p = VecProvider(recall_vec_supported=False,
                        recall_returns=[Memory(text="unrelated")])
        assert dedup.should_store("y" * 200, p, vec=[0.1]) is True
        assert p.recall_calls, "should have fallen back to recall()"

    def test_falls_back_when_no_vector_given(self):
        p = VecProvider(recall_returns=[Memory(text="unrelated")])
        assert dedup.should_store("z" * 200, p) is True
        assert p.recall_vec_calls == []
        assert p.recall_calls == ["z" * 200], "text path, truncated to 500"

    def test_near_duplicate_still_detected_through_vector_path(self):
        content = "the proxy retry layer honours retry-after " * 10
        p = VecProvider(recall_returns=[Memory(text=content)])
        assert dedup.should_store(content, p, threshold=0.85,
                                  vec=[0.5, 0.25, 0.125]) is False

    def test_recall_vec_error_fails_open(self):
        class Boom(VecProvider):
            def recall_vec(self, vec, k=5):
                raise RuntimeError("backend down")

        assert dedup.should_store("q" * 200, Boom(), vec=[0.1]) is True


# ===================================================================== #
# Stop hook wiring (inline path)
# ===================================================================== #
def _edit_transcript(transcript_file):
    return transcript_file(
        user="please edit",
        assistant_text="done",
        assistant_tools=[{"name": "Edit", "input": {"file_path": "x.py"}}],
    )


class TestStopHookInline:
    def test_vector_is_computed_once_and_handed_to_store(
        self, base_config, transcript_file,
    ):
        p = VecProvider(name="pgvector")
        stop.handle(
            event={"transcript_path": _edit_transcript(transcript_file),
                   "cwd": "/p", "session_id": "s1"},
            config=base_config(hooks={"stop": {"detach_store": False}}),
            providers=[p],
        )
        assert len(p.embed_calls) == 1, "exactly one embed per store"
        assert len(p.stored) == 1
        _content, _meta, vec = p.stored[0]
        assert vec == [0.5, 0.25, 0.125], "store must receive the reused vector"

    def test_unaware_provider_never_sees_the_vec_keyword(
        self, base_config, transcript_file, fake_provider,
    ):
        """conftest.FakeProvider.store() has no ``vec`` parameter. If the
        hook ever passes the keyword unconditionally this raises
        TypeError, the store is swallowed as a provider failure, and the
        memory is silently lost."""
        p = fake_provider(name="qdrant")
        stop.handle(
            event={"transcript_path": _edit_transcript(transcript_file),
                   "cwd": "/p", "session_id": "s1"},
            config=base_config(hooks={"stop": {"detach_store": False}}),
            providers=[p],
        )
        assert len(p.stored) == 1

    def test_embed_failure_degrades_to_the_old_path(
        self, base_config, transcript_file,
    ):
        class BadEmbed(VecProvider):
            def embed_for_store(self, content):
                raise RuntimeError("embedder down")

        p = BadEmbed(name="pgvector")
        stop.handle(
            event={"transcript_path": _edit_transcript(transcript_file),
                   "cwd": "/p", "session_id": "s1"},
            config=base_config(hooks={"stop": {"detach_store": False}}),
            providers=[p],
        )
        assert len(p.stored) == 1, "a dead embedder must not drop the memory"
        assert p.stored[0][2] is None


# ===================================================================== #
# Detached path (the one that actually runs with detach_store on)
# ===================================================================== #
class TestStoreAsync:
    def test_vector_reused_in_detached_child(self):
        p = VecProvider(name="pgvector")
        store_async._run_dedup_and_store(
            {"providers": {"pgvector": {"dedup_threshold": 0.85}}},
            "s" * 200, {"type": "session_turn"}, [p],
        )
        assert len(p.embed_calls) == 1
        assert p.recall_vec_calls == [[0.5, 0.25, 0.125]]
        assert p.stored and p.stored[0][2] == [0.5, 0.25, 0.125]

    def test_unaware_provider_never_sees_the_vec_keyword(self, fake_provider):
        p = fake_provider(name="qdrant")
        store_async._run_dedup_and_store(
            {"providers": {"qdrant": {"dedup_threshold": 0.0}}},
            "t" * 200, {"type": "session_turn"}, [p],
        )
        assert len(p.stored) == 1

    def test_dedup_skip_still_short_circuits(self):
        content = "the proxy retry layer honours retry-after " * 10
        p = VecProvider(name="pgvector",
                        recall_returns=[Memory(text=content)])
        store_async._run_dedup_and_store(
            {"providers": {"pgvector": {"dedup_threshold": 0.85}}},
            content, {}, [p],
        )
        assert p.stored == [], "near-duplicate must not be written"


# ===================================================================== #
# Provider-level: the vector actually replaces the embed
# ===================================================================== #
class _CountingEmbedder:
    dim = 3

    def __init__(self):
        self.calls: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        return [1.0, 0.0, 0.0]

    def embed_batch(self, texts):
        return [self.embed(t) for t in texts]


class TestSqliteVecProviderReuse:
    def _provider(self, tmp_path):
        pytest.importorskip("sqlite_vec")
        from claude_hooks.providers.sqlite_vec import SqliteVecProvider
        p = SqliteVecProvider(
            ServerCandidate(server_key="sqlite_vec", url="", source="test"),
            {"db_path": str(tmp_path / "m.db"), "table": "memory",
             "embedder": "null"},
        )
        emb = _CountingEmbedder()
        p._embedder = emb
        return p, emb

    def test_store_with_vec_skips_the_embed(self, tmp_path):
        p, emb = self._provider(tmp_path)
        p.store("hello world", metadata={}, vec=[0.0, 1.0, 0.0])
        assert emb.calls == [], "supplied vector must replace the embed"

    def test_store_without_vec_still_embeds(self, tmp_path):
        p, emb = self._provider(tmp_path)
        p.store("hello world", metadata={})
        assert emb.calls == ["hello world"]

    def test_embed_for_store_returns_a_vector(self, tmp_path):
        p, emb = self._provider(tmp_path)
        assert p.embed_for_store("hello world") == [1.0, 0.0, 0.0]
        assert emb.calls == ["hello world"]

    def test_embed_for_store_ignores_blank(self, tmp_path):
        p, emb = self._provider(tmp_path)
        assert p.embed_for_store("   ") is None
        assert emb.calls == []

    def test_recall_vec_round_trips(self, tmp_path):
        p, emb = self._provider(tmp_path)
        p.store("hello world", metadata={}, vec=[1.0, 0.0, 0.0])
        hits = p.recall_vec([1.0, 0.0, 0.0], k=3)
        assert hits is not None
        assert any("hello world" in m.text for m in hits)
        assert emb.calls == [], "the whole point: no embed on either half"
