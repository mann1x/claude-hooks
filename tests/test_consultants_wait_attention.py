"""A backgrounded wait is the notification, so it returns when the run
needs the caller, not only when it ends (backup_models #2414).

csl-2026-11-27-0656-503b sat 20 min at the adversary checkpoint while
`follow-up --wait` blocked on: the wait returned only on a terminal
status, so the harness never woke the session that had to answer.

langgraph-free → runs in BOTH envs.
"""
from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from consultants import cli  # noqa: E402
from consultants.cli import (  # noqa: E402
    EXIT_CHANGED, EXIT_NEEDS_ATTENTION, PAUSE_PENDING_GRACE_S, WaitStopped,
    _wait_for_terminal, attention_of, build_parser,
)

RUN = {"sid": "csl-1", "status": "running", "progress": {"planner": "done"}}


def _seq_http(seq, result=None):
    q = list(seq)

    def fake(method, url, *, body=None, timeout=600.0):
        if url.endswith("/result"):
            return result or {"sid": "csl-1", "summary_markdown": "ANSWER"}
        if method == "POST":
            return {"sid": "csl-1", "status": "running"}
        return q.pop(0) if len(q) > 1 else q[0]
    return fake


def _wait(seq, until="attention", clock=None):
    clock = clock or {"t": 0.0}

    def sleep(s):
        clock["t"] += s
    with mock.patch("consultants.cli._http", side_effect=_seq_http(seq)):
        return _wait_for_terminal("http://b", "csl-1", interval=5.0, timeout=0.0,
                                  until=until, sleep_fn=sleep,
                                  now_fn=lambda: clock["t"])


class TestAttentionOf:
    def test_nothing_waiting(self):
        assert attention_of(RUN) is None

    def test_checkpoint(self):
        why = attention_of({**RUN, "adversary_checkpoint_deadline_ts": 99.0})
        assert why["reason"] == "adversary_checkpoint"
        assert why["deadline_ts"] == 99.0 and why["on_timeout"] == "auto-resumes"
        assert why["answer"] == "claude-consultants adversary-ack csl-1"

    def test_tool_approval_names_the_earliest_deadline_and_that_it_denies(self):
        why = attention_of({**RUN, "pending_tool_approvals": [
            {"deadline_ts": 50.0}, {"deadline_ts": 20.0}]})
        assert why["reason"] == "tool_approval"
        assert (why["deadline_ts"], why["on_timeout"], why["pending"]) == (20.0, "denied", 2)

    def test_a_parked_pause(self):
        why = attention_of({**RUN, "pause_state": "parked", "paused_roles": ["critic"]})
        assert why["reason"] == "paused" and why["roles"] == ["critic"]

    def test_a_pending_pause_only_after_the_grace(self):
        rec = {**RUN, "pause_state": "pending", "pause_blocked_by": "x"}
        assert attention_of(rec, pending_for=5.0) is None
        assert attention_of(rec, pending_for=PAUSE_PENDING_GRACE_S)["reason"] == \
            "pause_not_taking_effect"


class TestWaitReturnsOnAttention:
    def test_the_checkpoint_stops_the_wait(self):
        with pytest.raises(WaitStopped) as e:
            _wait([RUN, RUN, {**RUN, "adversary_checkpoint_deadline_ts": 9.0}])
        assert e.value.exit_code == EXIT_NEEDS_ATTENTION
        assert e.value.why["reason"] == "adversary_checkpoint"

    def test_terminal_keeps_the_old_behaviour(self):
        rec = _wait([{**RUN, "adversary_checkpoint_deadline_ts": 9.0},
                     {**RUN, "status": "completed"}], until="terminal")
        assert rec["status"] == "completed"

    def test_a_pending_pause_is_reported_once_it_has_lasted(self):
        pend = {**RUN, "pause_state": "pending", "pause_blocked_by": "x"}
        clock = {"t": 0.0}
        with pytest.raises(WaitStopped) as e:
            _wait([pend], clock=clock)
        assert e.value.why["reason"] == "pause_not_taking_effect"
        assert clock["t"] >= PAUSE_PENDING_GRACE_S

    def test_any_change_returns_on_progress(self):
        later = {**RUN, "progress": {"planner": "done", "researcher": "in_progress"}}
        with pytest.raises(WaitStopped) as e:
            _wait([RUN, RUN, later], until="any-change")
        assert e.value.exit_code == EXIT_CHANGED

    def test_attention_ignores_plain_progress(self):
        later = {**RUN, "progress": {"planner": "done", "researcher": "done"}}
        rec = _wait([RUN, later, {**later, "status": "completed"}])
        assert rec["status"] == "completed"


def _run_cli(argv, seq):
    args = build_parser().parse_args(argv)
    buf = StringIO()
    with mock.patch("consultants.cli._http", side_effect=_seq_http(seq)), \
            mock.patch("consultants.cli.time.sleep"), mock.patch("sys.stdout", buf):
        rc = args.fn(args, "http://b")
    return rc, json.loads(buf.getvalue())


class TestCli:
    def test_follow_up_wait_exits_10_with_the_reason(self):
        # The 503b shape: follow-up --wait, then the checkpoint opens.
        rc, doc = _run_cli(["follow-up", "csl-0", "--message", "q", "--wait"],
                           [RUN, {**RUN, "adversary_checkpoint_deadline_ts": 9.0}])
        assert rc == EXIT_NEEDS_ATTENTION
        assert doc["ok"] is False and doc["needs_attention"] is True
        assert doc["reason"] == "adversary_checkpoint"
        assert doc["answer"] == "claude-consultants adversary-ack csl-1"
        assert doc["rewait"] == "claude-consultants wait csl-1"

    def test_consult_wait_until_terminal(self):
        rc, doc = _run_cli(["consult", "--message", "q", "--wait", "--wait-until", "terminal"],
                           [{**RUN, "adversary_checkpoint_deadline_ts": 9.0},
                            {**RUN, "status": "completed"}])
        assert rc == 0 and doc["summary_markdown"] == "ANSWER"

    def test_wait_verb_collects_the_result(self):
        rc, doc = _run_cli(["wait", "csl-1"], [RUN, {**RUN, "status": "completed"}])
        assert rc == 0 and doc["ok"] is True and doc["summary_markdown"] == "ANSWER"

    def test_wait_verb_on_a_failed_run(self):
        rc, doc = _run_cli(["wait", "csl-1"], [{**RUN, "status": "failed"}])
        assert rc == 1 and doc["ok"] is False

    def test_wait_verb_has_no_wait_flag(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["wait", "csl-1", "--wait"])


class TestCallersThatCannotAnswer:
    """The Workflow driver's agent and the bench cannot answer a
    checkpoint; they must keep waiting for the end."""

    @pytest.mark.parametrize("rel", [
        ".claude/workflows/consult-with-adversarial-review.mjs",
        "benchmarks/consultants/role_tools_bench.py",
    ])
    def test_pinned_to_terminal(self, rel):
        src = (Path(__file__).resolve().parent.parent / rel).read_text(encoding="utf-8")
        waits = src.count("--wait --") + src.count('"--wait",')
        pinned = src.count("--wait --wait-until terminal") + \
            src.count('"--wait", "--wait-until", "terminal"')
        assert waits >= 1 and pinned == waits


def test_namespace_without_wait_until_defaults_to_attention():
    # Callers that build args by hand (tests, Workflows) get the new default.
    ns = SimpleNamespace(poll_interval=0.01, wait_timeout=0.0)
    with mock.patch("consultants.cli._http", side_effect=_seq_http(
            [{**RUN, "adversary_checkpoint_deadline_ts": 9.0}])), \
            mock.patch("sys.stdout", StringIO()):
        assert cli._consult_wait(ns, "http://b", {"sid": "csl-1"}) == EXIT_NEEDS_ATTENTION
