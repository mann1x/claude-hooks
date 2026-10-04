"""Per-turn hooks read the end of a transcript, never all of it.

Transcripts of long-lived sessions reach gigabytes (opencoti 5.2 GB).
The Stop hook and the task nudge parsed the whole file on every Stop,
which held the hook daemon at a 20.9 GB high-water mark.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from claude_hooks.transcript_tail import read_tail  # noqa: E402


def _prompt(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def _tool_result():
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "content": "x" * 200}]}}


def _assistant(i):
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "name": "Edit", "input": {"file_path": f"f{i}"}}]}}


class ReadTailTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "t.jsonl"

    def write(self, rows):
        self.path.write_text("".join(json.dumps(r) + "\n" for r in rows))

    def is_prompt(self, r):
        return isinstance(r["message"]["content"], str)

    def test_grows_until_the_turn_boundary(self):
        rows = [_prompt("old")] + [_tool_result() for _ in range(200)]
        rows += [_prompt("this turn")] + [_tool_result() for _ in range(300)]
        self.write(rows)
        got = read_tail(str(self.path), stop_at=self.is_prompt, start=512)
        self.assertIn(_prompt("this turn"), got)
        self.assertLess(len(got), len(rows))
        self.assertEqual(got[-1], rows[-1])

    def test_never_reads_more_than_the_cap(self):
        self.write([_tool_result() for _ in range(2000)])
        with mock.patch("builtins.open", wraps=open) as op:
            got = read_tail(str(self.path), stop_at=self.is_prompt,
                            start=1024, cap=16 * 1024)
        self.assertTrue(op.called)
        self.assertLess(len(got), 2000)
        self.assertGreater(len(got), 0)

    def test_a_small_file_is_read_whole(self):
        rows = [_prompt("a"), _assistant(1)]
        self.write(rows)
        self.assertEqual(read_tail(str(self.path), stop_at=self.is_prompt),
                         rows)

    def test_the_cut_first_line_is_dropped(self):
        rows = [_prompt("p")] + [_assistant(i) for i in range(50)]
        self.write(rows)
        got = read_tail(str(self.path), start=300)
        for r in got:
            self.assertIn(r, rows)

    def test_missing_file_is_none(self):
        self.assertIsNone(read_tail(str(self.path) + ".nope"))


class StopReadsOnlyTheTurnTests(unittest.TestCase):

    def test_stop_transcript_is_the_tail_with_the_turn(self):
        from claude_hooks.hooks import stop
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.jsonl"
            rows = ([_prompt("old")] + [_tool_result()] * 30000
                    + [_prompt("now"), _assistant(1), _tool_result()])
            p.write_text("".join(json.dumps(r) + "\n" for r in rows))
            got = stop._read_transcript(str(p))
        self.assertLess(len(got), len(rows))
        idx = stop._find_last_user_idx(got)
        self.assertEqual(got[idx], _prompt("now"))
        self.assertTrue(stop._turn_modified_files(got))

    def test_task_nudge_sees_the_turns_tools(self):
        from claude_hooks.tasks.hook import _turn_tools
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.jsonl"
            rows = ([_prompt("old"), _assistant(0)] + [_tool_result()] * 30000
                    + [_prompt("now"), _assistant(1), _tool_result()])
            p.write_text("".join(json.dumps(r) + "\n" for r in rows))
            uses = _turn_tools(str(p))
        self.assertEqual([u["input"]["file_path"] for u in uses], ["f1"])


class SessionEndStreamsTests(unittest.TestCase):

    def test_push_streams_the_file(self):
        from claude_hooks.hooks import session_end
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.jsonl"
            p.write_text(json.dumps(_prompt("x" * 500)) + "\n")
            seen = {}

            class _Resp:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def read(self):
                    return b'{"project": "p"}'

            def fake(req, timeout):
                seen["data"] = req.data
                seen["len"] = req.get_header("Content-length")
                seen["body"] = req.data.read()
                return _Resp()

            with mock.patch("urllib.request.urlopen", side_effect=fake):
                session_end._push_transcript(
                    {"transcript_path": str(p), "cwd": d, "session_id": "s"},
                    {"server_url": "http://x"})
        self.assertFalse(isinstance(seen["data"], (bytes, bytearray)))
        self.assertEqual(int(seen["len"]), p.stat().st_size if p.exists()
                         else len(seen["body"]))
        self.assertTrue(seen["data"].closed)
