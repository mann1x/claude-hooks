"""Pre-flight path reachability — ``consultants.engine.preflight``.

Why this exists (csl-2026-08-02-0532-d737): a consultancy ran for
**55 minutes** against a codebase it could not read. One allowed root
never reached the citation linter, so every cite in a fully-grounded
answer came back ``[unverified — file not found]`` and the result was
nearly reported as hallucination. The files were named in the question
from the first second; checking them costs a handful of ``stat`` calls.

Two behaviours are pinned here:

* **What blocks.** Only "the question names paths and *none* of them
  are readable" — the wrong-roots signature. A single unreachable path
  among readable ones is a typo or a file the asker wants created, and
  must not block.
* **What "readable" means.** Relative paths resolve exactly as the
  citation linter resolves them — under each root in order, first hit
  wins — because the pre-flight's job is to predict the linter's later
  verdict. Absolute paths are stricter than the linter: existing on
  disk isn't enough, they must sit under an allowed root, since a file
  the sandbox will refuse to open is not readable by this run.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from consultants.engine.preflight import (
    Preflight,
    check_question_paths,
    extract_paths,
)


class TestExtractPaths(unittest.TestCase):
    def test_finds_bare_path(self):
        got = extract_paths("please look at eval/scorers.py for this")
        self.assertEqual([p for _, p in got], ["eval/scorers.py"])

    def test_finds_cited_path_with_line(self):
        got = extract_paths("see eval/scorers.py:600 — the gate")
        self.assertEqual(got, [("eval/scorers.py:600", "eval/scorers.py")])

    def test_finds_line_range(self):
        got = extract_paths("a2at/tools.py:870-883 does the merge")
        self.assertEqual(
            got, [("a2at/tools.py:870-883", "a2at/tools.py")])

    def test_dedupes_on_path_keeping_first(self):
        got = extract_paths("x/a.py:1 and later x/a.py:99")
        self.assertEqual(got, [("x/a.py:1", "x/a.py")])

    def test_ignores_bare_filename_without_directory(self):
        # "Makefile", "setup.py" alone are too ambiguous to act on —
        # and blocking a run over a prose mention would be worse than
        # not checking at all.
        self.assertEqual(extract_paths("run setup.py first"), [])

    def test_ignores_urls(self):
        got = extract_paths(
            "docs at https://example.com/docs/guide.html:12 explain it")
        self.assertEqual(got, [])

    def test_ignores_prose_version_numbers(self):
        self.assertEqual(extract_paths("shipped in v1.8.3, see notes"), [])

    def test_absolute_path_keeps_its_leading_slash(self):
        # Dropping the "/" would turn an absolute path into a
        # relative one and report a readable file as unreachable.
        got = extract_paths("open /srv/dev/an/eval/x.py:4 please")
        self.assertEqual([p for _, p in got], ["/srv/dev/an/eval/x.py"])


class _Tree:
    """A temp tree with two roots: ``primary`` and ``extra``."""

    def __enter__(self):
        self._td = TemporaryDirectory()
        base = Path(self._td.name)
        self.primary = base / "primary"
        self.extra = base / "extra"
        (self.primary / "pkg").mkdir(parents=True)
        (self.extra / "eval").mkdir(parents=True)
        (self.primary / "pkg" / "here.py").write_text("x = 1\n")
        (self.extra / "eval" / "scorers.py").write_text("y = 2\n")
        return self

    def __exit__(self, *exc):
        self._td.cleanup()
        return False


class TestCheckQuestionPaths(unittest.TestCase):
    def test_no_paths_named_never_blocks(self):
        pf = check_question_paths("why is the council slow?", roots=["/"])
        self.assertEqual(pf.refs, [])
        self.assertFalse(pf.blocking)
        self.assertIsNone(pf.warning())

    def test_resolvable_under_primary_root(self):
        with _Tree() as t:
            pf = check_question_paths(
                "explain pkg/here.py:1", roots=[str(t.primary)])
            self.assertEqual([r.status for r in pf.refs], ["ok"])
            self.assertFalse(pf.blocking)

    def test_resolvable_only_under_an_extra_root(self):
        # The exact csl-d737 shape: subject files live under a root
        # passed via --add-dir, not under cwd.
        with _Tree() as t:
            pf = check_question_paths(
                "explain eval/scorers.py:1",
                roots=[str(t.primary), str(t.extra)],
            )
            self.assertEqual([r.status for r in pf.refs], ["ok"])
            self.assertFalse(pf.blocking)

    def test_the_reproducer_blocks_when_the_extra_root_is_missing(self):
        with _Tree() as t:
            pf = check_question_paths(
                "explain eval/scorers.py:600 and eval/scorers.py:900 "
                "plus eval/other.py",
                roots=[str(t.primary)],
            )
            self.assertTrue(pf.blocking)
            self.assertEqual(len(pf.unreachable), 2)

    def test_one_bad_path_among_good_ones_warns_but_does_not_block(self):
        with _Tree() as t:
            pf = check_question_paths(
                "compare pkg/here.py with nope/gone.py",
                roots=[str(t.primary)],
            )
            self.assertFalse(pf.blocking)
            warn = pf.warning()
            self.assertIsNotNone(warn)
            self.assertIn("nope/gone.py", warn)

    def test_missing_file_in_an_existing_directory_is_creatable(self):
        # "write me pkg/newthing.py" must never be refused.
        with _Tree() as t:
            pf = check_question_paths(
                "create pkg/newthing.py with a parser",
                roots=[str(t.primary)],
            )
            self.assertEqual([r.status for r in pf.refs], ["creatable"])
            self.assertFalse(pf.blocking)

    def test_creatable_alone_produces_no_warning(self):
        with _Tree() as t:
            pf = check_question_paths(
                "create pkg/newthing.py", roots=[str(t.primary)])
            self.assertIsNone(pf.warning())

    def test_directory_is_not_a_readable_file(self):
        # ``pkg`` exists but is a directory; a cite to it is not a
        # resolvable file. It has no extension so it isn't extracted
        # at all — pin that rather than let a future regex change it
        # silently.
        with _Tree() as t:
            pf = check_question_paths("look in pkg/", roots=[str(t.primary)])
            self.assertEqual(pf.refs, [])

    def test_absolute_path_outside_every_root_is_unreachable(self):
        with _Tree() as t:
            pf = check_question_paths(
                "read /definitely/not/here/a.py and "
                "/definitely/not/here/b.py and /definitely/not/here/c.py",
                roots=[str(t.primary)],
            )
            self.assertTrue(pf.blocking)

    def test_empty_roots_blocks_when_paths_are_named(self):
        pf = check_question_paths(
            "see a/x.py, b/y.py and c/z.py", roots=[])
        self.assertTrue(pf.blocking)


class TestMessage(unittest.TestCase):
    def _blocked(self) -> Preflight:
        return check_question_paths(
            "see a/x.py, b/y.py and c/z.py", roots=["/nonexistent-root"])

    def test_message_names_paths_roots_and_the_fix(self):
        msg = self._blocked().message()
        self.assertIn("a/x.py", msg)
        self.assertIn("/nonexistent-root", msg)
        self.assertIn("--add-dir", msg)
        # The operator's first question is "did this cost me anything?"
        self.assertIn("Nothing was spent", msg)

    def test_message_offers_both_explanations(self):
        # Refusing on "all named paths are unreachable" catches the
        # wrong-roots case, but a greenfield ask that names only
        # files under a directory that doesn't exist yet looks
        # identical. The message must not assert the first reading.
        msg = self._blocked().message()
        self.assertIn("roots are wrong", msg)
        self.assertIn("doesn't exist", msg)

    def test_message_prefers_display_roots(self):
        # Realpaths are unreadable to a human who typed /shared/dev/x.
        msg = self._blocked().message(display_roots=["/shared/dev/x"])
        self.assertIn("/shared/dev/x", msg)
        self.assertNotIn("/nonexistent-root", msg)

    def test_warning_is_none_when_blocking(self):
        # Blocking and warning are mutually exclusive: the caller
        # logs one or the other, never both for the same verdict.
        self.assertIsNone(self._blocked().warning())


class _FakeState:
    """Enough of SessionState for ``_preflight_refused``."""

    def __init__(self, cwd: str):
        self.sid = "csl-test-0001"
        self.status = "running"
        self.error = None
        self.finished_at = None
        self.progress = {"planner": "in_progress", "researcher": "queued"}
        self.topology = "council"
        self.effort = "high"
        self.started_at = 0.0
        self.parent_sid = None
        self.root_sid = None


class TestRunnerRefusal(unittest.TestCase):
    """``runner._preflight_refused`` — the bridge from verdict to a
    failed session. Importable without LangGraph: it is a module-level
    function, not part of the closure ``make_runner`` builds."""

    def _refused(self, question: str, roots, cwd: str):
        from consultants.server import runner as R
        state = _FakeState(cwd)
        written = {}

        def _fake_write(st, cwd_, q, exc):
            written["error"] = str(exc)

        orig = R._write_failed_artifacts
        R._write_failed_artifacts = _fake_write
        try:
            out = R._preflight_refused(
                state, question, cwd=cwd, extra_roots=tuple(roots),
                cwd_display=cwd, extra_roots_display=tuple(roots),
                label="council",
            )
        finally:
            R._write_failed_artifacts = orig
        return out, state, written

    def test_unreadable_question_marks_the_session_failed(self):
        with _Tree() as t:
            out, state, written = self._refused(
                "explain eval/scorers.py:600, eval/scorers.py:900 and "
                "eval/other.py",
                roots=[], cwd=str(t.primary),
            )
            self.assertTrue(out)
            self.assertEqual(state.status, "failed")
            self.assertIn("pre-flight refused", state.error)
            self.assertIn("pre-flight refused", written.get("error", ""))
            # No role may be left "in_progress" — a poller would show a
            # session that is failed and still working.
            self.assertEqual(set(state.progress.values()), {"done"})

    def test_readable_question_proceeds_untouched(self):
        with _Tree() as t:
            out, state, written = self._refused(
                "explain pkg/here.py:1", roots=[], cwd=str(t.primary),
            )
            self.assertFalse(out)
            self.assertEqual(state.status, "running")
            self.assertIsNone(state.error)
            self.assertEqual(written, {})

    def test_a_broken_preflight_never_blocks_a_run(self):
        # The check exists to save money, not to become a new way for
        # runs to die. If it raises, the run proceeds.
        from consultants.server import runner as R
        import consultants.engine.preflight as PF

        def _boom(*a, **kw):
            raise RuntimeError("preflight is broken")

        orig = PF.check_question_paths
        PF.check_question_paths = _boom
        try:
            state = _FakeState("/tmp")
            out = R._preflight_refused(
                state, "see a/x.py, b/y.py, c/z.py", cwd="/tmp",
                extra_roots=(), cwd_display="/tmp",
                extra_roots_display=(), label="council",
            )
        finally:
            PF.check_question_paths = orig
        self.assertFalse(out)
        self.assertEqual(state.status, "running")


if __name__ == "__main__":
    unittest.main()
