"""M5 — ``consult --wait``: the blocking convenience path.

``--wait`` turns the otherwise-async consult into one command that
returns the final result, removing the Workflow-authoring footgun of
hand-rolling a poll loop. The bare (no ``--wait``) path is unchanged —
parity-trivial.

Cohorts:
- parser: ``--wait`` / ``--poll-interval`` / ``--wait-timeout`` defaults.
- ``_wait_for_terminal``: polls past ``running`` to a terminal status;
  raises on a positive timeout while still running (run is NOT killed).
- ``_fetch_result``: retries the brief status-flips-before-artifacts race.
- ``cmd_consult --wait``: completed → ``ok:true`` + result; failed →
  ``ok:false`` + rc 1.
- bare ``cmd_consult`` (no ``--wait``) → unchanged run-record output.

langgraph-free → runs in BOTH envs.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from consultants.cli import (  # noqa: E402
    CLIError,
    _fetch_result,
    _wait_for_terminal,
    build_parser,
    cmd_consult,
)


def _consult_ns(**kw):
    defaults = {
        "message": "why is the sky blue?",
        "cwd": None, "effort": None, "add_dir": [], "trace": None,
        "wait": False, "poll_interval": 2.0, "wait_timeout": 0.0,
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


# ----------------------- parser ---------------------------------- #

class TestParser(unittest.TestCase):
    def test_wait_defaults(self):
        args = build_parser().parse_args(
            ["consult", "--message", "q?"])
        self.assertFalse(args.wait)
        self.assertEqual(args.poll_interval, 2.0)
        self.assertEqual(args.wait_timeout, 0.0)

    def test_wait_flags_parse(self):
        args = build_parser().parse_args([
            "consult", "--message", "q?",
            "--wait", "--poll-interval", "0.5", "--wait-timeout", "60",
        ])
        self.assertTrue(args.wait)
        self.assertEqual(args.poll_interval, 0.5)
        self.assertEqual(args.wait_timeout, 60.0)


# ----------------------- _wait_for_terminal ---------------------- #

class TestWaitForTerminal(unittest.TestCase):
    def test_polls_until_completed(self):
        statuses = [
            {"sid": "csl-1", "status": "running"},
            {"sid": "csl-1", "status": "running"},
            {"sid": "csl-1", "status": "completed"},
        ]
        calls = {"n": 0}

        def _fake_http(method, url, *, body=None, timeout=600.0):
            i = calls["n"]
            calls["n"] += 1
            return statuses[i]

        slept = []
        with mock.patch("consultants.cli._http", side_effect=_fake_http):
            rec = _wait_for_terminal(
                "http://b", "csl-1", interval=1.0, timeout=0.0,
                sleep_fn=lambda s: slept.append(s))
        self.assertEqual(rec["status"], "completed")
        self.assertEqual(calls["n"], 3)
        self.assertEqual(slept, [1.0, 1.0])    # slept between the 3 polls

    def test_failed_is_terminal(self):
        with mock.patch(
            "consultants.cli._http",
            return_value={"sid": "csl-1", "status": "failed"},
        ):
            rec = _wait_for_terminal(
                "http://b", "csl-1", interval=1.0, timeout=0.0,
                sleep_fn=lambda s: None)
        self.assertEqual(rec["status"], "failed")

    def test_timeout_raises_without_killing(self):
        clock = {"t": 100.0}

        def _now():
            return clock["t"]

        def _sleep(s):
            clock["t"] += s        # advance the injected clock

        with mock.patch(
            "consultants.cli._http",
            return_value={"sid": "csl-1", "status": "running"},
        ):
            with self.assertRaises(CLIError) as ctx:
                _wait_for_terminal(
                    "http://b", "csl-1", interval=5.0, timeout=10.0,
                    sleep_fn=_sleep, now_fn=_now)
        self.assertEqual(ctx.exception.exit_code, 1)
        self.assertIn("still running", str(ctx.exception))


# ----------------------- _fetch_result --------------------------- #

class TestFetchResult(unittest.TestCase):
    def test_retries_then_succeeds(self):
        seq = [
            CLIError("HTTP 409 ...", exit_code=1),
            CLIError("HTTP 404 ...", exit_code=1),
            {"sid": "csl-1", "summary_markdown": "A", "metadata": {}},
        ]
        calls = {"n": 0}

        def _fake_http(method, url, *, body=None, timeout=600.0):
            v = seq[calls["n"]]
            calls["n"] += 1
            if isinstance(v, Exception):
                raise v
            return v

        with mock.patch("consultants.cli._http", side_effect=_fake_http):
            out = _fetch_result(
                "http://b", "csl-1", sleep_fn=lambda s: None)
        self.assertEqual(out["summary_markdown"], "A")
        self.assertEqual(calls["n"], 3)

    def test_exhausts_and_raises(self):
        with mock.patch(
            "consultants.cli._http",
            side_effect=CLIError("HTTP 404 ...", exit_code=1),
        ):
            with self.assertRaises(CLIError):
                _fetch_result(
                    "http://b", "csl-1", attempts=2,
                    sleep_fn=lambda s: None)


# ----------------------- cmd_consult --wait ---------------------- #

class TestCmdConsultWait(unittest.TestCase):
    def _run(self, statuses, *, result=None, ns=None, capsys_buf=None):
        """Drive cmd_consult with a scripted _http. POST returns the run
        record; GET status pops ``statuses``; GET result returns
        ``result``."""
        q = list(statuses)

        def _fake_http(method, url, *, body=None, timeout=600.0):
            if method == "POST" and url.endswith("/v1/consult"):
                return {"sid": "csl-1", "status": "running"}
            if method == "GET" and url.endswith("/v1/consult/csl-1"):
                return q.pop(0)
            if url.endswith("/v1/consult/csl-1/result"):
                return result or {}
            raise AssertionError(f"unexpected {method} {url}")

        ns = ns or _consult_ns(wait=True, poll_interval=0.01)
        with mock.patch("consultants.cli._http", side_effect=_fake_http), \
                mock.patch("consultants.cli.time.sleep"):
            from io import StringIO
            buf = StringIO()
            with mock.patch("sys.stdout", buf):
                rc = cmd_consult(ns, "http://b")
        return rc, buf.getvalue()

    def test_completed_prints_result(self):
        rc, out = self._run(
            [{"sid": "csl-1", "status": "running"},
             {"sid": "csl-1", "status": "completed"}],
            result={"sid": "csl-1", "summary_markdown": "THE ANSWER",
                    "metadata": {"effort": "high"}})
        self.assertEqual(rc, 0)
        doc = json.loads(out)
        self.assertTrue(doc["ok"])
        self.assertEqual(doc["summary_markdown"], "THE ANSWER")

    def test_failed_prints_not_ok_rc1(self):
        rc, out = self._run(
            [{"sid": "csl-1", "status": "failed",
              "error": "model down"}])
        self.assertEqual(rc, 1)
        doc = json.loads(out)
        self.assertFalse(doc["ok"])
        self.assertEqual(doc["status"], "failed")

    def test_bare_consult_unchanged(self):
        # No --wait → prints the run record, returns 0, never polls.
        def _fake_http(method, url, *, body=None, timeout=600.0):
            self.assertTrue(url.endswith("/v1/consult"))   # POST only
            return {"sid": "csl-1", "status": "running"}

        with mock.patch("consultants.cli._http", side_effect=_fake_http):
            from io import StringIO
            buf = StringIO()
            with mock.patch("sys.stdout", buf):
                rc = cmd_consult(_consult_ns(wait=False), "http://b")
        self.assertEqual(rc, 0)
        doc = json.loads(buf.getvalue())
        self.assertTrue(doc["ok"])
        self.assertEqual(doc["status"], "running")     # not awaited


if __name__ == "__main__":
    unittest.main()
