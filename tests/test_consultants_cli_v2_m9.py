"""M9 CLI subcommand tests.

The CLI handlers are thin: parse argv → call ``_http`` → pretty-print.
We test:

1. **Argv parsing** — every new subparser accepts a representative
   set of arguments and dispatches to the right handler.
2. **HTTP shaping** — each handler sends the right method, URL and
   body to ``_http``. Monkeypatched in tests so we never hit the
   network.
3. **Edge cases** — ``_parse_relative_time`` covers ``+30m`` / ``+2h``
   / bare ``45`` / bad input.

These tests run on the **main** env (no fastapi required) — they
only exercise the client-side cli.py code; the server tests live
in ``test_consultants_v2_control_routes.py``.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock


from consultants.cli import (
    _parse_relative_time,
    build_parser,
    cmd_cancel,
    cmd_control,
    cmd_inject,
    cmd_pause,
    cmd_resume,
    cmd_state,
)
from consultants.cli import CLIError  # type: ignore[attr-defined]


# ============================================================== #
# argv → fn dispatch wiring
# ============================================================== #


class TestArgvDispatch(unittest.TestCase):

    def test_state_subparser(self):
        args = build_parser().parse_args(["state", "csl-1"])
        self.assertEqual(args.fn.__name__, "cmd_state")
        self.assertEqual(args.sid, "csl-1")

    def test_inject_requires_message_or_file(self):
        # Mutually-exclusive group rejects empty.
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["inject", "csl-1"])

    def test_inject_with_message(self):
        args = build_parser().parse_args([
            "inject", "csl-1", "-m", "hello", "--role", "researcher",
        ])
        self.assertEqual(args.role, "researcher")
        self.assertEqual(args.message, "hello")
        self.assertIsNone(args.file)

    def test_inject_with_file(self):
        args = build_parser().parse_args([
            "inject", "csl-1", "-f", "/tmp/x.txt",
        ])
        self.assertEqual(args.file, "/tmp/x.txt")
        self.assertIsNone(args.message)

    def test_control_accepts_many_flags(self):
        args = build_parser().parse_args([
            "control", "csl-1",
            "--time", "+30m",
            "--max-rounds", "5",
            "--confidence", "0.7",
            "--strictness", "strict",
            "--enable", "critic",
            "--enable", "synthesizer",
        ])
        self.assertEqual(args.time, "+30m")
        self.assertEqual(args.max_rounds, 5)
        self.assertEqual(args.confidence, 0.7)
        self.assertEqual(args.strictness, "strict")
        self.assertEqual(args.enable, ["critic", "synthesizer"])

    def test_pause_resume_cancel_events_subparsers(self):
        p = build_parser()
        for argv, fn_name in [
            (["pause", "csl-1"], "cmd_pause"),
            (["resume", "csl-1", "--value", '{"approve": true}'],
             "cmd_resume"),
            (["cancel", "csl-1", "--discard-partial"], "cmd_cancel"),
            (["events", "csl-1", "--since", "5"], "cmd_events"),
        ]:
            args = p.parse_args(argv)
            self.assertEqual(args.fn.__name__, fn_name)


# ============================================================== #
# _parse_relative_time
# ============================================================== #


class TestParseRelativeTime(unittest.TestCase):

    def test_minutes(self):
        before = _parse_relative_time("+30m")
        # Should be ~30 min in the future.
        import time as _t
        delta = before - _t.time()
        self.assertGreater(delta, 30 * 60 - 5)
        self.assertLess(delta, 30 * 60 + 5)

    def test_hours(self):
        import time as _t
        v = _parse_relative_time("+2h")
        delta = v - _t.time()
        self.assertGreater(delta, 2 * 3600 - 5)
        self.assertLess(delta, 2 * 3600 + 5)

    def test_seconds(self):
        import time as _t
        v = _parse_relative_time("+45s")
        delta = v - _t.time()
        self.assertGreater(delta, 40)
        self.assertLess(delta, 50)

    def test_days(self):
        import time as _t
        v = _parse_relative_time("+1d")
        delta = v - _t.time()
        self.assertGreater(delta, 86400 - 5)
        self.assertLess(delta, 86400 + 5)

    def test_bare_number_is_seconds(self):
        import time as _t
        v = _parse_relative_time("120")
        delta = v - _t.time()
        self.assertGreater(delta, 115)
        self.assertLess(delta, 125)

    def test_plus_is_optional(self):
        import time as _t
        v = _parse_relative_time("30m")
        delta = v - _t.time()
        self.assertGreater(delta, 30 * 60 - 5)

    def test_empty_raises(self):
        with self.assertRaises(CLIError):
            _parse_relative_time("")

    def test_garbage_raises(self):
        with self.assertRaises(CLIError):
            _parse_relative_time("+banana")

    def test_zero_raises(self):
        with self.assertRaises(CLIError):
            _parse_relative_time("+0s")

    def test_negative_raises(self):
        # Leading '+' is stripped, then we'd try float("-30") which
        # parses fine, but the post-check rejects.
        with self.assertRaises(CLIError):
            _parse_relative_time("+-30s")


# ============================================================== #
# Handler HTTP shaping (monkeypatch _http)
# ============================================================== #


class TestHandlerHttpShaping(unittest.TestCase):

    def setUp(self):
        self.calls: list[dict] = []

        def _fake_http(method, url, *, body=None, timeout=600.0):
            self.calls.append({
                "method": method, "url": url, "body": body,
            })
            # Return a minimal valid response per endpoint.
            return {"ok": True, "applied": {}, "runtime_control": {}}

        self._patch = mock.patch(
            "consultants.cli._http", side_effect=_fake_http,
        )
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _ns(self, **kw):
        # Build an args namespace with sensible defaults so we
        # don't have to fill the full set at every test.
        defaults = {
            "sid": "csl-1", "role": "any", "message": None,
            "file": None, "source": "user", "time": None,
            "soft_target": None, "max_rounds": None,
            "max_reroutes": None, "confidence": None,
            "strictness": None, "enable": [], "disable": [],
            "reason": None, "value": None, "decision": "",
            "discard_partial": False, "since": None,
        }
        defaults.update(kw)
        return SimpleNamespace(**defaults)

    def test_state_sends_get(self):
        cmd_state(self._ns(), "http://base")
        self.assertEqual(self.calls[0]["method"], "GET")
        self.assertEqual(
            self.calls[0]["url"], "http://base/v1/consult/csl-1/state",
        )

    def test_inject_sends_post_with_text(self):
        cmd_inject(
            self._ns(role="researcher", message="hello"),
            "http://base",
        )
        self.assertEqual(self.calls[0]["method"], "POST")
        self.assertEqual(self.calls[0]["body"]["role"], "researcher")
        self.assertEqual(self.calls[0]["body"]["text"], "hello")

    def test_control_with_max_rounds(self):
        cmd_control(self._ns(max_rounds=7), "http://base")
        body = self.calls[0]["body"]
        self.assertEqual(body["runtime_control"]["max_rounds"], 7)

    def test_control_with_time_sets_deadline(self):
        cmd_control(self._ns(time="+30m"), "http://base")
        body = self.calls[0]["body"]
        self.assertIn("deadline_ts", body["runtime_control"])
        # And it's a float in the near future.
        import time as _t
        self.assertGreater(
            body["runtime_control"]["deadline_ts"], _t.time(),
        )

    def test_control_empty_raises(self):
        with self.assertRaises(CLIError):
            cmd_control(self._ns(), "http://base")

    def test_pause_sends_reason(self):
        cmd_pause(self._ns(reason="thinking"), "http://base")
        self.assertEqual(
            self.calls[0]["body"]["reason"], "thinking",
        )

    def test_resume_decodes_json_value(self):
        cmd_resume(
            self._ns(value='{"approve": true}'),
            "http://base",
        )
        body = self.calls[0]["body"]
        self.assertEqual(body["value"], {"approve": True})

    def test_resume_passes_bare_string(self):
        cmd_resume(self._ns(value="just-a-string"), "http://base")
        body = self.calls[0]["body"]
        # Not valid JSON -> passed through as a literal.
        self.assertEqual(body["value"], "just-a-string")

    def test_cancel_with_discard(self):
        cmd_cancel(self._ns(discard_partial=True), "http://base")
        body = self.calls[0]["body"]
        self.assertTrue(body["discard_partial"])


# ============================================================== #
# cmd_control --disable subtracts from current state
# ============================================================== #


class TestControlDisable(unittest.TestCase):

    def test_disable_subtracts_from_get_state_snapshot(self):
        calls: list[dict] = []

        def _fake_http(method, url, *, body=None, timeout=600.0):
            calls.append({"method": method, "url": url, "body": body})
            if method == "GET" and url.endswith("/state"):
                return {
                    "ok": True,
                    "runtime_control": {
                        "enabled_roles": [
                            "planner", "researcher", "critic",
                            "synthesizer",
                        ],
                    },
                }
            return {"ok": True}

        with mock.patch(
                "consultants.cli._http", side_effect=_fake_http):
            cmd_control(SimpleNamespace(
                sid="csl-1", time=None, soft_target=None,
                max_rounds=None, max_reroutes=None,
                confidence=None, strictness=None, enable=[],
                disable=["critic"],
            ), "http://base")

        # Two calls: GET /state (snapshot) + POST /control.
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0]["url"].endswith("/state"))
        post = calls[1]
        self.assertEqual(post["method"], "POST")
        self.assertEqual(
            post["body"]["runtime_control"]["enabled_roles"],
            ["planner", "researcher", "synthesizer"],
        )


if __name__ == "__main__":
    unittest.main()
