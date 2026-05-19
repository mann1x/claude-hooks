"""Tests for
``consultants/engine/tool_executor_defaults.py``.

Pins the module's shape + the current runtime-default contract.
Flips of ``RECOMMENDED_DEFAULT_ON`` (and the paired
``DEFAULT_ENABLED_BY_ROLE["tool_executor"]`` in
``consultants/config.py``) are deliberate, recorded in a
benchmark file under ``benchmarks/consultants/results/...``,
and require BOTH constants to move atomically — the drift test
below catches accidents.

Current state (2026-05-18): both default to ``False`` after the
M14 first-real-ask tool_executor on/off A/B showed the role
net-negative on grep-shaped questions. The M11c-2 bench result
remains the recommendation when operators opt in.
"""

from __future__ import annotations

import unittest

from consultants.engine import tool_executor_defaults as ted


class TestToolExecutorDefaultsScaffoldShape(unittest.TestCase):

    def test_provenance_constants_exist(self) -> None:
        self.assertIsInstance(ted.RECOMMENDED_AS_OF, str)
        self.assertGreater(len(ted.RECOMMENDED_AS_OF), 0)
        self.assertIsInstance(ted.RECOMMENDED_SUITE_VERSION, str)
        self.assertGreater(len(ted.RECOMMENDED_SUITE_VERSION), 0)
        # Hash prefix is populated in M11c-2 (was empty in M11c-1);
        # check it's an 8-hex-char string matching the suite hash
        # prefix the bench writes to its results metadata.
        self.assertIsInstance(ted.RECOMMENDED_SUITE_HASH_PREFIX, str)
        # Empty is still tolerated for a pre-live scaffold rev, but
        # when present it MUST be 8 lowercase hex chars (matches
        # ``SuiteManifest.suite_hash[:8]``).
        if ted.RECOMMENDED_SUITE_HASH_PREFIX:
            self.assertEqual(len(ted.RECOMMENDED_SUITE_HASH_PREFIX), 8)
            self.assertRegex(
                ted.RECOMMENDED_SUITE_HASH_PREFIX, r"^[0-9a-f]{8}$",
            )

    def test_recommended_model_populated_by_m11c2(self) -> None:
        """M11c-2 (2026-05-17): the live bench picked
        ``gemma4:31b-cloud`` (87.5% pass rate / 5.00 avg quality /
        4.9 s avg wall — won every tiebreaker among the four
        models tied on pass rate). The constant must surface a
        non-empty value so the config layer can route the role
        to the bench-validated model when enabled.
        """
        self.assertNotEqual(
            ted.RECOMMENDED_TOOL_EXECUTOR_MODEL, "",
            "M11c-2 populates this — empty string means the scaffold "
            "regressed",
        )

    def test_recommended_default_on_is_false_after_m14_ab(self) -> None:
        """Flip history:

        * M11c-1 (2026-05-17): scaffold False.
        * M11c-5 (2026-05-17): True after the two-part gate
          cleared — M11c-2 bench rubric pass + task #103
          (x-tier proper composition) via M11c-3 refactor.
        * 2026-05-18: back to False. The M14 first-real-ask
          tool_executor on/off A/B
          (``benchmarks/consultants/results/2026-05-18/tool-executor-ab/``)
          showed the role costing +12 minutes wall + 43% tokens
          AND identifying fewer edge cases on a grep-shaped
          question. The M11c-2 bench result still validates the
          role for tool-heavy reasoning corpora; the flip-back
          says the bench corpus is not what most operator
          questions look like.

        This test guards against an accidental re-flip without
        a fresh A/B providing the inverse evidence.
        """
        self.assertEqual(ted.RECOMMENDED_DEFAULT_ON, False)


class TestRuntimeDefault(unittest.TestCase):
    """Pin the current runtime default. Flips happen deliberately
    (with paired updates to ``RECOMMENDED_DEFAULT_ON`` AND a
    benchmark record under
    ``benchmarks/consultants/results/...``); this test fails
    CI when one half of the flip is missed.
    """

    def test_default_enabled_by_role_is_false(self) -> None:
        """``DEFAULT_ENABLED_BY_ROLE["tool_executor"]`` is now
        ``False`` (2026-05-18 flip-back). A fresh
        ConsultantsConfig leaves tool_executor disabled; the
        researcher runs its own inline tool-loop. Operators who
        want the specialist role opt in via TOML:
        ``[role.tool_executor]  enabled = true``."""
        from consultants.config import DEFAULT_ENABLED_BY_ROLE

        self.assertEqual(
            DEFAULT_ENABLED_BY_ROLE["tool_executor"], False,
        )

    def test_default_model_by_role_unchanged(self) -> None:
        """``DEFAULT_MODEL_BY_ROLE["tool_executor"]`` stays
        ``"gemma4:31b-cloud"`` — the M11c-2 bench result is
        still the recommended model when operators opt in. The
        default-off flip didn't invalidate the bench, only the
        baseline-on conclusion."""
        from consultants.config import DEFAULT_MODEL_BY_ROLE

        self.assertEqual(
            DEFAULT_MODEL_BY_ROLE["tool_executor"], "gemma4:31b-cloud",
        )

    def test_scaffold_default_on_matches_runtime_default(self) -> None:
        """``RECOMMENDED_DEFAULT_ON`` and
        ``DEFAULT_ENABLED_BY_ROLE["tool_executor"]`` must agree
        bit-for-bit. Any flip MUST touch both fields atomically —
        this test fails when one is nudged without the other."""
        from consultants.config import DEFAULT_ENABLED_BY_ROLE

        self.assertEqual(
            ted.RECOMMENDED_DEFAULT_ON,
            DEFAULT_ENABLED_BY_ROLE["tool_executor"],
        )


class TestToolExecutorDefaultsPublicSurface(unittest.TestCase):
    """``__all__`` is the documented contract — drift on either
    side is a behavior change.
    """

    def test_all_exports_present(self) -> None:
        expected = {
            "RECOMMENDED_AS_OF",
            "RECOMMENDED_SUITE_VERSION",
            "RECOMMENDED_SUITE_HASH_PREFIX",
            "RECOMMENDED_TOOL_EXECUTOR_MODEL",
            "RECOMMENDED_DEFAULT_ON",
        }
        self.assertEqual(set(ted.__all__), expected)

    def test_no_private_exports(self) -> None:
        for name in ted.__all__:
            self.assertFalse(
                name.startswith("_"),
                f"{name} should not appear in __all__",
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
