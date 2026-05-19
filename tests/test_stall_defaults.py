"""Scaffold-shape tests for ``consultants/engine/stall_defaults.py``.

M11a-1 ships the module as an empty per-model scaffold. These
tests pin the shape so M11a-2 (the live-run + closeout commit)
can't accidentally land while leaving the scaffold half-populated
or with mismatched defaults.

Crucially the tests assert the **M12 parity guarantee**: importing
this module does NOT change the global defaults that the runtime
actually reads — those still live in
``consultants/engine/control.py``. The wiring from
``resolve_stall_thresholds`` into ``RuntimeControl`` defaults
happens in M11a-2, after measured data lands.
"""

from __future__ import annotations

import unittest

from consultants.engine import stall_defaults as sd


class TestStallDefaultsScaffoldShape(unittest.TestCase):
    """The module's public surface is the same in M11a-1 (empty) as
    it will be in M11a-2 (populated). These tests pin the shape.
    """

    def test_provenance_constants_exist(self) -> None:
        # Strings; non-empty for the date + version stamps.
        self.assertIsInstance(sd.RECOMMENDED_AS_OF, str)
        self.assertGreater(len(sd.RECOMMENDED_AS_OF), 0)
        self.assertIsInstance(sd.RECOMMENDED_SUITE_VERSION, str)
        self.assertGreater(len(sd.RECOMMENDED_SUITE_VERSION), 0)
        # Hash prefix MAY be empty in M11a-1 (scaffold); just check
        # the attribute exists with the right type.
        self.assertIsInstance(sd.RECOMMENDED_SUITE_HASH_PREFIX, str)

    def test_default_stall_matches_control_constants(self) -> None:
        """The M12 parity guarantee.

        ``RECOMMENDED_DEFAULT_STALL`` must equal the global defaults
        in ``control.py`` so wiring ``resolve_stall_thresholds`` in
        place of the constants is a no-op for unmeasured models.
        """
        from consultants.engine import control

        self.assertEqual(
            sd.RECOMMENDED_DEFAULT_STALL.stall_threshold_s,
            control.DEFAULT_STALL_THRESHOLD_S,
        )
        self.assertEqual(
            sd.RECOMMENDED_DEFAULT_STALL.hard_cap_s,
            control.PER_LANE_HARD_S_DEFAULT,
        )

    def test_default_stall_values_are_300_3600(self) -> None:
        """Belt-and-braces: the scaffold values are also exactly
        what they were when M3 landed. If control.py drifts and
        someone updates that in isolation, this test still flags
        the stall-side oversight.
        """
        self.assertEqual(sd.RECOMMENDED_DEFAULT_STALL.stall_threshold_s, 300.0)
        self.assertEqual(sd.RECOMMENDED_DEFAULT_STALL.hard_cap_s, 3600.0)

    def test_per_model_map_populated_from_m11a2_tier1(self) -> None:
        """M11a-2 (Tier 1 live run, 2026-05-17) populated the
        per-model map with seven entries — the M11b-mlang cohort
        plus gemini-3-flash-preview, matching the table in
        ``docs/consultants-skill-eval-baselines.md``.
        """
        expected_models = {
            "glm-5.1:cloud",
            "kimi-k2.6:cloud",
            "gemma4:31b-cloud",
            "qwen3-coder-next:cloud",
            "deepseek-v4-pro:cloud",
            "deepseek-v4-flash:cloud",
            "gemini-3-flash-preview:cloud",
        }
        self.assertEqual(
            set(sd.RECOMMENDED_STALL_THRESHOLDS_BY_MODEL.keys()),
            expected_models,
        )

    def test_kimi_threshold_higher_than_global_default(self) -> None:
        """The headline finding of the M11a-2 Tier-1 live run: kimi
        needs ``stall_threshold_s > 300`` (the global default).
        This test locks that empirical fact — if a future run
        gives kimi a different default, the regression flag should
        fire so the operator confirms intentional drift."""
        kimi = sd.RECOMMENDED_STALL_THRESHOLDS_BY_MODEL["kimi-k2.6:cloud"]
        self.assertGreater(
            kimi.stall_threshold_s,
            sd.RECOMMENDED_DEFAULT_STALL.stall_threshold_s,
        )
        # Locked exact value from the M11a-2 closeout.
        self.assertEqual(kimi.stall_threshold_s, 390.0)
        self.assertEqual(kimi.hard_cap_s, 540.0)

    def test_deepseek_flash_hard_cap_above_300(self) -> None:
        """Second headline finding: deepseek-v4-flash's p99 wall
        (256 s) pushed its hard_cap_s above the 300 s floor."""
        ds = sd.RECOMMENDED_STALL_THRESHOLDS_BY_MODEL[
            "deepseek-v4-flash:cloud"
        ]
        self.assertEqual(ds.stall_threshold_s, 210.0)
        self.assertEqual(ds.hard_cap_s, 780.0)

    def test_provenance_stamps_populated(self) -> None:
        """M11a-2 fills the suite-hash prefix the scaffold left
        empty + bumps the AS_OF stamp to the live-run date."""
        self.assertEqual(sd.RECOMMENDED_SUITE_HASH_PREFIX, "c8306c62")
        self.assertEqual(sd.RECOMMENDED_AS_OF, "2026-05-17")
        self.assertEqual(sd.RECOMMENDED_TIER_MIX, "tier1-only")


class TestStallDefaultsResolver(unittest.TestCase):
    """``resolve_stall_thresholds`` is a pure function. In M11a-1
    every input returns the default.
    """

    def test_none_returns_default(self) -> None:
        self.assertEqual(
            sd.resolve_stall_thresholds(None),
            sd.RECOMMENDED_DEFAULT_STALL,
        )

    def test_empty_string_returns_default(self) -> None:
        self.assertEqual(
            sd.resolve_stall_thresholds(""),
            sd.RECOMMENDED_DEFAULT_STALL,
        )

    def test_unknown_model_returns_default(self) -> None:
        """Any model NOT in
        ``RECOMMENDED_STALL_THRESHOLDS_BY_MODEL`` falls through
        to the global default. M11a-2 populated the in-cohort
        models; everything else still falls through.
        """
        for model in (
            "made-up-model:v0",
            "claude-3-opus:cloud",
            "gpt-5:cloud",
            "out-of-cohort:v1",
        ):
            with self.subTest(model=model):
                self.assertEqual(
                    sd.resolve_stall_thresholds(model),
                    sd.RECOMMENDED_DEFAULT_STALL,
                )

    def test_known_model_returns_per_model_override(self) -> None:
        """Inverse of the unknown-model test: in-cohort models
        get their per-model entry from the map, NOT the global
        default."""
        kimi = sd.resolve_stall_thresholds("kimi-k2.6:cloud")
        self.assertIsNot(kimi, sd.RECOMMENDED_DEFAULT_STALL)
        self.assertEqual(kimi.stall_threshold_s, 390.0)

        glm = sd.resolve_stall_thresholds("glm-5.1:cloud")
        self.assertIsNot(glm, sd.RECOMMENDED_DEFAULT_STALL)
        self.assertEqual(glm.stall_threshold_s, 90.0)

    def test_resolver_returns_same_object_when_no_override(self) -> None:
        """Identity is intentional — the resolver returns the
        shared module-level default rather than a fresh copy when
        no override exists. Frozen dataclass means this is safe;
        callers can't mutate it.
        """
        self.assertIs(
            sd.resolve_stall_thresholds("never-seen-this-model:v0"),
            sd.RECOMMENDED_DEFAULT_STALL,
        )


class TestStallThresholdsDataclass(unittest.TestCase):
    """``StallThresholds`` is a frozen dataclass; pin the contract."""

    def test_constructs_from_kwargs(self) -> None:
        st = sd.StallThresholds(stall_threshold_s=120.0, hard_cap_s=900.0)
        self.assertEqual(st.stall_threshold_s, 120.0)
        self.assertEqual(st.hard_cap_s, 900.0)

    def test_frozen(self) -> None:
        st = sd.StallThresholds(stall_threshold_s=30.0, hard_cap_s=300.0)
        with self.assertRaises(Exception):
            # FrozenInstanceError lives in dataclasses, but
            # different Python versions raise different concrete
            # types; assert it raises *something*.
            st.stall_threshold_s = 60.0  # type: ignore[misc]

    def test_equal_when_fields_match(self) -> None:
        a = sd.StallThresholds(stall_threshold_s=30.0, hard_cap_s=300.0)
        b = sd.StallThresholds(stall_threshold_s=30.0, hard_cap_s=300.0)
        self.assertEqual(a, b)


class TestStallDefaultsPublicSurface(unittest.TestCase):
    """``__all__`` is the documented contract — drift on either
    side is a behavior change.
    """

    def test_all_exports_present(self) -> None:
        expected = {
            "StallThresholds",
            "RECOMMENDED_AS_OF",
            "RECOMMENDED_SUITE_VERSION",
            "RECOMMENDED_SUITE_HASH_PREFIX",
            "RECOMMENDED_TIER_MIX",
            "RECOMMENDED_DEFAULT_STALL",
            "RECOMMENDED_STALL_THRESHOLDS_BY_MODEL",
            "resolve_stall_thresholds",
        }
        self.assertEqual(set(sd.__all__), expected)

    def test_no_extra_exports(self) -> None:
        # No private names leaking through __all__.
        for name in sd.__all__:
            self.assertFalse(
                name.startswith("_"),
                f"{name} should not appear in __all__",
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
