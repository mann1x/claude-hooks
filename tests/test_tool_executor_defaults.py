"""Scaffold-shape tests for
``consultants/engine/tool_executor_defaults.py``.

M11c-1 ships the module as an empty scaffold. These tests pin the
shape so M11c-2 (the live-run + closeout commit) can't accidentally
land while leaving the scaffold half-populated or with mismatched
defaults.

Crucially: assertions hold the **M12 parity guarantee** — importing
this module does NOT change the existing
``DEFAULT_ENABLED_BY_ROLE["tool_executor"]=False`` from
``consultants/config.py``. The engine wiring (if M11c-2 flips
``RECOMMENDED_DEFAULT_ON=True``) happens in a separate commit
that reads this module's constants and updates config defaults
explicitly.
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

    def test_recommended_default_on_true_after_103_resolved(self) -> None:
        """M11c-5 outcome (2026-05-17): both parts of the gate
        cleared — rubric pass at 87.5% / 5.00 (M11c-2), AND task
        #103 (x-tier proper composition) resolved by the M11c-3
        engine refactor (commit ``e62fd85``: per-lane
        ``parent_lane_idx`` threading + the
        ``_fanout_after_tool_executor`` conditional edge). The
        default-on bit flipped to True; this test guards against
        a regression back to False without re-evaluating the
        two-part gate."""
        self.assertEqual(ted.RECOMMENDED_DEFAULT_ON, True)


class TestM11c5RuntimeDefault(unittest.TestCase):
    """M11c-5 (2026-05-17) atomic flip of the runtime default.

    The M12 parity guarantee from M11c-1 ("importing
    ``tool_executor_defaults`` doesn't change runtime behavior")
    no longer applies — M11c-5 deliberately changes the runtime
    default. These tests pin the new state so a future commit
    that quietly reverts the flip fails CI loudly.
    """

    def test_default_enabled_by_role_is_true(self) -> None:
        """``DEFAULT_ENABLED_BY_ROLE["tool_executor"]`` is now
        ``True`` — M11c-5 atomic flip. A fresh ConsultantsConfig
        gets the tool_executor lane wired by default."""
        from consultants.config import DEFAULT_ENABLED_BY_ROLE

        self.assertEqual(
            DEFAULT_ENABLED_BY_ROLE["tool_executor"], True,
        )

    def test_default_model_by_role_unchanged(self) -> None:
        """``DEFAULT_MODEL_BY_ROLE["tool_executor"]`` stays
        ``"gemma4:31b-cloud"`` — the M11c-2 bench confirmed the
        M6 fallback was the right pick; no change here."""
        from consultants.config import DEFAULT_MODEL_BY_ROLE

        self.assertEqual(
            DEFAULT_MODEL_BY_ROLE["tool_executor"], "gemma4:31b-cloud",
        )

    def test_scaffold_default_on_matches_runtime_default(self) -> None:
        """``RECOMMENDED_DEFAULT_ON`` and
        ``DEFAULT_ENABLED_BY_ROLE["tool_executor"]`` must agree
        bit-for-bit. M11c-1 shipped both at False; M11c-2 left
        them both at False (gate part 2 unresolved); M11c-5
        flipped them both to True atomically. A future commit
        that nudges one without the other (e.g. to disable the
        role again) MUST touch both fields — this test guards
        against drift."""
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
