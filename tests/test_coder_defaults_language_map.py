"""Task #111 — defaults module tests for the per-language coder
routing map. Pure-function level: extension -> language id, route
table shape, single-source resolver.

No I/O, no graph machinery — just the constants + helpers in
``consultants.engine.coder_defaults``.
"""
from __future__ import annotations

import unittest

from consultants.engine.coder_defaults import (
    LANGUAGE_BY_EXTENSION,
    QUALIFYING_MODELS_2026_05_17_MLANG,
    RECOMMENDED_AS_OF,
    RECOMMENDED_CODER_DEFAULT_ROUTE,
    RECOMMENDED_CODER_MODEL,
    RECOMMENDED_CODER_ROUTES_BY_LANGUAGE,
    RECOMMENDED_SUITE_HASH_PREFIX,
    RECOMMENDED_SUITE_VERSION,
    language_from_path,
    resolve_coder_route,
)
from consultants.engine.state_v2 import CoderLanguageRoute


class TestLanguageFromPath(unittest.TestCase):
    """``language_from_path`` is the single source for extension →
    language id. Coverage walks every in-cohort extension + the
    edge cases the resolver depends on."""

    def test_in_cohort_extensions(self):
        cases = [
            ("solution.c", "c"),
            ("solution.h", "c"),
            ("solution.cpp", "cpp"),
            ("solution.cxx", "cpp"),
            ("solution.cc", "cpp"),
            ("solution.hpp", "cpp"),
            ("solution.hh", "cpp"),
            ("solution.cs", "csharp"),
            ("solution.go", "go"),
            ("solution.py", "python"),
            ("solution.pyi", "python"),
            ("solution.rs", "rust"),
        ]
        for path, expected in cases:
            with self.subTest(path=path):
                self.assertEqual(language_from_path(path), expected)

    def test_out_of_cohort_extensions(self):
        # Out-of-cohort extensions still resolve to a language id
        # (so the resolver has a name to look up), but no per-
        # language entry exists in the seeded map — the resolver
        # falls through to the global default.
        cases = [
            ("app.ts", "typescript"),
            ("App.java", "java"),
            ("script.sh", "shell"),
        ]
        for path, expected in cases:
            with self.subTest(path=path):
                self.assertEqual(language_from_path(path), expected)

    def test_case_insensitive_extension(self):
        self.assertEqual(language_from_path("Foo.CS"), "csharp")
        self.assertEqual(language_from_path("Bar.PY"), "python")

    def test_returns_none_on_no_extension(self):
        # Empty / no extension / unknown extension all return None
        # — the resolver routes to the global default in every case.
        self.assertIsNone(language_from_path(""))
        self.assertIsNone(language_from_path("Makefile"))
        self.assertIsNone(language_from_path("README"))
        self.assertIsNone(language_from_path("solution.xyz"))

    def test_strips_whitespace(self):
        self.assertEqual(language_from_path("  solution.py  "), "python")


class TestRecommendedRoutes(unittest.TestCase):
    """Shape assertions on the recommended-routes table — catches
    accidental edits that would silently break per-language
    selection."""

    def test_six_in_cohort_languages_present(self):
        expected = {"c", "cpp", "csharp", "go", "python", "rust"}
        self.assertEqual(set(RECOMMENDED_CODER_ROUTES_BY_LANGUAGE),
                         expected)

    def test_every_route_has_primary(self):
        for lang, route in RECOMMENDED_CODER_ROUTES_BY_LANGUAGE.items():
            with self.subTest(lang=lang):
                self.assertIsInstance(route, CoderLanguageRoute)
                self.assertTrue(route.primary.strip(),
                                f"{lang} missing primary")

    def test_every_route_has_fallback(self):
        # The recommended table always provides a fallback — empty
        # is allowed in the data model but not in our seeded
        # defaults (we want failover everywhere out of the box).
        for lang, route in RECOMMENDED_CODER_ROUTES_BY_LANGUAGE.items():
            with self.subTest(lang=lang):
                self.assertTrue(route.fallback.strip(),
                                f"{lang} missing fallback")

    def test_user_overrides_present(self):
        # Two explicit user overrides (2026-05-17) that differ from
        # pure table-winner logic:
        # - csharp.primary = deepseek-v4-pro:cloud (top avgQ, was
        #   ambiguous on alg)
        # - python.primary = glm-5.1:cloud (user preference; tied
        #   for top avgQ with kimi)
        self.assertEqual(
            RECOMMENDED_CODER_ROUTES_BY_LANGUAGE["csharp"].primary,
            "deepseek-v4-pro:cloud",
        )
        self.assertEqual(
            RECOMMENDED_CODER_ROUTES_BY_LANGUAGE["python"].primary,
            "glm-5.1:cloud",
        )

    def test_global_default_route_set(self):
        self.assertIsInstance(RECOMMENDED_CODER_DEFAULT_ROUTE,
                              CoderLanguageRoute)
        self.assertEqual(
            RECOMMENDED_CODER_DEFAULT_ROUTE.primary, "glm-5.1:cloud",
        )
        self.assertEqual(
            RECOMMENDED_CODER_DEFAULT_ROUTE.fallback, "kimi-k2.6:cloud",
        )

    def test_routes_only_use_qualifying_models(self):
        # Sanity check the recommended map only references models
        # from the v1.0.1-mlang cohort — guard against typos that
        # would land an unqualified model in defaults.
        qualifying = set(QUALIFYING_MODELS_2026_05_17_MLANG)
        for lang, route in RECOMMENDED_CODER_ROUTES_BY_LANGUAGE.items():
            with self.subTest(lang=lang):
                self.assertIn(route.primary, qualifying,
                              f"{lang}.primary not in qualifying set")
                self.assertIn(route.fallback, qualifying,
                              f"{lang}.fallback not in qualifying set")

    def test_legacy_model_constant_stable(self):
        # Don't accidentally rename the legacy fallback — it's the
        # v1 single-model winner and several callers still reference
        # it by name.
        self.assertEqual(RECOMMENDED_CODER_MODEL, "glm-5.1:cloud")

    def test_provenance_stamps_current(self):
        # Date stamp + suite version + hash prefix all updated for
        # the mlang v1.0.1 baseline.
        self.assertEqual(RECOMMENDED_AS_OF, "2026-05-17")
        self.assertEqual(RECOMMENDED_SUITE_VERSION, "1.0.1-mlang")
        self.assertEqual(RECOMMENDED_SUITE_HASH_PREFIX, "ddef8095")


class TestResolveCoderRoute(unittest.TestCase):
    """Single-source resolver tests. The graph builder + config
    layer both call this; getting it wrong reroutes the entire
    coder role."""

    def setUp(self):
        self.routes = dict(RECOMMENDED_CODER_ROUTES_BY_LANGUAGE)
        self.default = RECOMMENDED_CODER_DEFAULT_ROUTE

    def test_per_language_entry_wins(self):
        r = resolve_coder_route(
            "csharp", routes_by_language=self.routes,
            default_route=self.default,
        )
        self.assertEqual(r, self.routes["csharp"])

    def test_unknown_language_falls_to_default(self):
        r = resolve_coder_route(
            "java", routes_by_language=self.routes,
            default_route=self.default,
        )
        self.assertIs(r, self.default)

    def test_none_language_falls_to_default(self):
        r = resolve_coder_route(
            None, routes_by_language=self.routes,
            default_route=self.default,
        )
        self.assertIs(r, self.default)

    def test_no_default_uses_legacy_fallback(self):
        r = resolve_coder_route(
            None, routes_by_language={},
            default_route=None, legacy_model_fallback="qwen3:cloud",
        )
        self.assertEqual(r, CoderLanguageRoute(primary="qwen3:cloud"))
        self.assertEqual(r.fallback, "")

    def test_raises_when_nothing_provided(self):
        with self.assertRaises(ValueError):
            resolve_coder_route(
                None, routes_by_language={},
                default_route=None, legacy_model_fallback="",
            )

    def test_no_fallback_on_entry_preserved(self):
        # An entry without a fallback (operator chose "no failover")
        # round-trips verbatim — the lane code decides whether to
        # fail fast.
        routes = {"python": CoderLanguageRoute(primary="glm:cloud")}
        r = resolve_coder_route(
            "python", routes_by_language=routes,
            default_route=self.default,
        )
        self.assertEqual(r.primary, "glm:cloud")
        self.assertEqual(r.fallback, "")


class TestLanguageByExtensionShape(unittest.TestCase):
    """Defensive checks on the extension map: keys lower-case,
    values lower-case slugs, no duplicates within the in-cohort
    bucket. Catches editor-noise that would silently break
    dispatch."""

    def test_keys_lower_case_dot_prefix(self):
        for ext in LANGUAGE_BY_EXTENSION:
            with self.subTest(ext=ext):
                self.assertTrue(ext.startswith("."), ext)
                self.assertEqual(ext, ext.lower(), ext)

    def test_values_lower_case_slugs(self):
        import re
        for ext, lang in LANGUAGE_BY_EXTENSION.items():
            with self.subTest(ext=ext, lang=lang):
                self.assertRegex(lang, r"^[a-z][a-z0-9_+-]*$")


if __name__ == "__main__":
    unittest.main()
