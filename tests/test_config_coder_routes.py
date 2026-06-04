"""Task #111 — config-layer tests for the per-language coder
routes (``RoleConfig.routes_by_language`` + ``default_route``):
default seeding, TOML round-trip, partial-override merge semantics,
and the three mutators (``set_coder_route`` / ``unset_coder_route`` /
``set_coder_default_route``).
"""
from __future__ import annotations

import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from consultants import config as cc
from consultants.engine.coder_defaults import (
    RECOMMENDED_CODER_DEFAULT_ROUTE,
    RECOMMENDED_CODER_ROUTES_BY_LANGUAGE,
)
from consultants.engine.state_v2 import CoderLanguageRoute


class TestDefaultRoleConfig(unittest.TestCase):
    """A fresh ConsultantsConfig must carry the seeded coder routes
    without any TOML on disk."""

    def test_coder_role_has_seeded_routes(self):
        cfg = cc.ConsultantsConfig()
        rc = cfg.roles["coder"]
        self.assertEqual(len(rc.routes_by_language), 6)
        for lang, route in RECOMMENDED_CODER_ROUTES_BY_LANGUAGE.items():
            self.assertEqual(rc.routes_by_language[lang], route,
                              f"{lang} mismatch")

    def test_coder_role_has_seeded_default_route(self):
        cfg = cc.ConsultantsConfig()
        rc = cfg.roles["coder"]
        self.assertEqual(rc.default_route, RECOMMENDED_CODER_DEFAULT_ROUTE)

    def test_other_roles_have_empty_route_fields(self):
        # routes_by_language + default_route are coder-only — other
        # roles must keep them at their dataclass defaults so the
        # config layer's per-role iteration site doesn't accidentally
        # treat them as routable.
        cfg = cc.ConsultantsConfig()
        for role in ("planner", "researcher", "critic", "synthesizer"):
            rc = cfg.roles[role]
            self.assertEqual(rc.routes_by_language, {},
                             f"{role} unexpectedly seeded routes")
            self.assertIsNone(rc.default_route,
                              f"{role} unexpectedly seeded default")

    def test_seeded_routes_are_independent_per_instance(self):
        # Defaults are deep-copied per-instance so mutating one
        # ConsultantsConfig's routes doesn't leak into another.
        cfg1 = cc.ConsultantsConfig()
        cfg2 = cc.ConsultantsConfig()
        cfg1.roles["coder"].routes_by_language["python"] = \
            CoderLanguageRoute(primary="x", fallback="y")
        self.assertNotEqual(
            cfg1.roles["coder"].routes_by_language["python"],
            cfg2.roles["coder"].routes_by_language["python"],
        )


class TestCoderResolveRoute(unittest.TestCase):
    """The single-source resolver used by graph.py + the tests."""

    def test_per_language_entry_wins(self):
        cfg = cc.ConsultantsConfig()
        r = cc.coder_resolve_route(cfg, "csharp")
        self.assertEqual(r.primary, "kimi-k2.6:cloud")

    def test_unknown_language_falls_to_default(self):
        cfg = cc.ConsultantsConfig()
        r = cc.coder_resolve_route(cfg, "java")
        self.assertEqual(r, RECOMMENDED_CODER_DEFAULT_ROUTE)

    def test_no_routes_no_default_falls_to_legacy_model(self):
        cfg = cc.ConsultantsConfig()
        cfg.roles["coder"].routes_by_language = {}
        cfg.roles["coder"].default_route = None
        cfg.roles["coder"].model = "custom-model:cloud"
        r = cc.coder_resolve_route(cfg, "python")
        self.assertEqual(r.primary, "custom-model:cloud")
        self.assertEqual(r.fallback, "")


class TestCoderUniqueModels(unittest.TestCase):
    """``coder_unique_models`` is the runner's source of truth for
    which ChatClients to pre-build."""

    def test_returns_all_distinct_models(self):
        cfg = cc.ConsultantsConfig()
        models = cc.coder_unique_models(cfg)
        # coder_med defaults: routes use kimi/pro/flash/minimax-m3;
        # default_route + legacy model add glm → 5 unique models.
        self.assertEqual(set(models),
                         {"glm-5.1:cloud", "kimi-k2.6:cloud",
                          "deepseek-v4-pro:cloud",
                          "deepseek-v4-flash:cloud",
                          "minimax-m3:cloud"})

    def test_dedups_when_legacy_model_overlaps(self):
        cfg = cc.ConsultantsConfig()
        # The legacy ``model`` field is glm-5.1:cloud, which is
        # already in the default route + python route → de-duped.
        models = cc.coder_unique_models(cfg)
        self.assertEqual(models.count("glm-5.1:cloud"), 1)


class TestTomlRoundTrip(unittest.TestCase):
    """``_render`` + ``_merge_layer`` must round-trip every route
    entry. A drop here means an operator's edit silently disappears."""

    def test_full_round_trip(self):
        cfg = cc.ConsultantsConfig()
        text = cc._render(cfg)
        raw = tomllib.loads(text)
        cfg2 = cc._merge_layer(cc.ConsultantsConfig(), raw)
        self.assertEqual(
            cfg2.roles["coder"].routes_by_language,
            cfg.roles["coder"].routes_by_language,
        )
        self.assertEqual(
            cfg2.roles["coder"].default_route,
            cfg.roles["coder"].default_route,
        )

    def test_partial_override_replaces_routes_dict(self):
        # Per design (documented in _merge_role): a TOML
        # [role.coder.routes] section REPLACES the routes dict.
        # Operators who want to add one entry while keeping the
        # rest re-emit them all (the CLI does this via the
        # set_coder_route mutator).
        raw = {
            "role": {"coder": {"routes": {
                "python": {"primary": "qwen3:cloud",
                            "fallback": "kimi:cloud"},
            }}},
        }
        cfg = cc._merge_layer(cc.ConsultantsConfig(), raw)
        self.assertEqual(len(cfg.roles["coder"].routes_by_language), 1)
        self.assertEqual(
            cfg.roles["coder"].routes_by_language["python"].primary,
            "qwen3:cloud",
        )

    def test_default_route_override(self):
        raw = {"role": {"coder": {"default_route": {
            "primary": "kimi:cloud", "fallback": "glm:cloud",
        }}}}
        cfg = cc._merge_layer(cc.ConsultantsConfig(), raw)
        self.assertEqual(cfg.roles["coder"].default_route.primary,
                         "kimi:cloud")
        self.assertEqual(cfg.roles["coder"].default_route.fallback,
                         "glm:cloud")

    def test_empty_default_route_clears(self):
        raw = {"role": {"coder": {"default_route": {}}}}
        cfg = cc._merge_layer(cc.ConsultantsConfig(), raw)
        self.assertIsNone(cfg.roles["coder"].default_route)

    def test_default_route_without_primary_clears(self):
        # _coerce_route returns None when primary is missing; the
        # merger then clears default_route. Catches malformed TOML
        # without crashing.
        raw = {"role": {"coder": {"default_route": {
            "fallback": "kimi:cloud",  # no primary
        }}}}
        cfg = cc._merge_layer(cc.ConsultantsConfig(), raw)
        self.assertIsNone(cfg.roles["coder"].default_route)


class TestCoerceRoute(unittest.TestCase):
    """Sub-helper used by the TOML merger. Tests round here are the
    cheapest catch for "operator passed garbage TOML"."""

    def test_minimal_valid(self):
        r = cc._coerce_route({"primary": "x"})
        self.assertEqual(r, CoderLanguageRoute(primary="x", fallback=""))

    def test_with_fallback(self):
        r = cc._coerce_route({"primary": "x", "fallback": "y"})
        self.assertEqual(r, CoderLanguageRoute(primary="x", fallback="y"))

    def test_missing_primary_returns_none(self):
        self.assertIsNone(cc._coerce_route({"fallback": "y"}))
        self.assertIsNone(cc._coerce_route({"primary": ""}))
        self.assertIsNone(cc._coerce_route({"primary": "   "}))

    def test_non_dict_returns_none(self):
        for v in (None, "x", 42, [], ["x"]):
            with self.subTest(v=v):
                self.assertIsNone(cc._coerce_route(v))

    def test_strips_whitespace(self):
        r = cc._coerce_route({"primary": "  x  ", "fallback": "  y  "})
        self.assertEqual(r.primary, "x")
        self.assertEqual(r.fallback, "y")


class TestMutators(unittest.TestCase):
    """The three new public mutators (``set_coder_route`` /
    ``unset_coder_route`` / ``set_coder_default_route``) persist via
    ``save_config``. We use a temp HOME so the mutators write into
    an isolated file."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._home = Path(self._tmp.name)
        # Stub user_config_path to write into the temp dir.
        self._patcher = mock.patch.object(
            cc, "user_config_path",
            return_value=self._home / "consultants.toml",
        )
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_set_coder_route_creates_new_entry(self):
        cfg = cc.set_coder_route("typescript",
                                  primary="qwen3:cloud",
                                  fallback="kimi:cloud")
        self.assertEqual(
            cfg.roles["coder"].routes_by_language["typescript"],
            CoderLanguageRoute(primary="qwen3:cloud", fallback="kimi:cloud"),
        )
        # Round-trips to disk
        reloaded = cc.load_config(None)
        self.assertEqual(
            reloaded.roles["coder"].routes_by_language["typescript"].primary,
            "qwen3:cloud",
        )

    def test_set_coder_route_updates_primary_keeps_fallback(self):
        cc.set_coder_route("python",
                            primary="glm-5.1:cloud")  # no --fallback
        cfg = cc.load_config(None)
        route = cfg.roles["coder"].routes_by_language["python"]
        self.assertEqual(route.primary, "glm-5.1:cloud")
        # Original fallback preserved — we replaced primary only.
        # coder_med default python fallback is deepseek-v4-flash.
        self.assertEqual(route.fallback, "deepseek-v4-flash:cloud")

    def test_set_coder_route_clear_fallback_explicit_empty(self):
        # fallback="" is the explicit-clear contract.
        cc.set_coder_route("python", fallback="")
        cfg = cc.load_config(None)
        self.assertEqual(
            cfg.roles["coder"].routes_by_language["python"].fallback, "",
        )

    def test_set_coder_route_new_entry_requires_primary(self):
        with self.assertRaises(ValueError) as cm:
            cc.set_coder_route("newlang", primary=None, fallback="x:cloud")
        self.assertIn("--primary is required", str(cm.exception))

    def test_unset_coder_route(self):
        cc.unset_coder_route("python")
        cfg = cc.load_config(None)
        self.assertNotIn("python", cfg.roles["coder"].routes_by_language)

    def test_unset_coder_route_idempotent(self):
        cc.unset_coder_route("not-a-real-lang")  # no error
        cc.unset_coder_route("python")
        cc.unset_coder_route("python")  # second call is a no-op
        # Survives the round-trip
        cfg = cc.load_config(None)
        self.assertNotIn("python", cfg.roles["coder"].routes_by_language)

    def test_set_coder_default_route(self):
        cc.set_coder_default_route(primary="kimi:cloud",
                                    fallback="glm:cloud")
        cfg = cc.load_config(None)
        self.assertEqual(cfg.roles["coder"].default_route.primary,
                         "kimi:cloud")
        self.assertEqual(cfg.roles["coder"].default_route.fallback,
                         "glm:cloud")

    def test_set_coder_default_route_partial_update(self):
        cc.set_coder_default_route(fallback="x:cloud")  # primary unchanged
        cfg = cc.load_config(None)
        # Default primary was glm-5.1:cloud from the seed.
        self.assertEqual(cfg.roles["coder"].default_route.primary,
                         "glm-5.1:cloud")
        self.assertEqual(cfg.roles["coder"].default_route.fallback,
                         "x:cloud")

    def test_set_coder_route_validates_language_slug(self):
        for bad in ("NOT A LANG", "py thon", "py/thon"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    cc.set_coder_route(bad, primary="x:cloud")


if __name__ == "__main__":
    unittest.main()
