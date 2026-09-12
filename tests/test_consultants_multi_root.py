"""Tests for the multi-root tool sandbox in /consultants (v1.8+).

Covers the HTTP boundary + state persistence + parent/follow-up
merge. The runner internals are exercised by the existing council /
runner suites; here we pin the contracts new to v1.8.
"""

from __future__ import annotations

import unittest

from consultants.server.app import (
    SessionState,
    _merge_extra_roots,
)


class TestSessionStateExtraRoots(unittest.TestCase):

    def test_default_empty_list(self):
        s = SessionState(sid="x", cwd="/tmp", question="q",
                         effort="medium", topology="council")
        self.assertEqual(s.extra_roots, [])

    def test_round_trips(self):
        roots = ["/a", "/b"]
        s = SessionState(sid="x", cwd="/tmp", question="q",
                         effort="medium", topology="council",
                         extra_roots=list(roots))
        self.assertEqual(s.extra_roots, roots)


class TestMergeExtraRoots(unittest.TestCase):

    def test_empty_parent_empty_followup(self):
        self.assertEqual(_merge_extra_roots([], []), [])

    def test_parent_only(self):
        self.assertEqual(_merge_extra_roots(["/a"], []), ["/a"])

    def test_followup_only(self):
        self.assertEqual(_merge_extra_roots([], ["/b"]), ["/b"])

    def test_parent_first_then_followup(self):
        self.assertEqual(
            _merge_extra_roots(["/a"], ["/b"]),
            ["/a", "/b"],
        )

    def test_dedup_preserves_first_occurrence(self):
        # Follow-up repeating a parent entry → dedup, parent's order wins.
        self.assertEqual(
            _merge_extra_roots(["/a", "/b"], ["/b", "/c"]),
            ["/a", "/b", "/c"],
        )

    def test_drops_empty_strings(self):
        self.assertEqual(
            _merge_extra_roots(["/a", ""], ["", "/b"]),
            ["/a", "/b"],
        )

    def test_multi_dedup(self):
        # Multiple parent + follow-up entries with overlaps.
        self.assertEqual(
            _merge_extra_roots(
                ["/a", "/b", "/c"],
                ["/c", "/d", "/b", "/e"],
            ),
            ["/a", "/b", "/c", "/d", "/e"],
        )


class TestRunnerInputMerge(unittest.TestCase):
    """Verify the follow-up runner sees the merged parent+follow-up set
    when it calls ``runner_input.get("extra_roots")``. We don't drive
    the full HTTP layer here; just exercise the merge contract that
    ``app.py`` and ``runner.py`` share."""

    def test_runner_merges_parent_and_followup(self):
        # Mirror runner.py's merge logic — confirms the contract is
        # one-to-one with ``_merge_extra_roots``.
        parent_extras = ["/parent-1", "/parent-2"]
        follow_extras = ["/followup-1", "/parent-1"]
        seen: set = set()
        merged = []
        for r in parent_extras + follow_extras:
            if r and r not in seen:
                seen.add(r)
                merged.append(r)
        self.assertEqual(
            merged,
            ["/parent-1", "/parent-2", "/followup-1"],
        )
        # And the canonical helper produces the same result.
        self.assertEqual(merged, _merge_extra_roots(parent_extras, follow_extras))


class TestDiskReopenRestoresRoots(unittest.TestCase):
    """A disk-reopened parent must contribute its ``extra_roots`` to a
    follow-up.

    A follow-up merges the parent's roots with its own. Before
    metadata.json carried them (2026-08-02), a parent reopened from
    disk — which is every parent after an engine restart or an idle
    reap — contributed nothing, so the follow-up silently ran with cwd
    alone. Same blindness as the ``extra_roots`` channel drop, arriving
    by a different route.
    """

    def _reopen(self, meta_extra: dict):
        import json
        import tempfile
        from pathlib import Path

        from consultants.engine import storage
        from consultants.server.app import _load_session_from_artifacts

        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            sid = "csl-2026-08-02-reopen"
            sdir = cwd / ".claude-hooks" / "consultants" / sid
            sdir.mkdir(parents=True)
            meta = {
                "session_id": sid, "question": "q", "models": {},
                "topology": "council", "effort": "high",
                "final_answer": "a", "turns": [],
                "duration_seconds": 1.0, "status": "completed",
                "created": "2026-08-02T07:00:00", "cwd": str(cwd),
            }
            meta.update(meta_extra)
            (sdir / storage.METADATA_FILENAME).write_text(
                json.dumps(meta))
            return _load_session_from_artifacts(sid, cwd), str(cwd)

    def test_roots_are_restored_from_metadata(self):
        state, _ = self._reopen({
            "extra_roots": ["/srv/real/a", "/srv/real/b"],
            "extra_roots_display": ["/shared/a", "/shared/b"],
            "cwd_display": "/shared/proj",
        })
        self.assertEqual(state.extra_roots, ["/srv/real/a", "/srv/real/b"])
        self.assertEqual(state.extra_roots_display,
                         ["/shared/a", "/shared/b"])
        self.assertEqual(state.cwd_display, "/shared/proj")

    def test_pre_2026_08_metadata_reopens_with_empty_roots(self):
        # Sessions written before the fields existed must reopen
        # cleanly rather than raise — they just carry nothing, and the
        # follow-up has to re-pass --add-dir.
        state, cwd = self._reopen({})
        self.assertEqual(state.extra_roots, [])
        self.assertEqual(state.extra_roots_display, [])
        self.assertEqual(state.cwd_display, cwd)


if __name__ == "__main__":
    unittest.main()
