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
        # Hash prefix MAY be empty in M11c-1; just check the
        # attribute exists.
        self.assertIsInstance(ted.RECOMMENDED_SUITE_HASH_PREFIX, str)

    def test_recommended_model_is_empty_sentinel(self) -> None:
        """M11c-1: no model recommendation yet.

        M11c-2 must populate this with a real model tag like
        ``"gemma4:31b-cloud"`` or whichever wins the rubric.
        """
        self.assertEqual(ted.RECOMMENDED_TOOL_EXECUTOR_MODEL, "")

    def test_recommended_default_on_is_false_in_scaffold(self) -> None:
        """M11c-1: the role stays disabled-by-default until M11c-2
        proves a model passes the rubric AND task #103 resolves
        x-tier composition.
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
        """The scaffold's ``RECOMMENDED_DEFAULT_ON`` should match
        the runtime's ``DEFAULT_ENABLED_BY_ROLE["tool_executor"]``
        bit-for-bit while the scaffold is in M11c-1 state. If they
        ever drift, the engine wiring fired prematurely — surface
        as a test failure."""
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
