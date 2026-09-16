"""A bounded search must say it was bounded.

find_project_root stops at the nearest marker, and in a monorepo that is
the package's own package.json. The server is then rooted at the package
and answers correctly *for the package* — which is not the question the
caller asked. Measured on a real monorepo: a symbol with 443 occurrences
across the tree returned 9, all inside the declaring package, with
nothing in the result indicating a boundary. A plausible number a caller
acts on is worse than an absurd one.

Widening the root is not the fix and was measured too: rooted at that
repo (6.1 GB, no root tsconfig) tsserver answered 0 references in 81.7 s
and then failed, falling back to an inferred project over the whole
tree. So the engine keeps the narrow root and reports the boundary.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from claude_hooks.lsp_engine.engine import NavResponse  # noqa: E402
from claude_hooks.lsp_mcp import tools as T  # noqa: E402
from claude_hooks.lsp_mcp.server import (  # noqa: E402
    ROOT_SENTINEL, describe_scope, find_project_root,
)


class ScopeDescriptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        (self.repo / ".git").mkdir(parents=True)
        self.pkg = self.repo / "packages" / "shared"
        self.pkg.mkdir(parents=True)
        (self.pkg / "package.json").write_text("{}", encoding="utf-8")
        self.addCleanup(self.tmp.cleanup)

    def test_a_package_inside_a_repo_is_flagged(self) -> None:
        note = describe_scope(self.pkg)
        self.assertIsNotNone(note)
        assert note is not None
        self.assertIn("shared", note)
        self.assertIn(str(self.repo), note)
        self.assertIn("Sibling packages were not searched", note)

    def test_a_repo_root_is_not_flagged(self) -> None:
        # No false positives: the common case must stay quiet.
        self.assertIsNone(describe_scope(self.repo))

    def test_the_package_is_still_the_root(self) -> None:
        # Deliberately unchanged: widening it measured worse.
        src = self.pkg / "src"
        src.mkdir()
        f = src / "x.ts"
        f.write_text("", encoding="utf-8")
        self.assertEqual(find_project_root(f), self.pkg)


class RootSentinelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.pkg = self.repo / "packages" / "shared" / "src"
        self.pkg.mkdir(parents=True)
        (self.repo / "packages" / "shared" / "package.json").write_text(
            "{}", encoding="utf-8")
        (self.repo / ".git").mkdir()
        self.file = self.pkg / "x.ts"
        self.file.write_text("", encoding="utf-8")
        self.addCleanup(self.tmp.cleanup)

    def test_sentinel_overrides_the_marker_walk(self) -> None:
        sentinel = self.repo / ROOT_SENTINEL
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text("", encoding="utf-8")
        self.assertEqual(find_project_root(self.file), self.repo)

    def test_without_it_the_package_wins(self) -> None:
        self.assertEqual(find_project_root(self.file),
                         self.repo / "packages" / "shared")

    def test_the_declared_root_is_not_flagged_as_bounded(self) -> None:
        # Declared deliberately, so there is no boundary to warn about
        # even when the declared root sits INSIDE a larger repository.
        inner = self.repo / "packages"
        sentinel = inner / ROOT_SENTINEL
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text("", encoding="utf-8")
        self.assertIsNone(describe_scope(inner))
        # ...while an undeclared package at the same depth still warns.
        self.assertIsNotNone(describe_scope(self.repo / "packages" / "shared"))


class ScopeRenderingTests(unittest.TestCase):
    def test_results_carry_the_searched_root(self) -> None:
        from claude_hooks.lsp_engine.protocol import Location, Position, Range
        loc = Location(uri=Path(tempfile.gettempdir(), "a.ts").as_uri(),
                       range=Range(start=Position(line=1, character=0),
                                   end=Position(line=1, character=4)))
        out = T.render_locations(
            NavResponse(items=[loc], consulted=("tsserver",)),
            title="References", scope="shared — a package inside /repo")
        self.assertIn("References (1)", out)
        self.assertIn("SEARCHED", out)
        self.assertIn("a package inside /repo", out)

    def test_an_empty_bounded_result_still_says_where_it_looked(self) -> None:
        # The dangerous case: "none found" for a symbol used next door.
        out = T.render_locations(
            NavResponse(items=[], consulted=("tsserver",)),
            title="References", scope="shared — a package inside /repo")
        self.assertIn("none found", out)
        self.assertIn("SEARCHED", out)

    def test_no_scope_means_no_extra_line(self) -> None:
        out = T.render_locations(
            NavResponse(items=[], consulted=("tsserver",)), title="References")
        self.assertNotIn("SEARCHED", out)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
