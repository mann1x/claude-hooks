"""M14 — ``create_app`` store-reaper wiring tests.

Exercises :func:`consultants.server.app._maybe_start_store_reaper`
gating + the ``app.state.store_reaper`` lifecycle hook. The
provider + distiller paths are mocked so the test runs without
LangGraph, FastAPI startup events, or a live Postgres / sqlite.

Three gates feed the reaper-start decision (any False short-
circuits to no-op):

- ``cfg`` and ``ollama_base_url`` passed to ``create_app``.
- ``cfg.store.enabled`` is True.
- Either ``cfg.store.ttl.enabled`` or
  ``cfg.store.distillation.enabled`` is True.

Default config keeps ``store.enabled = False`` — the most common
deploy path is "reaper does not start", which we cover explicitly.
"""
from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch


try:
    from fastapi import FastAPI  # noqa: F401
    HAVE_FASTAPI = True
except ImportError:
    HAVE_FASTAPI = False


def _cfg_with_store(
    *,
    enabled: bool = True,
    backend: str = "sqlite_vec",
    ttl_enabled: bool = True,
    distill_enabled: bool = True,
):
    """Build a ConsultantsConfig with the M14 sub-configs."""
    from consultants.config import ConsultantsConfig
    cfg = ConsultantsConfig()
    cfg.store.enabled = enabled
    cfg.store.backend = backend
    cfg.store.ttl.enabled = ttl_enabled
    cfg.store.distillation.enabled = distill_enabled
    cfg.store.distillation.sweep_interval_seconds = 999999.0  # never tick
    return cfg


@unittest.skipUnless(HAVE_FASTAPI, "FastAPI not installed")
class TestStoreReaperWiring(unittest.TestCase):

    def setUp(self) -> None:
        # Don't spawn the idle reaper from these tests — it would
        # add background-thread noise without changing what's
        # being verified.
        self._common_kwargs: dict[str, Any] = {
            "run_council": None,
            "start_reaper": False,
            "ollama_base_url": "http://127.0.0.1:11434",
        }

    def test_no_cfg_does_not_start_store_reaper(self) -> None:
        """Pre-M14 callers (no cfg passed) keep working with no
        reaper running — backwards-compat."""
        from consultants.server.app import create_app
        app = create_app(run_council=None, start_reaper=False)
        self.assertIsNone(getattr(app.state, "store_reaper", None))

    def test_no_ollama_base_url_does_not_start_store_reaper(self) -> None:
        """cfg present but no base_url → still skip — the distiller
        needs an upstream URL to talk to a chat model."""
        from consultants.server.app import create_app
        cfg = _cfg_with_store()
        app = create_app(
            run_council=None, start_reaper=False,
            cfg=cfg, ollama_base_url="",
        )
        self.assertIsNone(getattr(app.state, "store_reaper", None))

    def test_store_disabled_does_not_start(self) -> None:
        """The most common deploy path — defaults give
        ``store.enabled = False`` → no reaper, no daemon thread,
        zero cost. This is the path M12 parity guarantees."""
        from consultants.server.app import create_app
        cfg = _cfg_with_store(enabled=False)
        app = create_app(cfg=cfg, **self._common_kwargs)
        self.assertIsNone(getattr(app.state, "store_reaper", None))

    def test_neither_ttl_nor_distill_enabled_does_not_start(self) -> None:
        """Pathological config — store on but TTL + distillation
        both off → nothing for the reaper to do, so don't start."""
        from consultants.server.app import create_app
        cfg = _cfg_with_store(ttl_enabled=False, distill_enabled=False)
        app = create_app(cfg=cfg, **self._common_kwargs)
        self.assertIsNone(getattr(app.state, "store_reaper", None))

    def test_memory_backend_does_not_start(self) -> None:
        """Memory backend is per-process — no cross-session sweep
        possible. Reaper exits early; app comes up unchanged."""
        from consultants.server.app import create_app
        cfg = _cfg_with_store(backend="memory")
        app = create_app(cfg=cfg, **self._common_kwargs)
        self.assertIsNone(getattr(app.state, "store_reaper", None))

    def test_provider_load_failure_logs_and_continues(self) -> None:
        """When the backend loader returns None (e.g. psycopg
        missing), the reaper is not started — the app still comes
        up healthy."""
        from consultants.server.app import create_app
        cfg = _cfg_with_store(backend="pgvector")
        with patch(
            "consultants.engine.store._load_pgvector",
            return_value=None,
        ):
            app = create_app(cfg=cfg, **self._common_kwargs)
        self.assertIsNone(getattr(app.state, "store_reaper", None))

    def test_full_opt_in_starts_reaper_and_stops_cleanly(self) -> None:
        """End-to-end gate: store on + ttl on + distill on +
        provider loads → reaper starts. Manual stop returns it
        cleanly to ``is_alive == False``."""
        from consultants.server.app import create_app

        # Fake the heavy bits — we only want to verify wiring, not
        # actually spin up sqlite_vec or LangGraph here.
        class _FakeProvider:
            def expire_before(self, **_kw) -> list:
                return []
            def delete_by_hashes(self, hashes) -> int:
                return 0

        class _FakeStore:
            def put(self, *a, **kw) -> None:
                pass

        provider = _FakeProvider()
        store = _FakeStore()
        cfg = _cfg_with_store(backend="sqlite_vec")
        with patch(
            "consultants.engine.store._load_sqlite_vec",
            return_value=provider,
        ), patch(
            "consultants.engine.store.make_consultants_store",
            return_value=store,
        ):
            app = create_app(cfg=cfg, **self._common_kwargs)
        reaper = getattr(app.state, "store_reaper", None)
        self.assertIsNotNone(reaper)
        self.assertTrue(reaper.is_alive)
        # Verify the wiring carried our provider + store through.
        self.assertIs(reaper._provider, provider)
        self.assertIs(reaper._store, store)
        # Stop is responsive.
        reaper.stop(timeout=2.0)
        self.assertFalse(reaper.is_alive)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
