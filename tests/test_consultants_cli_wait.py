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
    cmd_follow_up,
    cmd_grant,
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


class TestWaitAcrossEngineRestart(unittest.TestCase):
    """The engine suspends running councils on shutdown and the next one
    resumes them, so a wait must ride out the gap instead of failing on
    the first refused connection."""

    def _down(self):
        return CLIError("Could not reach http://b", unreachable=True)

    def test_an_outage_is_retried_and_the_run_collected(self):
        seq = [{"status": "running"}, self._down(), self._down(),
               {"status": "suspended"}, {"status": "completed"}]

        def _fake_http(method, url, *, body=None, timeout=600.0):
            item = seq.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        with mock.patch("consultants.cli._http", side_effect=_fake_http):
            rec = _wait_for_terminal("http://b", "csl-1", interval=1.0,
                                     timeout=0.0, sleep_fn=lambda s: None)
        self.assertEqual(rec["status"], "completed")
        self.assertEqual(seq, [])

    def test_an_engine_that_stays_down_still_fails(self):
        clock = {"t": 0.0}

        def _sleep(s):
            clock["t"] += s

        with mock.patch("consultants.cli._http", side_effect=self._down()):
            with self.assertRaises(CLIError) as ctx:
                _wait_for_terminal("http://b", "csl-1", interval=30.0,
                                   timeout=0.0, sleep_fn=_sleep,
                                   now_fn=lambda: clock["t"])
        self.assertTrue(ctx.exception.unreachable)
        self.assertGreaterEqual(clock["t"], 300.0)

    def test_an_http_error_is_not_retried(self):
        with mock.patch("consultants.cli._http",
                        side_effect=CLIError("HTTP 404 from x")):
            with self.assertRaises(CLIError):
                _wait_for_terminal("http://b", "csl-1", interval=1.0,
                                   timeout=0.0, sleep_fn=lambda s: None)


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


# ----------------------- cmd_follow_up --wait -------------------- #

def _follow_ns(**kw):
    defaults = {
        "parent_sid": "csl-0", "message": "and the sunset?",
        "cwd": None, "effort": None, "add_dir": [], "trace": None,
        "skip_preflight": False, "allow_extra": None, "force": False,
        "wait": False, "poll_interval": 2.0, "wait_timeout": 0.0,
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


class TestCmdFollowUpWait(unittest.TestCase):
    """The skill waits on follow-ups with ``follow-up --wait``; the
    parser once accepted the flag only on ``consult``, so every
    documented follow-up failed before it started."""

    def _run(self, post, statuses=(), result=None, ns=None):
        q = list(statuses)
        calls = []

        def _fake_http(method, url, *, body=None, timeout=600.0):
            calls.append((method, url))
            if method == "POST" and url.endswith("/follow-up"):
                return post
            if method == "GET" and url.endswith("/v1/consult/csl-2"):
                return q.pop(0)
            if url.endswith("/v1/consult/csl-2/result"):
                return result or {}
            raise AssertionError(f"unexpected {method} {url}")

        ns = ns or _follow_ns(wait=True, poll_interval=0.01)
        with mock.patch("consultants.cli._http", side_effect=_fake_http), \
                mock.patch("consultants.cli.time.sleep"):
            from io import StringIO
            buf = StringIO()
            with mock.patch("sys.stdout", buf):
                rc = cmd_follow_up(ns, "http://b")
        return rc, json.loads(buf.getvalue()), calls

    def test_parser_accepts_wait_on_follow_up(self):
        args = build_parser().parse_args([
            "follow-up", "csl-0", "--message", "q?",
            "--wait", "--poll-interval", "0.5", "--wait-timeout", "60"])
        self.assertTrue(args.wait)
        self.assertEqual((args.poll_interval, args.wait_timeout), (0.5, 60.0))

    def test_waits_on_the_new_sid_and_prints_its_result(self):
        rc, doc, _ = self._run(
            {"sid": "csl-2", "status": "running", "parent_sid": "csl-0"},
            [{"sid": "csl-2", "status": "running"},
             {"sid": "csl-2", "status": "completed"}],
            result={"sid": "csl-2", "summary_markdown": "FOLLOW ANSWER"})
        self.assertEqual(rc, 0)
        self.assertEqual(doc["summary_markdown"], "FOLLOW ANSWER")

    def test_cap_refusal_is_passed_through_not_waited_on(self):
        rc, doc, calls = self._run(
            {"ok": False, "reason": "followup_limit_reached",
             "sid": None, "parent_sid": "csl-0"})
        self.assertFalse(doc["ok"])
        self.assertEqual(doc["reason"], "followup_limit_reached")
        self.assertEqual([m for m, _ in calls], ["POST"], "never polled")


class TestCmdGrant(unittest.TestCase):
    """``grant <sid> [N]`` raises the cap now, without a followup."""

    def _run(self, argv):
        calls = []

        def _fake_http(method, url, *, body=None, timeout=600.0):
            calls.append((method, url, body))
            return {"ok": True, "granted": (body or {}).get("allow_extra")}

        args = build_parser().parse_args(argv)
        self.assertIs(args.fn, cmd_grant)
        with mock.patch("consultants.cli._http", side_effect=_fake_http), \
                mock.patch("sys.stdout"):
            self.assertEqual(args.fn(args, "http://x"), 0)
        return calls

    def test_posts_n_to_the_grant_route(self):
        (method, url, body), = self._run(
            ["grant", "csl-1", "6", "--cwd", "/proj"])
        self.assertEqual((method, url), ("POST",
                                         "http://x/v1/consult/csl-1/grant"))
        self.assertEqual(body["allow_extra"], 6)

    def test_bare_grant_leaves_the_size_to_the_engine(self):
        (_, _, body), = self._run(["grant", "csl-1", "--cwd", "/proj"])
        self.assertNotIn("allow_extra", body)


def _options_under(parser) -> set:
    """Every option string ``parser`` or any nested subcommand accepts
    (``config set-store --enabled`` counts as a ``config`` flag)."""
    out = set()
    for a in parser._actions:
        out.update(a.option_strings)
        choices = getattr(a, "choices", None)
        for sub in (choices.values() if isinstance(choices, dict) else ()):
            if hasattr(sub, "_actions"):
                out |= _options_under(sub)
    return out


class TestSkillFlagsExist(unittest.TestCase):
    """Every ``<verb> --flag`` the consultants skill tells a session to
    run must be a flag that verb's parser accepts. A documented flag
    the CLI rejects fails the call before the council starts."""

    def test_documented_flags_parse(self):
        import re
        skill = (Path(__file__).resolve().parent.parent / ".claude" /
                 "skills" / "consultants" / "SKILL.md").read_text(
                     encoding="utf-8")
        parser = build_parser()
        subs = next(a for a in parser._actions
                    if a.__class__.__name__ == "_SubParsersAction")
        verbs = subs.choices
        missing = []
        for m in re.finditer(
                r"(?<![\w-])(" + "|".join(map(re.escape, verbs)) +
                r")((?:[ \t]+(?:\S+))*)", skill):
            verb, rest = m.group(1), m.group(2)
            known = _options_under(verbs[verb])
            for flag in re.findall(r"(?<!\S)(--[a-z][a-z-]*)", rest):
                if flag not in known:
                    missing.append(f"{verb} {flag}")
        self.assertEqual(sorted(set(missing)), [])


if __name__ == "__main__":
    unittest.main()
