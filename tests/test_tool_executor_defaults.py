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

    def test_recommended_default_on_still_false_pending_103(self) -> None:
        """M11c-2 outcome: rubric clears (87.5% / 5.00), but task
        #103 (x-tier proper composition) is NOT yet resolved, so
        the default-on bit stays False per the two-part gate in
        the M11c plan. When #103 closes (Option 1 doc deferral OR
        Option 2 proper composition engine refactor), a separate
        commit flips this to True and wires
        ``DEFAULT_ENABLED_BY_ROLE["tool_executor"]`` accordingly.
        """
        self.assertEqual(ted.RECOMMENDED_DEFAULT_ON, False)


class TestM12ParityGuarantee(unittest.TestCase):
    """The critical guarantee: importing
    ``tool_executor_defaults`` does NOT change the existing role
    defaults the runtime reads. The engine wiring happens in a
    separate M11c-2-or-later commit.
    """

    def test_default_enabled_by_role_unchanged(self) -> None:
        """``DEFAULT_ENABLED_BY_ROLE["tool_executor"]`` must still
        be ``False`` after importing this module — exactly what
        M6 / current config layer shipped."""
        from consultants.config import DEFAULT_ENABLED_BY_ROLE

        self.assertEqual(
            DEFAULT_ENABLED_BY_ROLE["tool_executor"], False,
        )

    def test_default_model_by_role_unchanged(self) -> None:
        """``DEFAULT_MODEL_BY_ROLE["tool_executor"]`` must still be
        ``"gemma4:31b-cloud"`` (the M6 pick)."""
        from consultants.config import DEFAULT_MODEL_BY_ROLE

        self.assertEqual(
            DEFAULT_MODEL_BY_ROLE["tool_executor"], "gemma4:31b-cloud",
        )

    def test_scaffold_default_on_matches_runtime_default(self) -> None:
        """``RECOMMENDED_DEFAULT_ON`` should match the runtime's
        ``DEFAULT_ENABLED_BY_ROLE["tool_executor"]`` bit-for-bit
        for as long as the engine wiring hasn't fired. M11c-1
        shipped the scaffold with both ``False``; M11c-2 kept
        ``RECOMMENDED_DEFAULT_ON=False`` because task #103 isn't
        resolved yet. When #103 closes and a separate commit
        flips the bit, the engine wiring commit MUST update
        ``DEFAULT_ENABLED_BY_ROLE`` in the same atomic change so
        this test stays green."""
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
