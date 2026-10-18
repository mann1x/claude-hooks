"""Fixes from council csl-2026-10-18-1033-5a67 (report #1620, #1621):
the critic's evidence bar (4), the researcher's room with a shell (5) and
the synthesizer's verified / unverified rule (6)."""
from __future__ import annotations

from consultants.engine import council

RUN = {"type": "function", "function": {"name": "run_command"}}
READ = {"type": "function", "function": {"name": "read_file"}}


def test_the_critic_has_an_evidence_bar():
    p = council.CRITIC_SYSTEM
    assert "EVIDENCE BAR" in p and "UNVERIFIED:" in p
    assert "deliberate" in p                       # #1621: read the comment
    assert "tests were not run" in p
    assert "UNVERIFIED:" in council.META_CRITIC_SYSTEM


def test_the_synthesizer_ranks_only_what_was_checked():
    p = council.SYNTHESIZER_SYSTEM
    assert "VERIFIED vs UNVERIFIED" in p
    assert "never ranked, never ✓" in p and "not tested" in p


def test_a_lane_with_the_shell_gets_more_room():
    caps = council.caps_for("xhigh")
    assert council.researcher_iters(caps, [READ]) == (30, 24)
    assert council.researcher_iters(caps, [READ, RUN]) == (45, 39)
    assert council.researcher_iters(council.caps_for("medium"), [RUN]) == (18, 16)
    assert council.researcher_iters(council.caps_for("max"), [RUN]) == (90, 84)


def test_the_researcher_loop_gets_those_caps(monkeypatch):
    from types import SimpleNamespace
    seen = {}

    def loop(payload, cwd, *, config, **kw):
        seen["check"] = config.report_check
        seen["iters"] = config.max_iterations
        seen["force"] = config.force_answer_after
        return {"choices": [{"message": {"role": "assistant", "content": "r a.py:1"},
                             "finish_reason": "stop"}]}
    s = council.initial_state(question="q", cwd="/p", models={"researcher": "m"},
                              topology="council", effort="xhigh")
    s["plan"] = "p"
    council.researcher_node(s, chat_client=SimpleNamespace(chat=lambda p: {}),
                            tool_executor=lambda *a, **k: "", tool_specs=[READ, RUN],
                            grounding_msgs=[], model="m", cwd="/p", loop_runner=loop)
    assert (seen["iters"], seen["force"]) == (45, 39)
    # council run 4: a shell lane's report is checked against its runs
    from consultants.engine.evidence import no_run_report_check
    assert seen["check"] is no_run_report_check
    council.researcher_node(s, chat_client=SimpleNamespace(chat=lambda p: {}),
                            tool_executor=lambda *a, **k: "", tool_specs=[READ],
                            grounding_msgs=[], model="m", cwd="/p", loop_runner=loop)
    assert seen["check"] is None
