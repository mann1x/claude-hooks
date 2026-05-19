"""M14 — ProviderBackedStore TTL filter + refresh-on-read.

The M8 store grew per-namespace TTL semantics in M14: ``_do_put``
stamps ``expires_at`` on the provider metadata, ``_do_get`` and
``_do_search`` filter expired items, and recall hits roll their
``expires_at`` forward when ``refresh_on_read = True``. Without a
ttl_config (or with ``ttl_config.enabled = False``) the store
behaves identically to M8 — every row lives forever.

These tests run only in environments where LangGraph is installed
(the consultants env); they skip cleanly elsewhere.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional


try:
    from langgraph.store.base import BaseStore  # noqa: F401
    HAVE_LANGGRAPH = True
except ImportError:
    HAVE_LANGGRAPH = False


# ============================================================== #
# Fakes — a provider that records expires_at writes + refresh /
# delete calls so the tests can assert on the protocol surface.
# ============================================================== #


@dataclass
class _FakeMemory:
    text: str
    metadata: dict


@dataclass
class _Stored:
    """One row's worth of state kept by ``_TTLFakeProvider``. The
    in-process index is hand-rolled to mirror what the real
    pgvector / sqlite_vec store + recall_hybrid + refresh_expires_at
    flow does, so the tests exercise the same paths as a live deploy."""
    text: str
    metadata: dict
    expires_at: Optional[str] = None


class _TTLFakeProvider:
    """Minimal duck-typed provider that participates in the M14 TTL
    contract:

    - ``store`` records (text, metadata) and pulls ``expires_at`` out
      of metadata into its own column-shaped slot.
    - ``recall_hybrid`` re-attaches the slot to metadata before
      returning, mirroring what the real providers do via JOIN.
    - ``refresh_expires_at`` bumps the slot keyed by ``content_hash``.
    - ``delete_by_hashes`` evicts by content_hash.
    """

    def __init__(self):
        from claude_hooks.providers._content_hash import content_hash
        self._ch = content_hash
        self._rows: dict[bytes, _Stored] = {}
        self.refresh_calls: list[tuple[bytes, str]] = []
        self.delete_calls: list[list[bytes]] = []
        self.store_calls: list[tuple[str, dict]] = []

    def store(self, content: str, metadata: dict) -> None:
        self.store_calls.append((content, dict(metadata)))
        h = self._ch(content)
        exp = None
        meta_copy = dict(metadata)
        if isinstance(meta_copy.get("expires_at"), str):
            exp = meta_copy["expires_at"]
        # Mirror the real providers: expires_at lives in its own
        # column, but recall_hybrid returns it via metadata.
        self._rows[h] = _Stored(
            text=content, metadata=meta_copy, expires_at=exp,
        )

    def recall_hybrid(self, query: str, k: int = 5) -> list[_FakeMemory]:
        out = []
        # Insertion order is good enough for unit tests; the marker
        # filter + namespace-prefix check downstream do the real work.
        for h, row in list(self._rows.items())[:k]:
            meta = dict(row.metadata)
            if row.expires_at is not None:
                meta["expires_at"] = row.expires_at
            else:
                meta.pop("expires_at", None)
            out.append(_FakeMemory(text=row.text, metadata=meta))
        return out

    def refresh_expires_at(
        self, content_hash_bytes: bytes, new_iso: str,
    ) -> None:
        self.refresh_calls.append((content_hash_bytes, new_iso))
        row = self._rows.get(content_hash_bytes)
        if row is not None:
            row.expires_at = new_iso

    def delete_by_hashes(self, hashes: list) -> int:
        self.delete_calls.append(list(hashes))
        deleted = 0
        for h in hashes:
            if h in self._rows:
                self._rows.pop(h)
                deleted += 1
        return deleted

    def expire_before(self, *, before_iso: str, limit: int = 1000):
        from claude_hooks.providers._content_hash import ExpiringRow
        out: list = []
        for h, row in self._rows.items():
            if row.expires_at and row.expires_at < before_iso:
                out.append(ExpiringRow(
                    content_hash=h, content=row.text,
                    metadata=row.metadata, expires_at=row.expires_at,
                ))
        return out[:limit]


def _ttl_cfg(**overrides):
    """Build a StoreTTLConfig with explicit defaults for tests.

    All TTLs default off (None) so tests opt into specific TTLs
    one at a time. Pass ``enabled=False`` to mimic the M8 path.

    Jitter (#215) is disabled (``jitter_pct = 0.0``) by default so
    the TTL math here stays deterministic — jitter is exercised
    separately in :mod:`test_consultants_v2_store_reaper`. Tests
    that want to see jitter pass ``jitter_pct=0.1`` explicitly.
    """
    from consultants.config import StoreTTLConfig
    kwargs = dict(
        enabled=True,
        research_days=None,
        tool_results_hours=None,
        project_days=None,
        user_days=None,
        refresh_on_read=False,
        jitter_pct=0.0,
    )
    kwargs.update(overrides)
    return StoreTTLConfig(**kwargs)


# ============================================================== #
# TTL math — ``StoreTTLConfig.ttl_for_namespace``
# ============================================================== #


class TestTTLForNamespace(unittest.TestCase):
    """The pure-function math that turns a namespace tuple into a
    TTL in seconds. No LangGraph required."""

    def test_research_kind_uses_research_days(self):
        cfg = _ttl_cfg(research_days=30.0)
        self.assertEqual(
            cfg.ttl_for_namespace(("csl-abc", "research")),
            30 * 86400.0,
        )

    def test_tool_results_kind_uses_tool_results_hours(self):
        cfg = _ttl_cfg(tool_results_hours=24.0)
        self.assertEqual(
            cfg.ttl_for_namespace(("csl-abc", "tool_results")),
            24 * 3600.0,
        )

    def test_project_namespace_uses_project_days(self):
        cfg = _ttl_cfg(project_days=7.0)
        self.assertEqual(
            cfg.ttl_for_namespace(("project", "pid-abc")),
            7 * 86400.0,
        )

    def test_user_namespace_uses_user_days(self):
        cfg = _ttl_cfg(user_days=14.0)
        self.assertEqual(
            cfg.ttl_for_namespace(("user", "uid-xyz")),
            14 * 86400.0,
        )

    def test_project_with_none_returns_none(self):
        cfg = _ttl_cfg(project_days=None)
        self.assertIsNone(
            cfg.ttl_for_namespace(("project", "pid-abc")),
        )

    def test_user_with_none_returns_none(self):
        cfg = _ttl_cfg(user_days=None)
        self.assertIsNone(
            cfg.ttl_for_namespace(("user", "uid-abc")),
        )

    def test_unknown_kind_returns_none(self):
        cfg = _ttl_cfg(research_days=30.0)
        # A sid-namespace with an unknown second segment.
        self.assertIsNone(
            cfg.ttl_for_namespace(("csl-abc", "exotic")),
        )

    def test_bad_namespace_shape_returns_none(self):
        cfg = _ttl_cfg(research_days=30.0)
        # 1-tuple, 3-tuple, etc.
        self.assertIsNone(cfg.ttl_for_namespace(("just-head",)))
        self.assertIsNone(
            cfg.ttl_for_namespace(("a", "b", "c")),
        )


# ============================================================== #
# ProviderBackedStore — TTL plumbing (langgraph-gated)
# ============================================================== #


@unittest.skipUnless(HAVE_LANGGRAPH, "langgraph not installed")
class TestPutStampsExpiresAt(unittest.TestCase):
    """``_do_put`` writes ``expires_at`` to provider metadata when
    the namespace has a TTL; omits it otherwise."""

    def _store(self, ttl_config=None):
        from consultants.engine.store import ProviderBackedStore
        provider = _TTLFakeProvider()
        store = ProviderBackedStore(provider, ttl_config=ttl_config)
        return store, provider

    def test_no_ttl_config_omits_expires_at(self):
        # No ttl_config — strict M8 contract.
        store, provider = self._store(ttl_config=None)
        store.put(("csl-a", "research"), "k1", {"text": "hi"})
        text, meta = provider.store_calls[0]
        self.assertNotIn("expires_at", meta)

    def test_disabled_ttl_config_omits_expires_at(self):
        cfg = _ttl_cfg(enabled=False, research_days=30.0)
        store, provider = self._store(ttl_config=cfg)
        store.put(("csl-a", "research"), "k1", {"text": "hi"})
        text, meta = provider.store_calls[0]
        self.assertNotIn("expires_at", meta)

    def test_research_namespace_gets_expires_at(self):
        cfg = _ttl_cfg(research_days=30.0)
        store, provider = self._store(ttl_config=cfg)
        store.put(("csl-a", "research"), "k1", {"text": "hi"})
        text, meta = provider.store_calls[0]
        self.assertIn("expires_at", meta)
        # ~30 days out — give a 1-minute window for test scheduling.
        exp = datetime.fromisoformat(meta["expires_at"])
        delta = exp - datetime.now(timezone.utc)
        self.assertGreater(delta.total_seconds(), 30 * 86400 - 60)
        self.assertLess(delta.total_seconds(), 30 * 86400 + 60)

    def test_tool_results_namespace_gets_expires_at(self):
        cfg = _ttl_cfg(tool_results_hours=24.0)
        store, provider = self._store(ttl_config=cfg)
        store.put(
            ("csl-a", "tool_results"), "k1", {"text": "hi"},
        )
        text, meta = provider.store_calls[0]
        self.assertIn("expires_at", meta)
        exp = datetime.fromisoformat(meta["expires_at"])
        delta = exp - datetime.now(timezone.utc)
        self.assertGreater(delta.total_seconds(), 24 * 3600 - 60)
        self.assertLess(delta.total_seconds(), 24 * 3600 + 60)

    def test_project_namespace_never_expires_when_days_none(self):
        cfg = _ttl_cfg(project_days=None)
        store, provider = self._store(ttl_config=cfg)
        store.put(("project", "pid-x"), "k1", {"text": "hi"})
        text, meta = provider.store_calls[0]
        self.assertNotIn("expires_at", meta)

    def test_user_namespace_never_expires_when_days_none(self):
        cfg = _ttl_cfg(user_days=None)
        store, provider = self._store(ttl_config=cfg)
        store.put(("user", "uid-x"), "k1", {"text": "hi"})
        text, meta = provider.store_calls[0]
        self.assertNotIn("expires_at", meta)


@unittest.skipUnless(HAVE_LANGGRAPH, "langgraph not installed")
class TestSearchFiltersExpired(unittest.TestCase):
    """``_do_search`` drops expired hits server-side via the
    metadata.expires_at field."""

    def _store(self, ttl_config):
        from consultants.engine.store import ProviderBackedStore
        provider = _TTLFakeProvider()
        store = ProviderBackedStore(provider, ttl_config=ttl_config)
        return store, provider

    def _seed_with_expiry(self, provider, *, text, ns, expires_at):
        from claude_hooks.providers._content_hash import content_hash
        # Use the marker the store filter expects.
        meta = {
            "_consultants_store": True,
            "namespace": list(ns),
            "key": text[:6],
            "value": {"text": text},
            "created_at": (
                datetime.now(timezone.utc).isoformat()
            ),
            "updated_at": (
                datetime.now(timezone.utc).isoformat()
            ),
            "expires_at": expires_at,
        }
        provider._rows[content_hash(text)] = _Stored(
            text=text, metadata=meta, expires_at=expires_at,
        )

    def test_expired_hit_skipped(self):
        store, provider = self._store(_ttl_cfg(research_days=30.0))
        # Past expiry.
        self._seed_with_expiry(
            provider, text="finding-A", ns=("csl-a", "research"),
            expires_at="2020-01-01T00:00:00+00:00",
        )
        results = store.search(("csl-a", "research"), query="finding")
        self.assertEqual(len(results), 0)

    def test_unexpired_hit_returned(self):
        store, provider = self._store(_ttl_cfg(research_days=30.0))
        future = (
            datetime.now(timezone.utc) + timedelta(days=10)
        ).isoformat()
        self._seed_with_expiry(
            provider, text="finding-A", ns=("csl-a", "research"),
            expires_at=future,
        )
        results = store.search(("csl-a", "research"), query="finding")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].key, "findin")

    def test_null_expires_at_returned_normally(self):
        # Legacy / pre-M14 row with no expires_at metadata.
        store, provider = self._store(_ttl_cfg(research_days=30.0))
        self._seed_with_expiry(
            provider, text="legacy", ns=("csl-a", "research"),
            expires_at=None,
        )
        results = store.search(("csl-a", "research"), query="legacy")
        self.assertEqual(len(results), 1)


@unittest.skipUnless(HAVE_LANGGRAPH, "langgraph not installed")
class TestRefreshOnRead(unittest.TestCase):
    """When ``refresh_on_read = True``, a successful recall hit
    bumps ``expires_at`` forward on the provider AND the in-process
    index so the two views agree."""

    def _store(self, *, refresh_on_read):
        from consultants.engine.store import ProviderBackedStore
        cfg = _ttl_cfg(
            research_days=30.0, refresh_on_read=refresh_on_read,
        )
        provider = _TTLFakeProvider()
        store = ProviderBackedStore(provider, ttl_config=cfg)
        return store, provider, cfg

    def test_refresh_on_read_calls_provider_refresh(self):
        store, provider, _ = self._store(refresh_on_read=True)
        store.put(("csl-a", "research"), "k1", {"text": "hello"})
        # Capture what the provider actually stored — when ``index``
        # is the LangGraph default (None / []), the adapter serializes
        # the whole value dict via ``_extract_indexable_text`` and
        # stores that as the row's content. The refresh-on-read call
        # must use the same text → same content_hash.
        stored_text = provider.store_calls[0][0]
        provider.refresh_calls.clear()
        store.search(("csl-a", "research"), query="hello")
        self.assertEqual(len(provider.refresh_calls), 1)
        from claude_hooks.providers._content_hash import content_hash
        h, new_iso = provider.refresh_calls[0]
        # The hash must match the SAME text the provider stored —
        # otherwise the UPDATE would touch zero rows.
        self.assertEqual(h, content_hash(stored_text))
        exp = datetime.fromisoformat(new_iso)
        self.assertGreater(exp, datetime.now(timezone.utc))

    def test_refresh_disabled_does_not_call_provider(self):
        store, provider, _ = self._store(refresh_on_read=False)
        store.put(("csl-a", "research"), "k1", {"text": "hello"})
        provider.refresh_calls.clear()
        store.search(("csl-a", "research"), query="hello")
        self.assertEqual(provider.refresh_calls, [])

    def test_refresh_noop_for_null_ttl_namespace(self):
        # Project namespace has no TTL → refresh would have nothing
        # meaningful to compute (no ``ttl_for_namespace``), and the
        # provider call would be wasteful.
        from consultants.engine.store import ProviderBackedStore
        cfg = _ttl_cfg(project_days=None, refresh_on_read=True)
        provider = _TTLFakeProvider()
        store = ProviderBackedStore(provider, ttl_config=cfg)
        store.put(("project", "pid"), "k1", {"text": "durable"})
        provider.refresh_calls.clear()
        store.search(("project", "pid"), query="durable")
        self.assertEqual(provider.refresh_calls, [])


@unittest.skipUnless(HAVE_LANGGRAPH, "langgraph not installed")
class TestGetFiltersExpired(unittest.TestCase):
    """``_do_get`` returns ``None`` for items whose in-process
    ``updated_at + TTL`` is in the past."""

    def _store(self, ttl_config):
        from consultants.engine.store import ProviderBackedStore
        provider = _TTLFakeProvider()
        store = ProviderBackedStore(provider, ttl_config=ttl_config)
        return store, provider

    def test_unexpired_get_returns_item(self):
        store, provider = self._store(_ttl_cfg(research_days=30.0))
        store.put(("csl-a", "research"), "k1", {"text": "fresh"})
        item = store.get(("csl-a", "research"), "k1")
        self.assertIsNotNone(item)
        self.assertEqual(item.value["text"], "fresh")

    def test_get_returns_none_when_in_process_anchor_is_old(self):
        # Stuff a fake item directly into the index with an
        # updated_at in the deep past.
        from consultants.engine.store import ProviderBackedStore
        from langgraph.store.base import Item
        cfg = _ttl_cfg(research_days=0.0001)  # ~9 seconds
        provider = _TTLFakeProvider()
        store = ProviderBackedStore(provider, ttl_config=cfg)
        ns = ("csl-a", "research")
        # Synthesize an item with updated_at = 30 days ago.
        old = datetime.now(timezone.utc) - timedelta(days=30)
        store._index[ns]["k-old"] = Item(
            value={"text": "stale"}, key="k-old", namespace=ns,
            created_at=old, updated_at=old,
        )
        item = store.get(ns, "k-old")
        self.assertIsNone(item)


@unittest.skipUnless(HAVE_LANGGRAPH, "langgraph not installed")
class TestMakeConsultantsStoreThreadsTTL(unittest.TestCase):
    """``make_consultants_store`` forwards ``cfg.store.ttl`` into the
    adapter's ``ttl_config`` slot when the backend is provider-
    based. The ``memory`` backend (LangGraph's InMemoryStore) is
    exempt — that path is for test scaffolding only."""

    def test_factory_passes_ttl_config_for_sqlite_vec_backend(self):
        from consultants.config import ConsultantsConfig
        from consultants.engine.store import (
            make_consultants_store, ProviderBackedStore,
        )
        cfg = ConsultantsConfig()
        cfg.store.enabled = True
        cfg.store.backend = "sqlite_vec"
        cfg.store.ttl.enabled = True

        # Inject a fake provider via the test seam so we don't need
        # a real sqlite_vec DB on disk.
        provider = _TTLFakeProvider()

        def loader(_):
            return provider

        store = make_consultants_store(
            cfg, sid="csl-x", effort="high",
            provider_loader=loader,
        )
        self.assertIsInstance(store, ProviderBackedStore)
        self.assertIsNotNone(store._ttl)
        self.assertEqual(store._ttl, cfg.store.ttl)

    def test_factory_drops_ttl_config_when_disabled(self):
        # cfg.store.ttl.enabled = False → adapter sees ttl as None.
        from consultants.config import ConsultantsConfig
        from consultants.engine.store import (
            make_consultants_store, ProviderBackedStore,
        )
        cfg = ConsultantsConfig()
        cfg.store.enabled = True
        cfg.store.backend = "sqlite_vec"
        cfg.store.ttl.enabled = False

        provider = _TTLFakeProvider()
        store = make_consultants_store(
            cfg, sid="csl-x", effort="high",
            provider_loader=lambda _: provider,
        )
        self.assertIsInstance(store, ProviderBackedStore)
        # Adapter normalizes "disabled" → internal None so the
        # hot-path skips the TTL branches entirely.
        self.assertIsNone(store._ttl)


if __name__ == "__main__":
    unittest.main()
