"""SharedReadCache — one council run's cache of read-only tool results."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest

from consultants.server.tool_cache import SharedReadCache


class _Recorder:
    def __init__(self, output="ok", delay=0.0):
        self.calls: list[tuple[str, str]] = []
        self.output = output
        self.delay = delay
        self._lock = threading.Lock()

    def __call__(self, name, raw_args, cwd, **kw):
        with self._lock:
            self.calls.append((name, raw_args))
        if self.delay:
            time.sleep(self.delay)
        out = self.output
        return out(name, raw_args) if callable(out) else out


def _args(**kw):
    return json.dumps(kw)


class TestSharedReadCache(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = self.tmp.name
        with open(os.path.join(self.cwd, "a.py"), "w") as f:
            f.write("x = 1\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_repeated_read_runs_once(self):
        ex = _Recorder("contents")
        cache = SharedReadCache(ex)
        for _ in range(3):
            self.assertEqual(cache("read_file", _args(path="a.py"), self.cwd),
                             "contents")
        self.assertEqual(len(ex.calls), 1)
        self.assertEqual((cache.hits, cache.misses), (2, 1))

    def test_argument_order_does_not_split_the_key(self):
        ex = _Recorder()
        cache = SharedReadCache(ex)
        cache("grep", '{"pattern": "x", "path": "."}', self.cwd)
        cache("grep", '{"path": ".", "pattern": "x"}', self.cwd)
        self.assertEqual(len(ex.calls), 1)

    def test_an_edited_file_is_read_again(self):
        ex = _Recorder()
        cache = SharedReadCache(ex)
        cache("read_file", _args(path="a.py"), self.cwd)
        path = os.path.join(self.cwd, "a.py")
        with open(path, "w") as f:
            f.write("x = 2  # longer\n")
        cache("read_file", _args(path="a.py"), self.cwd)
        self.assertEqual(len(ex.calls), 2)

    def test_errors_are_not_replayed(self):
        ex = _Recorder("error: grep timed out")
        cache = SharedReadCache(ex)
        cache("grep", _args(pattern="x"), self.cwd)
        cache("grep", _args(pattern="x"), self.cwd)
        self.assertEqual(len(ex.calls), 2)

    def test_recall_memory_is_never_cached(self):
        """The council writes findings into that store as it runs."""
        ex = _Recorder()
        cache = SharedReadCache(ex)
        cache("recall_memory", _args(query="q"), self.cwd)
        cache("recall_memory", _args(query="q"), self.cwd)
        self.assertEqual(len(ex.calls), 2)

    def test_an_effectful_call_clears_the_cache(self):
        ex = _Recorder()
        cache = SharedReadCache(ex)
        cache("glob", _args(pattern="*.py"), self.cwd)
        cache("write_note", _args(text="t"), self.cwd)
        cache("glob", _args(pattern="*.py"), self.cwd)
        self.assertEqual([c[0] for c in ex.calls],
                         ["glob", "write_note", "glob"])

    def test_a_read_only_call_leaves_the_cache_alone(self):
        ex = _Recorder()
        cache = SharedReadCache(ex, read_only=frozenset({"git_log"}))
        cache("glob", _args(pattern="*.py"), self.cwd)
        cache("recall_memory", _args(query="q"), self.cwd)
        cache("git_log", _args(), self.cwd)
        cache("glob", _args(pattern="*.py"), self.cwd)
        self.assertEqual([c[0] for c in ex.calls],
                         ["glob", "recall_memory", "git_log"])

    def test_concurrent_lanes_share_one_execution(self):
        ex = _Recorder("tree", delay=0.2)
        cache = SharedReadCache(ex)
        results: list[str] = []
        threads = [threading.Thread(target=lambda: results.append(
            cache("survey_project", "{}", self.cwd))) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results, ["tree"] * 6)
        self.assertEqual(len(ex.calls), 1)

    def test_a_raising_call_lets_the_waiters_retry(self):
        state = {"n": 0}

        def flaky(name, raw_args, cwd, **kw):
            state["n"] += 1
            if state["n"] == 1:
                time.sleep(0.1)
                raise RuntimeError("boom")
            return "ok"

        cache = SharedReadCache(flaky)
        out: list = []

        def call():
            try:
                out.append(cache("list_files", "{}", self.cwd))
            except RuntimeError:
                out.append("raised")

        threads = [threading.Thread(target=call) for _ in range(3)]
        threads[0].start()
        time.sleep(0.02)
        for t in threads[1:]:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(out), ["ok", "ok", "raised"])

    def test_the_byte_bound_holds(self):
        ex = _Recorder(lambda n, a: "y" * 60)
        cache = SharedReadCache(ex, max_bytes=100)
        cache("grep", _args(pattern="1"), self.cwd)
        cache("grep", _args(pattern="2"), self.cwd)   # would exceed; not kept
        cache("grep", _args(pattern="2"), self.cwd)
        self.assertEqual(len(ex.calls), 3)


class TestSurfaceWiring(unittest.TestCase):

    def test_the_session_executor_is_cached(self):
        from consultants import config as cc
        from consultants.server.tool_surface import build_tool_surface
        _specs, executor, _reg = build_tool_surface(cc.ConsultantsConfig())
        self.assertIsInstance(executor, SharedReadCache)


if __name__ == "__main__":
    unittest.main()
