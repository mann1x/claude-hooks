"""M14 follow-up — embedder config threading through the store factory.

Without ``store.embedder`` + ``store.embedder_options`` the consultants
pgvector / sqlite_vec backends fall back to ``NullEmbedder`` and
every store call raises. This file pins:

1. The new ``embedder`` + ``embedder_options`` fields on
   ``StoreConfig`` exist with the expected defaults.
2. The TOML merge layer picks them up from ``[store]`` and
   ``[store.embedder_options]`` cleanly.
3. The TOML render emits them so ``save_config`` →
   re-``load_config`` round-trips.
4. ``_load_pgvector`` / ``_load_sqlite_vec`` thread the config into
   the provider's ``options`` dict so the provider can actually
   embed.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from consultants.config import (
    ConsultantsConfig,
    StoreConfig,
    _merge_layer,
    _render,
)


class TestStoreEmbedderConfigFields(unittest.TestCase):

    def test_embedder_default_is_none(self) -> None:
        # M14 follow-up: a fresh StoreConfig has no embedder
        # configured — the provider will fail loudly on first
        # store call (NullEmbedder raises). Operator must wire
        # embedder explicitly OR install.py copies from the main
        # recall pipeline config.
        cfg = StoreConfig()
        self.assertIsNone(cfg.embedder)
        self.assertEqual(cfg.embedder_options, {})

    def test_embedder_can_be_set(self) -> None:
        cfg = StoreConfig()
        cfg.embedder = "llamafile"
        cfg.embedder_options = {
            "url": "http://127.0.0.1:38092/embedding",
            "model": "qwen3-embedding:0.6b",
            "timeout": 30.0,
            "num_ctx": 16384,
            "daemon_ensure": True,
        }
        self.assertEqual(cfg.embedder, "llamafile")
        self.assertEqual(
            cfg.embedder_options["model"], "qwen3-embedding:0.6b",
        )


class TestStoreEmbedderTomlMerge(unittest.TestCase):

    def test_merge_reads_embedder_from_store_block(self) -> None:
        cfg = ConsultantsConfig()
        raw = {
            "store": {
                "enabled": True,
                "backend": "pgvector",
                "embedder": "llamafile",
                "embedder_options": {
                    "url": "http://127.0.0.1:38092/embedding",
                    "model": "qwen3-embedding:0.6b",
                    "timeout": 30.0,
                },
            },
        }
        out = _merge_layer(cfg, raw)
        self.assertEqual(out.store.embedder, "llamafile")
        self.assertEqual(
            out.store.embedder_options["url"],
            "http://127.0.0.1:38092/embedding",
        )

    def test_merge_blank_embedder_string_becomes_none(self) -> None:
        # Defensive: a TOML with ``embedder = ""`` should leave
        # the field None rather than wire an empty-string embedder
        # name that ``make_embedder`` doesn't know.
        cfg = ConsultantsConfig()
        raw = {"store": {"embedder": "   "}}
        out = _merge_layer(cfg, raw)
        self.assertIsNone(out.store.embedder)

    def test_merge_non_dict_options_ignored(self) -> None:
        # Hand-edited TOML that sets ``embedder_options`` to a string
        # by mistake → leave the field at its default rather than
        # crash.
        cfg = ConsultantsConfig()
        raw = {"store": {"embedder_options": "not a dict"}}
        out = _merge_layer(cfg, raw)
        self.assertEqual(out.store.embedder_options, {})


class TestStoreEmbedderTomlRender(unittest.TestCase):

    def test_render_includes_embedder_and_options(self) -> None:
        cfg = ConsultantsConfig()
        cfg.store.embedder = "llamafile"
        cfg.store.embedder_options = {
            "url": "http://127.0.0.1:38092/embedding",
            "model": "qwen3-embedding:0.6b",
            "daemon_ensure": True,
        }
        toml = _render(cfg)
        self.assertIn('embedder = "llamafile"', toml)
        self.assertIn("[store.embedder_options]", toml)
        self.assertIn(
            'url = "http://127.0.0.1:38092/embedding"', toml,
        )
        self.assertIn('model = "qwen3-embedding:0.6b"', toml)
        self.assertIn("daemon_ensure = true", toml)

    def test_render_emits_commented_template_when_no_embedder(self) -> None:
        # No embedder configured → render emits a commented-out
        # template block so the operator sees the right shape.
        cfg = ConsultantsConfig()
        toml = _render(cfg)
        self.assertIn("# embedder = ", toml)
        self.assertIn("# [store.embedder_options]", toml)

    def test_render_round_trip_via_tomllib(self) -> None:
        # End-to-end round-trip: render → parse → merge → fields
        # match.
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib  # type: ignore[no-redef]
        cfg = ConsultantsConfig()
        cfg.store.enabled = True
        cfg.store.backend = "pgvector"
        cfg.store.pgvector_dsn = "postgresql://u:p@h/db"
        cfg.store.pgvector_table = "consultants_store"
        cfg.store.embedder = "llamafile"
        cfg.store.embedder_options = {
            "url": "http://127.0.0.1:38092/embedding",
            "model": "qwen3-embedding:0.6b",
            "timeout": 30.0,
            "num_ctx": 16384,
            "daemon_ensure": True,
        }
        toml = _render(cfg)
        parsed = tomllib.loads(toml)
        self.assertEqual(parsed["store"]["embedder"], "llamafile")
        self.assertEqual(
            parsed["store"]["embedder_options"]["model"],
            "qwen3-embedding:0.6b",
        )
        # Merge the parsed dict back through the canonical path and
        # confirm the round-trip is symmetric.
        cfg2 = _merge_layer(ConsultantsConfig(), parsed)
        self.assertEqual(cfg2.store.embedder, cfg.store.embedder)
        self.assertEqual(
            cfg2.store.embedder_options, cfg.store.embedder_options,
        )


class TestProviderLoaderThreadsEmbedder(unittest.TestCase):

    def test_load_pgvector_passes_embedder_to_provider(self) -> None:
        # The factory must thread ``embedder`` + ``embedder_options``
        # into the provider's ``options`` dict — otherwise the
        # provider falls back to NullEmbedder.
        from consultants.engine.store import _load_pgvector

        # Stub the provider so we can capture the options dict
        # without needing psycopg / a live PG.
        captured: dict = {}

        class _StubProvider:
            def __init__(self, server, options=None):
                captured["server"] = server
                captured["options"] = dict(options or {})

        import claude_hooks.providers.pgvector as pg_mod
        original = pg_mod.PgvectorProvider
        try:
            pg_mod.PgvectorProvider = _StubProvider  # type: ignore[misc]
            cfg = StoreConfig(
                enabled=True,
                backend="pgvector",
                pgvector_dsn="postgresql://u:p@h/db",
                pgvector_table="consultants_store",
                embedder="llamafile",
                embedder_options={
                    "url": "http://127.0.0.1:38092/embedding",
                    "model": "qwen3-embedding:0.6b",
                },
            )
            provider = _load_pgvector(cfg)
            self.assertIsNotNone(provider)
        finally:
            pg_mod.PgvectorProvider = original  # type: ignore[misc]

        opts = captured["options"]
        self.assertEqual(opts["dsn"], "postgresql://u:p@h/db")
        self.assertEqual(opts["table"], "consultants_store")
        self.assertEqual(opts["embedder"], "llamafile")
        self.assertEqual(
            opts["embedder_options"]["model"], "qwen3-embedding:0.6b",
        )

    def test_load_sqlite_vec_passes_embedder_to_provider(self) -> None:
        from consultants.engine.store import _load_sqlite_vec

        captured: dict = {}

        class _StubProvider:
            def __init__(self, server, options=None):
                captured["server"] = server
                captured["options"] = dict(options or {})

        import claude_hooks.providers.sqlite_vec as sv_mod
        original = sv_mod.SqliteVecProvider
        try:
            sv_mod.SqliteVecProvider = _StubProvider  # type: ignore[misc]
            cfg = StoreConfig(
                enabled=True,
                backend="sqlite_vec",
                sqlite_vec_path="/tmp/test.db",
                embedder="ollama",
                embedder_options={
                    "url": "http://127.0.0.1:11434/api/embeddings",
                    "model": "nomic-embed-text",
                },
            )
            provider = _load_sqlite_vec(cfg)
            self.assertIsNotNone(provider)
        finally:
            sv_mod.SqliteVecProvider = original  # type: ignore[misc]

        opts = captured["options"]
        self.assertEqual(opts["db_path"], "/tmp/test.db")
        self.assertEqual(opts["embedder"], "ollama")
        self.assertEqual(
            opts["embedder_options"]["model"], "nomic-embed-text",
        )

    def test_load_pgvector_without_embedder_omits_keys(self) -> None:
        # If embedder isn't configured, the factory must NOT inject
        # an ``"embedder": null`` key — the provider's own default
        # (``"null"``) is reached cleanly.
        from consultants.engine.store import _load_pgvector

        captured: dict = {}

        class _StubProvider:
            def __init__(self, server, options=None):
                captured["options"] = dict(options or {})

        import claude_hooks.providers.pgvector as pg_mod
        original = pg_mod.PgvectorProvider
        try:
            pg_mod.PgvectorProvider = _StubProvider  # type: ignore[misc]
            cfg = StoreConfig(
                enabled=True,
                backend="pgvector",
                pgvector_dsn="postgresql://u:p@h/db",
            )
            _load_pgvector(cfg)
        finally:
            pg_mod.PgvectorProvider = original  # type: ignore[misc]

        self.assertNotIn("embedder", captured["options"])
        self.assertNotIn("embedder_options", captured["options"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
