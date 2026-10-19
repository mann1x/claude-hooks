"""Typed records between council roles (plan Milestone G1-G3).

Covers the records (parse, take, render back to each role's prose), the
agent loop's result tools, the single-shot and tool-loop paths, the
node wiring behind ``runtime_control.typed_protocol``, the degrade
paths, and parity: with the flag off nothing changes.
"""

from __future__ import annotations

import json

import pytest

from claude_hooks.agent_loop.runner import LoopConfig, run_loop
from consultants import config as cc
from consultants.engine import council
from consultants.engine import typed_results as tr
from consultants.engine.control import runtime_control_defaults


def _call(name: str, args, *, call_id="c1"):
    return {"id": call_id, "type": "function",
            "function": {"name": name,
                         "arguments": args if isinstance(args, str)
                         else json.dumps(args)}}


def _resp(text="", calls=None, done_reason="stop"):
    msg = {"role": "assistant", "content": text}
    if calls:
        msg["tool_calls"] = calls
    return {"choices": [{"message": msg,
                         "finish_reason": "tool_calls" if calls else done_reason,
                         "done_reason": done_reason}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                      "total_tokens": 15},
            "done_reason": done_reason}


class FakeChat:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[dict] = []

    def chat(self, payload):
        self.calls.append(json.loads(json.dumps(payload)))
        return self.replies.pop(0)


VERDICT = {"decision": "needs_more_research", "why": "One cite is unverified.",
           "gaps": [{"what": "retry cap value", "cite": "a.py:10"}],
           "corrections": [{"report": 2, "claimed": "MAX = 5", "actual": "MAX = 3",
                            "cite": "b.py:4"}],
           "confidence": 0.62}


# ------------------------------------------------------------ records

def test_verdict_renders_the_prose_the_router_and_escalator_read():
    rec = tr.parse(tr.VERDICT, VERDICT)
    text = tr.render(tr.VERDICT, rec)
    assert text.splitlines()[0] == "DECISION: needs_more_research"
    assert "- gap_named: retry cap value (`a.py:10`)" in text
    assert "CORRECTIONS:\n- report 2: claimed MAX = 5 — actual MAX = 3 (`b.py:4`)" in text
    assert council.parse_critic_decision(text) == "needs_more_research"
    stripped, conf = council._extract_confidence(text)
    assert conf == pytest.approx(0.62) and "CONFIDENCE" not in stripped
    from consultants.engine.escalation import _critic_named_gap
    assert _critic_named_gap({"critic_decision": "needs_more_research",
                              "critique": stripped})


def test_report_render_keeps_literal_path_line_for_the_linter():
    rec = tr.parse(tr.REPORT, {"summary": "Retry is bounded.",
                               "claims": [{"claim": "cap is 15", "path": "x.py",
                                           "line": 39, "symbol": "MAX"},
                                          {"claim": "no line", "path": "y.py"},
                                          {"claim": "", "path": "z.py"}],
                               "gaps": ["did not read z.py"]})
    text = tr.render(tr.REPORT, rec)
    assert "- cap is 15 (`x.py:39`) [`MAX`]" in text
    assert "- no line (`y.py`)" in text
    assert "z.py" in text and "Evidence insufficient:" in text
    assert len(rec["claims"]) == 2


def test_plan_and_refutation_render_to_their_parsers():
    plan = tr.render(tr.PLAN, tr.parse(tr.PLAN, {"steps": ["Read a.py", "Grep b"]}))
    assert council.parse_plan_items(plan) == ["Read a.py", "Grep b"]
    none = tr.render(tr.REFUTATION, tr.parse(tr.REFUTATION, {"issues": []}))
    assert council.parse_adversary_refutation(none) == ("none", "")
    some = tr.render(tr.REFUTATION, tr.parse(
        tr.REFUTATION, {"issues": [{"claim": "X", "why": "no source"}]}))
    assert council.parse_adversary_refutation(some)[0] == "issues"


def test_bad_records_are_refused_not_repaired():
    assert tr.parse(tr.VERDICT, {"decision": "maybe", "why": "", "confidence": 1}) is None
    assert tr.parse(tr.VERDICT, "{not json") is None
    assert tr.parse(tr.PLAN, {"steps": []}) is None
    assert tr.parse(tr.REPORT, {"summary": "", "claims": []}) is None


def test_long_text_is_cut_at_render_and_counts_are_clamped():
    rec = tr.parse(tr.VERDICT, {**VERDICT, "why": "w" * 5000,
                                "gaps": [{"what": "g"}] * 10})
    assert len(rec["why"]) <= tr.TEXT_MAX and len(rec["gaps"]) == 3


def test_schemas_never_carry_maxlength():
    # maxLength broke xOllama's grammar compiler (55d4baf70).
    for name in (tr.PLAN, tr.REPORT, tr.VERDICT, tr.REFUTATION):
        assert "maxLength" not in json.dumps(tr.result_tool_spec(name))


def test_take_statuses():
    ok = _resp(calls=[_call(tr.VERDICT, VERDICT)])
    assert tr.take(tr.VERDICT, ok)[1] == "taken"
    assert tr.take(tr.VERDICT, _resp("prose"))[1] == "absent"
    mixed = _resp(calls=[_call(tr.VERDICT, VERDICT), _call("read_file", {"path": "a"}, call_id="c2")])
    assert tr.take(tr.VERDICT, mixed)[1] == "mixed"
    cut = _resp(calls=[_call(tr.VERDICT, '{"decision": "rea')], done_reason="length")
    assert tr.take(tr.VERDICT, cut)[1] == "cut"


# ------------------------------------------------------------ the loop

def _loop(replies, *, result=(tr.REPORT,)):
    chat = FakeChat(replies)
    ran = []

    def execute(name, args, cwd):
        ran.append(name)
        return "file text"

    cfg = LoopConfig(max_iterations=6, force_first_tool_call=False,
                     result_tools=result,
                     result_not_alone_message=tr.NOT_ALONE,
                     result_invalid_message=tr.CUT_OR_INVALID)
    final = run_loop({"model": "m", "messages": [{"role": "user", "content": "q"}]},
                     "/tmp", config=cfg,
                     tool_specs=[{"type": "function", "function": {"name": "read_file"}},
                                 tr.result_tool_spec(tr.REPORT)],
                     chat_fn=chat.chat, tool_executor=execute)
    return final, chat, ran


REPORT = {"summary": "s", "claims": [{"claim": "c", "path": "a.py", "line": 1}]}


def test_loop_ends_on_a_lone_result_call_and_never_executes_it():
    final, chat, ran = _loop([
        _resp(calls=[_call("read_file", {"path": "a.py"})]),
        _resp(calls=[_call(tr.REPORT, REPORT, call_id="c2")]),
    ])
    assert ran == ["read_file"]
    assert final["_result_call"]["name"] == tr.REPORT


def test_result_beside_reads_is_not_taken_until_alone():
    final, chat, ran = _loop([
        _resp(calls=[_call("read_file", {"path": "a.py"}),
                     _call(tr.REPORT, REPORT, call_id="c2")]),
        _resp(calls=[_call(tr.REPORT, REPORT, call_id="c3")]),
    ])
    assert ran == ["read_file"]
    second = chat.calls[1]["messages"]
    answered = [m for m in second if m.get("role") == "tool"]
    assert any("Not taken yet" in m["content"] for m in answered)
    assert final["_result_call"]["name"] == tr.REPORT


def test_invalid_result_args_are_asked_for_again_once():
    final, chat, ran = _loop([
        _resp(calls=[_call(tr.REPORT, '{"summary": "cut')], done_reason="length"),
        _resp(calls=[_call(tr.REPORT, REPORT, call_id="c2")]),
    ])
    assert "cut off" in chat.calls[1]["messages"][-1]["content"]
    assert final["_result_call"]["arguments"]


def test_forced_answer_keeps_only_the_result_tool():
    chat = FakeChat([_resp(calls=[_call("read_file", {"path": "a"})]),
                     _resp(calls=[_call(tr.REPORT, REPORT, call_id="c2")])])
    cfg = LoopConfig(max_iterations=4, force_answer_after=1,
                     force_first_tool_call=False, result_tools=(tr.REPORT,))
    run_loop({"model": "m", "messages": []}, "/tmp", config=cfg,
             tool_specs=[{"type": "function", "function": {"name": "read_file"}},
                         tr.result_tool_spec(tr.REPORT)],
             chat_fn=chat.chat, tool_executor=lambda n, a, c: "x")
    names = [t["function"]["name"] for t in chat.calls[1]["tools"]]
    assert names == [tr.REPORT]


def test_loop_without_result_tools_is_unchanged():
    chat = FakeChat([_resp(calls=[_call(tr.REPORT, REPORT)]), _resp("done")])
    ran = []
    final = run_loop({"model": "m", "messages": []}, "/tmp",
                     config=LoopConfig(max_iterations=3, force_first_tool_call=False),
                     tool_specs=[tr.result_tool_spec(tr.REPORT)], chat_fn=chat.chat,
                     tool_executor=lambda n, a, c: ran.append(n) or "x")
    assert ran == [tr.REPORT] and "_result_call" not in final


# ------------------------------------------------------------ nodes

def _critic_state(typed: bool):
    state = council.initial_state(question="q", cwd="/p", models={"critic": "m"},
                                  topology="council", effort="high")
    state.update(plan="1. look", research=["- r (`a.py:1`)"], research_rounds_used=1,
                 critic_reroutes_used=0)
    state["runtime_control"] = {"typed_protocol": True} if typed else {}
    return state


def test_critic_typed_verdict_routes_like_prose():
    chat = FakeChat([_resp(calls=[_call(tr.VERDICT, VERDICT)])])
    up = council.critic_node(_critic_state(True), chat_client=chat, model="m")
    assert up["critic_decision"] == "needs_more_research"
    assert "gap_named: retry cap value" in up["critique"]
    assert "CONFIDENCE" not in up["critique"]
    assert up["confidence"] == [pytest.approx(0.62)]
    sent = chat.calls[0]
    assert [t["function"]["name"] for t in sent["tools"]] == [tr.VERDICT]
    system = next(m for m in sent["messages"] if m["role"] == "system")["content"]
    assert f"`{tr.VERDICT}`" in system
    user = next(m for m in sent["messages"] if m["role"] == "user")["content"]
    assert user.startswith(council.CLAIMS_FRAMING)


def test_critic_typed_degrades_to_prose_when_no_record():
    chat = FakeChat([_resp("DECISION: ready\nFine.\nCONFIDENCE: 0.9")])
    up = council.critic_node(_critic_state(True), chat_client=chat, model="m")
    assert up["critic_decision"] == "ready"


def test_critic_invalid_record_reasked_then_prose():
    chat = FakeChat([_resp(calls=[_call(tr.VERDICT, '{"decision": "x"}')]),
                     _resp(calls=[_call(tr.VERDICT, '{"decision": "y"}')]),
                     _resp("DECISION: ready\nok")])
    up = council.critic_node(_critic_state(True), chat_client=chat, model="m")
    assert up["critic_decision"] == "ready"
    assert "tools" not in chat.calls[2]  # the last answer is plain prose
    assert chat.calls[1]["messages"][-1]["role"] == "tool"


def test_parity_flag_off_sends_no_result_tool_and_no_framing():
    chat = FakeChat([_resp("DECISION: ready\nok")])
    council.critic_node(_critic_state(False), chat_client=chat, model="m")
    sent = chat.calls[0]
    assert "tools" not in sent
    user = next(m for m in sent["messages"] if m["role"] == "user")["content"]
    assert not user.startswith(council.CLAIMS_FRAMING)
    system = next(m for m in sent["messages"] if m["role"] == "system")["content"]
    assert "RESULT." not in system


def test_runtime_control_seed_carries_flag_only_when_on():
    cfg = cc.ConsultantsConfig()
    assert "typed_protocol" not in runtime_control_defaults(cfg, effort="medium")
    cfg.protocol.typed = True
    assert runtime_control_defaults(cfg, effort="medium")["typed_protocol"] is True


def test_protocol_cli_roundtrip(tmp_path, monkeypatch, capsys):
    from consultants import cli
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    assert cli.main(["config", "set-protocol", "--typed", "on", "--user"]) == 0
    assert json.loads(capsys.readouterr().out)["protocol"] == {"typed": True}
    assert cc.load_config(tmp_path).protocol.typed is True


# ------------------------------------------------------------ G4: claims at the source

SRC = ("import os\n"                       # 1
       "\n"                                # 2
       "def foo(x):\n"                     # 3
       "    if x is None:\n"               # 4
       "        return None\n"             # 5
       "    total = x + 1\n"               # 6
       "    return total\n"                # 7
       "\n"                                # 8
       "def bar():\n"                      # 9
       "    return foo(2)\n")              # 10


def _claims(tmp_path, *claims):
    (tmp_path / "a.py").write_text(SRC)
    rec = {"summary": "s", "claims": [dict(c) for c in claims], "gaps": []}
    return tr.verify_claims(rec, [str(tmp_path)])["claims"]


def test_claims_that_hold_are_left_alone(tmp_path):
    out = _claims(tmp_path,
                  {"claim": "foo adds one", "path": "a.py", "line": 6,
                   "symbol": "foo", "quote": "total = x + 1"},
                  {"claim": "whole file", "path": "a.py"},
                  # foo encloses line 7 though its name is 4 lines up
                  {"claim": "foo returns", "path": "a.py", "line": 7,
                   "symbol": "foo()"})
    assert not any("unverified" in c for c in out)


def test_each_field_that_fails_says_why(tmp_path):
    out = _claims(tmp_path,
                  {"claim": "c", "path": "gone.py", "line": 1},
                  {"claim": "c", "path": "a.py", "line": 40},
                  {"claim": "c", "path": "a.py", "line": 5, "symbol": "bar"},
                  {"claim": "c", "path": "a.py", "line": 4,
                   "quote": "return foo(2)"},
                  {"claim": "c", "path": "a.py", "line": 6,
                   "quote": "total = x * 2"})
    notes = [c.get("unverified") for c in out]
    assert notes == ["file not found", "file has 10 lines",
                     "`bar` is not at that line",
                     "the quote is at line 10",
                     "the quote is not in the file"]


def test_no_roots_checks_nothing(tmp_path):
    rec = {"summary": "s", "claims": [{"claim": "c", "path": "gone.py"}]}
    assert tr.verify_claims(rec, []) is rec


def test_a_failed_check_renders_beside_its_cite(tmp_path):
    out = _claims(tmp_path, {"claim": "c", "path": "a.py", "line": 5,
                             "symbol": "bar"})
    text = tr.render(tr.REPORT, {"summary": "s", "claims": out, "gaps": []})
    assert "(`a.py:5` [unverified — `bar` is not at that line])" in text


def test_researcher_typed_report_is_checked_not_relinted(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text(SRC)
    report = {"summary": "foo guards None.", "claims": [
        # The prose names `bar`, the record names foo, which holds: the
        # regex linter would have written "[in foo, not bar]" here.
        {"claim": "the `bar` path returns early", "path": "src/a.py", "line": 4,
         "symbol": "foo"},
        {"claim": "bar is at the top", "path": "src/a.py", "line": 1,
         "symbol": "bar"}]}
    chat = FakeChat([_resp(calls=[_call("read_file", {"path": "src/a.py"})]),
                     _resp(calls=[_call(tr.REPORT, report, call_id="c2")])])
    state = council.initial_state(question="q", cwd=str(tmp_path),
                                  models={"researcher": "m"},
                                  topology="council", effort="high")
    state.update(plan="1. look at a.py", research_rounds_used=0)
    state["runtime_control"] = {"typed_protocol": True}
    up = council.researcher_node(
        state, chat_client=chat, model="m", cwd=str(tmp_path),
        tool_specs=[{"type": "function", "function": {
            "name": "read_file", "parameters": {"type": "object"}}}],
        tool_executor=lambda n, a, c: SRC, grounding_msgs=[])
    text = up["research"][0]
    assert "(`src/a.py:4`) [`foo`]" in text
    assert "[in " not in text                       # not re-linted
    assert text.count("[unverified — `bar` is not at that line]") == 1


# ------------------------------------------------------------ G5: stop on no progress

def _rounds(*cites_per_round):
    reports, lanes = [], []
    for rnd, cites in enumerate(cites_per_round, start=1):
        reports.append(" ".join(f"(`{c}`)" for c in cites) or "nothing")
        lanes.append({"lane": 0, "round": rnd})
    return reports, lanes


def test_barren_rounds_count_trailing_rounds_with_no_new_cite():
    assert council.barren_rounds(*_rounds(["src/a.py:1"], ["src/a.py:2"])) == 0
    assert council.barren_rounds(*_rounds(["src/a.py:1"], ["src/a.py:1"])) == 1
    assert council.barren_rounds(*_rounds(["src/a.py:1"], ["src/a.py:1"], [])) == 2
    assert council.barren_rounds(*_rounds(["src/a.py:1"], [], ["src/b.py:3"])) == 0
    reports, lanes = _rounds(["src/a.py:1"], ["src/a.py:1"], ["src/a.py:1"])
    assert council.barren_rounds(reports, lanes[:2]) == 0    # misaligned


def _stalled_state(typed: bool):
    reports, lanes = _rounds(["src/a.py:1"], ["src/a.py:1"], ["src/a.py:1"])
    st = {"critic_decision": "needs_more_research", "effort": "max",
          "research_rounds_used": 3, "critic_reroutes_used": 0,
          "research": reports, "research_lanes": lanes,
          "runtime_control": {"max_rounds": 9, "max_reroutes": 9}}
    if typed:
        st["runtime_control"]["typed_protocol"] = True
    return st


def test_two_barren_rounds_overrule_the_reroute_on_the_typed_protocol():
    assert council.route_after_critic(_stalled_state(True)) == \
        council.ROUTE_SYNTHESIZER
    assert council.route_after_critic(_stalled_state(False)) == \
        council.ROUTE_RESEARCHER                              # parity
