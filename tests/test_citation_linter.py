"""Tests for :mod:`consultants.engine.citation_linter`.

The linter has three verification layers; tests cover each in
isolation + the combined ``lint_answer`` pipeline + the real
fabrications caught in the 2026-05-18 M14 first-real-ask
(``csl-2026-05-18-1031-9e3b``) as a regression fixture.

Test layout:

- :class:`TestExtractCitations` — regex extraction shape.
- :class:`TestVerifyCitation` — single-cite filesystem + bounds.
- :class:`TestSymbolMismatch` — AST claimed-vs-actual check.
- :class:`TestLintAnswer` — full pipeline end-to-end.
- :class:`TestRegressionCsl1031` — the 2026-05-18 fixture.
"""
from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from consultants.engine.citation_linter import (
    extract_citations,
    lint_answer,
    verify_citation,
)


# ---------------------------------------------------------------- #
# Fixtures.
# ---------------------------------------------------------------- #

def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _sample_python_module() -> str:
    """A 30-line sample with two functions + a class for AST checks."""
    return textwrap.dedent('''\
        """Sample module for the linter tests."""

        import sys


        def helper_func(arg):
            """A small helper."""
            return arg * 2


        class Container:
            """Some class."""

            def method_one(self):
                """First method."""
                return self.x


            def method_two(self, y):
                """Second method."""
                return y + 1


        def trailing_func():
            """Final function."""
            return 42
    ''')


# ---------------------------------------------------------------- #
# Regex extraction.
# ---------------------------------------------------------------- #

class TestExtractCitations(unittest.TestCase):

    def test_extracts_single_line_cite(self):
        out = extract_citations("see `foo/bar.py:42` for details")
        self.assertEqual(len(out), 1)
        match, path, ls, le = out[0]
        self.assertEqual(match, "foo/bar.py:42")
        self.assertEqual(path, "foo/bar.py")
        self.assertEqual(ls, 42)
        self.assertIsNone(le)

    def test_extracts_range_cite(self):
        out = extract_citations("see `foo/bar.py:42-58` for details")
        self.assertEqual(len(out), 1)
        _, _, ls, le = out[0]
        self.assertEqual(ls, 42)
        self.assertEqual(le, 58)

    def test_extracts_nested_path(self):
        out = extract_citations(
            "the `consultants/engine/store_reaper.py:128` cite"
        )
        self.assertEqual(out[0][1], "consultants/engine/store_reaper.py")
        self.assertEqual(out[0][2], 128)

    def test_does_not_match_bare_filename(self):
        # No directory prefix → no match. Keeps the regex conservative.
        out = extract_citations("see store_reaper.py:128")
        self.assertEqual(out, [])

    def test_does_not_match_url(self):
        # The negative lookbehind blocks `://` patterns.
        out = extract_citations(
            "https://github.com/org/repo/blob/main/file.py:42"
        )
        # First non-URL match would be from /file.py:42, but the
        # leading slash before file.py means the prefix is `/` which
        # the `[A-Za-z0-9_]` first-char rule rejects. So we get 0
        # matches from a clean URL.
        urls_only = [c for c in out if "github.com" in c[1]]
        self.assertEqual(urls_only, [])

    def test_multiple_cites_in_one_text(self):
        out = extract_citations(
            "see `a/b.py:1` and `c/d.py:2-5` and `e/f.py:9`"
        )
        self.assertEqual(len(out), 3)
        self.assertEqual([o[1] for o in out], ["a/b.py", "c/d.py", "e/f.py"])


# ---------------------------------------------------------------- #
# Single-cite verification (path + bounds).
# ---------------------------------------------------------------- #

class TestVerifyCitation(unittest.TestCase):

    def test_existing_file_in_bounds_returns_none(self):
        with tempfile.TemporaryDirectory() as root:
            _write(Path(root) / "src/foo.py", "line1\nline2\nline3\n")
            issue = verify_citation(
                "src/foo.py", 2, None, allowed_roots=[root],
            )
            self.assertIsNone(issue)

    def test_missing_file_returns_issue(self):
        with tempfile.TemporaryDirectory() as root:
            issue = verify_citation(
                "src/nope.py", 1, None, allowed_roots=[root],
            )
            self.assertIsNotNone(issue)
            self.assertIn("file not found", issue.reason)
            self.assertIn("[unverified — file not found]", issue.replacement)

    def test_line_beyond_eof_returns_issue(self):
        with tempfile.TemporaryDirectory() as root:
            _write(Path(root) / "src/foo.py", "only one line\n")
            issue = verify_citation(
                "src/foo.py", 99, None, allowed_roots=[root],
            )
            self.assertIsNotNone(issue)
            self.assertIn("file has 1 lines", issue.replacement)

    def test_range_with_end_beyond_eof_caught(self):
        with tempfile.TemporaryDirectory() as root:
            _write(Path(root) / "src/foo.py", "a\nb\nc\n")
            issue = verify_citation(
                "src/foo.py", 2, 10, allowed_roots=[root],
            )
            self.assertIsNotNone(issue)
            self.assertIn("file has 3 lines", issue.replacement)

    def test_first_root_with_file_wins(self):
        with tempfile.TemporaryDirectory() as a, \
             tempfile.TemporaryDirectory() as b:
            _write(Path(a) / "src/foo.py", "1\n")
            # b doesn't have the file; verify still resolves via a.
            issue = verify_citation(
                "src/foo.py", 1, None, allowed_roots=[b, a],
            )
            self.assertIsNone(issue)

    def test_empty_allowed_roots_returns_not_found(self):
        # Defensive: with no roots, every cite is unverified.
        issue = verify_citation(
            "src/foo.py", 1, None, allowed_roots=[],
        )
        self.assertIsNotNone(issue)
        self.assertIn("file not found", issue.reason)


# ---------------------------------------------------------------- #
# AST symbol mismatch check.
# ---------------------------------------------------------------- #

class TestSymbolMismatch(unittest.TestCase):

    def test_claimed_symbol_at_right_line_no_issue(self):
        with tempfile.TemporaryDirectory() as root:
            _write(Path(root) / "src/sample.py", _sample_python_module())
            # helper_func is at line 6 in the fixture. The answer
            # claims it correctly.
            answer = "the call to `helper_func` at `src/sample.py:6`"
            annotated, issues = lint_answer(
                answer, allowed_roots=[root],
            )
            self.assertEqual(annotated, answer)
            self.assertEqual(issues, [])

    def test_claimed_symbol_at_wrong_line_annotated(self):
        with tempfile.TemporaryDirectory() as root:
            _write(Path(root) / "src/sample.py", _sample_python_module())
            # helper_func is at line 6, but the answer cites :20
            # (which is inside Container.method_two and contains no
            # ``helper_func`` text — genuine wrong-line claim).
            # 2026-05-18 (#205): annotation format changed from
            # ``[in method_two, not helper_func]`` to
            # ``[no helper_func at this line; line is in method_two]``
            # — same semantics, clearer phrasing.
            answer = "the call to `helper_func` at `src/sample.py:20`"
            annotated, issues = lint_answer(
                answer, allowed_roots=[root],
            )
            self.assertEqual(len(issues), 1)
            self.assertIn("method_two", issues[0].replacement)
            self.assertIn("no helper_func", issues[0].replacement)
            self.assertIn(
                "[no helper_func at this line; line is in method_two]",
                annotated,
            )

    def test_claimed_symbol_at_module_scope_annotated(self):
        with tempfile.TemporaryDirectory() as root:
            _write(Path(root) / "src/sample.py", _sample_python_module())
            # Line 3 is module-scope (an import). Answer claims a
            # function name — annotate.
            answer = "the call to `helper_func` at `src/sample.py:3`"
            annotated, issues = lint_answer(
                answer, allowed_roots=[root],
            )
            self.assertEqual(len(issues), 1)
            self.assertIn("module scope", issues[0].replacement)

    def test_no_claimed_symbol_no_annotation(self):
        with tempfile.TemporaryDirectory() as root:
            _write(Path(root) / "src/sample.py", _sample_python_module())
            # The cite stands alone with no backticked function in
            # the proximity window. Annotation should NOT fire.
            answer = (
                "Some prose with no nearby symbol, just `src/sample.py:20`"
            )
            annotated, issues = lint_answer(
                answer, allowed_roots=[root],
            )
            self.assertEqual(annotated, answer)
            self.assertEqual(issues, [])

    def test_literal_filtered_from_claimed_symbol(self):
        with tempfile.TemporaryDirectory() as root:
            _write(Path(root) / "src/sample.py", _sample_python_module())
            # Backticked `None` is a Python literal, not a claimed
            # function name. Linter should not annotate.
            answer = "if the value is `None`, then `src/sample.py:20`"
            _, issues = lint_answer(answer, allowed_roots=[root])
            self.assertEqual(issues, [])

    def test_exception_name_filtered(self):
        with tempfile.TemporaryDirectory() as root:
            _write(Path(root) / "src/sample.py", _sample_python_module())
            # `DistillationFailed` looks like an exception class
            # name (suffix Failed); the cite is about a catch site,
            # not where the class is defined. Skip the mismatch
            # check.
            answer = (
                "the `DistillationFailed` exception is caught at "
                "`src/sample.py:20`"
            )
            _, issues = lint_answer(answer, allowed_roots=[root])
            self.assertEqual(issues, [])

    def test_class_method_claimed_uses_leaf(self):
        # `Container.method_one` cited at the right line: the AST
        # picks the bare method name and the leaf comparison passes.
        with tempfile.TemporaryDirectory() as root:
            _write(Path(root) / "src/sample.py", _sample_python_module())
            # method_one defined at line 15. Use that exact line.
            answer = "see `Container.method_one` at `src/sample.py:15`"
            _, issues = lint_answer(answer, allowed_roots=[root])
            self.assertEqual(issues, [])

    def test_non_python_file_skips_symbol_check(self):
        # The linter only does AST checks on .py files. A .txt
        # file with a function-shaped claim should NOT trigger an
        # annotation.
        with tempfile.TemporaryDirectory() as root:
            _write(Path(root) / "notes/log.txt", "line1\nline2\nline3\n")
            answer = "the call to `helper_func` at `notes/log.txt:2`"
            annotated, issues = lint_answer(
                answer, allowed_roots=[root],
            )
            self.assertEqual(annotated, answer)
            self.assertEqual(issues, [])


# ---------------------------------------------------------------- #
# Full pipeline.
# ---------------------------------------------------------------- #

class TestLintAnswer(unittest.TestCase):

    def test_empty_text_returns_unchanged(self):
        out, issues = lint_answer("", allowed_roots=["/tmp"])
        self.assertEqual(out, "")
        self.assertEqual(issues, [])

    def test_no_citations_returns_unchanged(self):
        with tempfile.TemporaryDirectory() as root:
            text = "plain prose with no citations whatsoever"
            out, issues = lint_answer(text, allowed_roots=[root])
            self.assertEqual(out, text)
            self.assertEqual(issues, [])

    def test_idempotent_on_already_annotated(self):
        with tempfile.TemporaryDirectory() as root:
            answer = (
                "see `src/missing.py:42 [unverified — file not found]` "
                "for details"
            )
            out, issues = lint_answer(answer, allowed_roots=[root])
            # Already annotated; no double-annotation.
            self.assertEqual(out, answer)

    def test_same_cite_appears_twice_replaces_both(self):
        with tempfile.TemporaryDirectory() as root:
            answer = (
                "see `a/missing.py:1` once and `a/missing.py:1` again"
            )
            out, issues = lint_answer(answer, allowed_roots=[root])
            self.assertEqual(out.count("[unverified"), 2)

    def test_mixed_real_and_fabricated_only_annotates_fabricated(self):
        with tempfile.TemporaryDirectory() as root:
            _write(Path(root) / "src/real.py", "a\nb\nc\nd\ne\n")
            answer = (
                "see `src/real.py:3` (real) and `src/fake.py:1` (fake)"
            )
            out, issues = lint_answer(answer, allowed_roots=[root])
            self.assertEqual(len(issues), 1)
            self.assertIn("src/fake.py:1 [unverified", out)
            # Real cite stays untouched — no [unverified] suffix on it.
            self.assertIn("`src/real.py:3` (real)", out)
            self.assertNotIn("src/real.py:3 [unverified", out)


# ---------------------------------------------------------------- #
# Regression fixture — 2026-05-18 M14 first-real-ask.
# ---------------------------------------------------------------- #

class TestRegressionCsl1031(unittest.TestCase):
    """The 2026-05-18 csl-2026-05-18-1031-9e3b session's synthesizer
    answer fabricated 2 distinct cites that this linter must catch:
    a non-existent ``consultants/engine/store_sql.py`` and a wrong
    line for ``_distill_group`` (claimed :128, real :360).

    The fixture is the actual answer text from that session
    truncated to the relevant cite-bearing paragraphs.
    """

    REAL_ANSWER_FRAGMENT = (
        "The reaper calls `_distill_group` "
        "(`consultants/engine/store_reaper.py:128`). "
        "If distillation succeeds, `_write_summary` is called "
        "(`consultants/engine/store_reaper.py:148`). This invokes "
        "`StoreSQL.write_distilled_summary` "
        "(`consultants/engine/store_sql.py:41-61`), which performs "
        "a synchronous `INSERT OR REPLACE` and `commit()` to the "
        "project namespace."
    )

    def test_against_real_repo(self):
        # Resolve the actual claude-hooks repo root from this test
        # file location so the linter has real files to check.
        # 2026-05-18 (#205): annotation format changed to
        # "[no <symbol> at this line; line is in <actual>]". Real
        # fabrications still caught — line 128 of store_reaper.py
        # is in ``_question_hint_for_group`` and contains no
        # ``_distill_group`` text, so the cite is correctly flagged
        # as a wrong-line claim.
        repo_root = str(Path(__file__).resolve().parents[1])
        annotated, issues = lint_answer(
            self.REAL_ANSWER_FRAGMENT,
            allowed_roots=[repo_root],
        )
        # The two known fabrications must be caught.
        replacements = [i.replacement for i in issues]
        self.assertTrue(
            any("store_sql.py" in r and "unverified" in r
                for r in replacements),
            f"store_sql.py fabrication missed; got: {replacements}",
        )
        self.assertTrue(
            any("no _distill_group" in r for r in replacements),
            f"_distill_group line-mismatch missed; got: {replacements}",
        )
        # The annotation must end up in the output text.
        self.assertIn("store_sql.py:41-61 [unverified", annotated)
        self.assertIn("no _distill_group at this line", annotated)


class TestCallSiteIsNotFabrication(unittest.TestCase):
    """#205 (2026-05-18): natural prose like "X calls Y at file:N"
    used to false-positive when N was a call SITE inside a different
    function. The csl-2026-05-18-1428-589c re-run made the bug
    visible — the (old) symbol-mismatch rule flagged ~100 cites
    that were correct claims about call locations.

    Under the new rule (#205), a cite is flagged only when the
    claimed symbol's text DOES NOT appear at the cited line range
    (±1 line slack). Call sites contain the symbol text by
    definition, so they no longer trip the linter.
    """

    def setUp(self):
        # Clear the linter's mtime cache between tests so tempdir
        # mtime collisions can't leak state across cases.
        from consultants.engine.citation_linter import (
            _FILE_LINES_CACHE,
        )
        _FILE_LINES_CACHE.clear()

    def _write_source(self, root: Path) -> None:
        body = textwrap.dedent('''\
            def helper():
                return 42


            def caller():
                # The call site lives here.
                x = helper()
                return x


            class C:
                def method(self):
                    return helper()
        ''')
        _write(root / "pkg" / "code.py", body)

    def test_call_site_cite_does_not_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_source(root)
            # Line 7 contains ``x = helper()`` — a legitimate call
            # site. Pre-#205 the linter flagged this because the
            # enclosing function is ``caller``, not ``helper``. Now
            # it correctly passes through.
            answer = "The function `helper` is called at `pkg/code.py:7`."
            annotated, issues = lint_answer(
                answer, allowed_roots=[str(root)],
            )
            self.assertEqual(issues, [])
            self.assertEqual(annotated, answer)

    def test_method_call_site_does_not_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_source(root)
            # Line 13 is ``return helper()`` inside C.method. Enclosing
            # is method, not helper — but ``helper`` text IS on the
            # line, so no flag.
            answer = "`helper` is invoked at `pkg/code.py:13`."
            annotated, issues = lint_answer(
                answer, allowed_roots=[str(root)],
            )
            self.assertEqual(issues, [])

    def test_genuine_wrong_line_still_flagged(self):
        """Symbol mentioned NOWHERE near the cited line → still flag."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_source(root)
            # Line 2 (``return 42``) has no ``caller`` text and is
            # inside helper, not caller — genuine wrong-line claim.
            answer = "The function `caller` is at `pkg/code.py:2`."
            annotated, issues = lint_answer(
                answer, allowed_roots=[str(root)],
            )
            self.assertEqual(len(issues), 1)
            self.assertIn("no caller at this line", issues[0].replacement)

    def test_slack_window_allows_off_by_one(self):
        """A cite at the def line ±1 (decorator vs def) should pass."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            body = textwrap.dedent('''\
                # comment

                def real_def():
                    return 1
            ''')
            _write(root / "pkg" / "code.py", body)
            # def at line 3; cite line 2 (the blank line just before).
            # Default slack=1 should accept this as valid (within ±1).
            answer = "`real_def` lives at `pkg/code.py:2`."
            annotated, issues = lint_answer(
                answer, allowed_roots=[str(root)],
            )
            self.assertEqual(issues, [])


class TestGraphFastPath(unittest.TestCase):
    """The linter prefers the on-disk code_graph for symbol lookup
    when available, falling back to on-demand ast.parse otherwise.

    The graph path is exercised end-to-end: build a tiny project,
    run the linter against a fabrication targeting the project's
    files, and verify the issue is caught. A second test then
    DELETES the graph and re-runs to confirm the ast.parse fallback
    still catches the same fabrication.
    """

    def setUp(self):
        # Tests build temp graphs; make sure the helper's process-
        # global mtime cache doesn't leak between scenarios.
        from claude_hooks.code_graph import enclosing
        enclosing.clear_cache()

    def _build_repo(self, tmp: Path) -> None:
        (tmp / ".git").mkdir()
        body = textwrap.dedent('''\
            """Sample for the graph fast-path test."""

            def helper_a():
                return 1


            def helper_b():
                return 2


            class Holder:
                def method_one(self):
                    return helper_a()

                def method_two(self):
                    return helper_b()
        ''')
        _write(tmp / "pkg" / "code.py", body)

    def test_graph_catches_wrong_symbol(self):
        from claude_hooks.code_graph.builder import build_graph
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._build_repo(root)
            build_graph(root)

            # Line 4 is inside helper_a (which returns 1) and contains
            # zero ``helper_b`` text. The fabrication mirrors gemma's
            # wrong-line failure mode — the symbol is real elsewhere
            # in the file but absent at the cited line.
            answer = (
                "The helper at `helper_b` lives at `pkg/code.py:4`."
            )
            annotated, issues = lint_answer(
                answer, allowed_roots=[str(root)],
            )
            self.assertEqual(len(issues), 1)
            self.assertIn("no helper_b", issues[0].replacement)
            self.assertIn("line is in helper_a", annotated)

    def test_fallback_when_graph_missing(self):
        """Without a graph the linter still catches the same fab via
        ast.parse — i.e. coverage doesn't depend on the graph being
        present."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._build_repo(root)
            # NO build_graph call: graphify-out/ doesn't exist.
            answer = (
                "The helper at `helper_b` lives at `pkg/code.py:4`."
            )
            annotated, issues = lint_answer(
                answer, allowed_roots=[str(root)],
            )
            self.assertEqual(len(issues), 1)
            self.assertIn("no helper_b", issues[0].replacement)

    def test_fallback_when_file_outside_graph(self):
        """A cite that points at an allowed_root file outside the
        graph build root must still be checked via ast.parse."""
        from claude_hooks.code_graph.builder import build_graph
        with tempfile.TemporaryDirectory() as graph_root, \
             tempfile.TemporaryDirectory() as off_graph_root:
            gr = Path(graph_root)
            ogr = Path(off_graph_root)
            self._build_repo(gr)
            build_graph(gr)
            # File only exists outside the graph build root.
            body = textwrap.dedent('''\
                def outsider():
                    return 0


                def neighbour():
                    return 1
            ''')
            _write(ogr / "src" / "outside.py", body)
            # Claim neighbour at line 2 (``return 0`` in outsider's
            # body) — no ``neighbour`` text on that line, so it's a
            # genuine wrong-line claim.
            answer = "The fn `neighbour` is at `src/outside.py:2`."
            annotated, issues = lint_answer(
                answer,
                allowed_roots=[str(gr), str(ogr)],
            )
            self.assertEqual(len(issues), 1)
            self.assertIn("no neighbour", issues[0].replacement)
            self.assertIn("line is in outsider", annotated)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
