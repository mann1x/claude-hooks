"""Plan council-memory M5: a critic not convinced challenges the claims it
doubts; each challenged lane answers in its own conversation, then the
critic rules, and only an open challenge reroutes."""
from __future__ import annotations

import json
import re
import threading

import pytest

pytest.importorskip("langgraph.graph")

from consultants.engine import council, cross_exam as cx, graph  # noqa: E402
from consultants.engine.graph import GraphDeps, build_council_graph  # noqa: E402
from consultants.engine.recorder import (  # noqa: E402
    MessageRecorder, RecorderMeta, load_lane_thread)

PLAN = "1. read a\n2. read b\n3. read c"


def _reply(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


class _Researcher:
    def __init__(self, answer="ANSWER report=2: settled — C1 printed it"):
        self.payloads, self.answer = [], answer
        self._lock = threading.Lock()

    def chat(self, payload, *, think=True):
        with self._lock:
            self.payloads.append(payload)
        last = payload["messages"][-1]["content"]
        if cx.LANE_NOTE in last:
            return _reply(self.answer)
        item = re.search(r"read [abc]", last)
        return _reply(f"FINDING about {item.group(0) if item else 'it'}")


class _Critic:
    def __init__(self, ruling="stands", after="DECISION: ready"):
        self.payloads, self.ruling, self.after, self.verdicts = [], ruling, after, 0
        self._lock = threading.Lock()

    def chat(self, payload, *, think=True):
        with self._lock:
            self.payloads.append(payload)
            if payload["messages"][0]["content"].startswith(cx.RULING_SYSTEM):
                return _reply(f"RULING report=2: {self.ruling} — the run shows it\n"
                              "DECISION: ready")
            self.verdicts += 1
            if self.verdicts == 1:
                return _reply("Report 2 overclaims.\nCHALLENGE report=2: the "
                              "edit is called blind. Settle with: a run.\n"
                              "DECISION: needs_more_research")
            return _reply(self.after)


class _Synth:
    def __init__(self):
        self.payloads = []

    def chat(self, payload, *, think=True):
        self.payloads.append(payload)
        return _reply("THE ANSWER")


def _run(tmp_path, *, on=True, critic=None, researcher=None):
    roles = ("planner", "researcher", "critic", "synthesizer")
    clients = {"planner": _Synth(), "researcher": researcher or _Researcher(),
               "critic": critic or _Critic(), "synthesizer": _Synth()}
    clients["planner"].chat = lambda payload, **kw: _reply(PLAN)
    rec = MessageRecorder(tmp_path / "transcript.db", meta=RecorderMeta(
        sid="csl-cx", cwd=str(tmp_path), question="q", effort="high",
        topology="council", models={}))
    deps = GraphDeps(chat_clients=clients, models={r: "m" for r in roles},
                     enabled_roles=roles, cwd=str(tmp_path),
                     tool_executor=lambda *a, **kw: "", tool_specs=[],
                     grounding_msgs=[], disable_cache=True, recorder=rec,
                     cross_examination=on)
    state = council.initial_state(question="the question", cwd=str(tmp_path),
                                  models={}, topology="council", effort="high")
    # Three fanned lanes count three rounds: room for a reroute.
    state["runtime_control"] = {"max_rounds": 10, "max_reroutes": 3}
    if on:
        state["runtime_control"]["cross_examination"] = True
    g = build_council_graph(deps)
    out = g.invoke(state)
    rec.close()
    return g, clients, out


@pytest.fixture(autouse=True)
def _one_item_a_lane(monkeypatch):
    monkeypatch.setattr(council, "FANOUT_MAX_LANES", 3)


def _exams(stub):
    return [p for p in stub.payloads if cx.LANE_NOTE in p["messages"][-1]["content"]]


class TestOnTheGraph:

    def test_the_challenged_lane_answers_in_its_own_conversation(self, tmp_path):
        g, clients, out = _run(tmp_path)
        assert graph.CROSS_EXAM_NODE in g.get_graph().nodes
        report2 = out["research"][1]
        item = re.search(r"read [abc]", report2).group(0)
        [ex] = _exams(clients["researcher"])
        msgs = ex["messages"]
        assert item in msgs[1]["content"]                 # its own prompt
        assert msgs[-2]["content"] == report2.strip() or item in msgs[-2]["content"]
        assert "CHALLENGE report=2: the edit is called blind" in msgs[-1]["content"]

    def test_a_claim_that_stands_needs_no_second_round(self, tmp_path):
        _, clients, out = _run(tmp_path)
        assert out["critic_decision"] == "ready"
        assert int(out.get("critic_reroutes_used") or 0) == 0
        assert len(clients["researcher"].payloads) == 4    # 3 lanes + 1 exam
        synth = clients["synthesizer"].payloads[0]["messages"][-1]["content"]
        assert cx.BLOCK_HEADER in synth and "): STANDS" in synth
        assert "settled — C1 printed it" in synth          # the answer, whole

    def test_an_open_challenge_falls_back_to_the_reroute(self, tmp_path):
        _, clients, out = _run(tmp_path, critic=_Critic(ruling="open"))
        assert len(clients["researcher"].payloads) == 5    # + the reroute
        reroute = clients["researcher"].payloads[-1]["messages"][-1]["content"]
        assert "THE CRITIC'S VERDICT ON THE ROUNDS ABOVE" in reroute
        assert cx.BLOCK_HEADER in reroute and "): OPEN" in reroute
        assert len(_exams(clients["researcher"])) == 1     # once a round

    def test_the_reroute_lane_is_handed_the_held_patches(self, tmp_path, monkeypatch):
        seen = []

        def _note(deps, state):
            seen.append(state.get("lane_idx"))
            return "" if state.get("plan_item") else "HELD PATCHES NOTE"
        monkeypatch.setattr(graph, "_held_note", _note)
        _, clients, _ = _run(tmp_path, critic=_Critic(ruling="open"))
        reroute = clients["researcher"].payloads[-1]["messages"][-1]["content"]
        assert reroute.count("HELD PATCHES NOTE") == 1
        first = clients["researcher"].payloads[0]["messages"][-1]["content"]
        assert "HELD PATCHES NOTE" not in first and len(seen) == 4

    def test_off_the_graph_is_unchanged(self, tmp_path):
        g, clients, out = _run(tmp_path, on=False)
        assert graph.CROSS_EXAM_NODE not in g.get_graph().nodes
        assert not _exams(clients["researcher"])
        first = clients["critic"].payloads[0]["messages"][-1]["content"]
        assert cx.CRITIC_NOTE not in first
        assert len(clients["researcher"].payloads) == 4    # 3 lanes + reroute

    def test_the_critic_is_told_how_to_challenge(self, tmp_path):
        _, clients, _ = _run(tmp_path)
        assert cx.CRITIC_NOTE in clients["critic"].payloads[0]["messages"][-1]["content"]

    def test_every_report_is_tagged_with_its_lane(self, tmp_path):
        _, _, out = _run(tmp_path, on=False)
        assert len(out["research_lanes"]) == len(out["research"])
        assert sorted(t["lane"] for t in out["research_lanes"][:3]) == [0, 1, 2]


class TestChallenges:

    def test_parsed_per_report_and_stopped_at_the_next_line_kind(self):
        text = ("Gaps.\n- **CHALLENGE report=2**: a claim.\nSettle with: a run.\n"
                "CHALLENGE report 3 — another\nCHALLENGE report=9: out of range\n"
                "CHALLENGE report=2: second\nDECISION: needs_more_research")
        got = cx.challenges(text, 3)
        assert got == {2: ["a claim.\nSettle with: a run.", "second"],
                       3: ["another"]}

    def test_at_most_per_lane(self):
        text = "\n".join(f"CHALLENGE report=1: c{i}" for i in range(6))
        assert len(cx.challenges(text, 1)[1]) == cx.MAX_PER_LANE

    def _state(self, **kw):
        return {"critic_decision": "needs_more_research", "research_rounds_used": 1,
                "research": ["r1", "r2", "r3"],
                "research_lanes": [{"lane": 0}, {"lane": 1}, {"lane": 0}],
                "critique": "CHALLENGE report=1: x\nCHALLENGE report=3: y\n"
                            "CHALLENGE report=2: z", **kw}

    def test_one_exam_per_lane(self):
        assert cx.plan(self._state()) == [
            {"lane": 0, "reports": {1: ["x"], 3: ["y"]}},
            {"lane": 1, "reports": {2: ["z"]}}]

    def test_nothing_when_it_cannot_be_traced_or_was_done(self):
        assert cx.plan(self._state(research_lanes=[{"lane": 0}])) == []
        assert cx.plan(self._state(critic_decision="ready")) == []
        assert cx.plan(self._state(cross_exam_round=1)) == []
        assert cx.plan(self._state(critique="no challenge here")) == []

    def test_at_most_lanes(self, monkeypatch):
        monkeypatch.setattr(cx, "MAX_LANES", 1)
        assert [e["lane"] for e in cx.plan(self._state())] == [0]


class TestRulings:

    EXAMS = [{"lane": 0, "reports": {1: ["x"]}, "ok": True, "answer": "ran it"},
             {"lane": 1, "reports": {2: ["z"]}, "ok": False, "error": "gone"}]

    def test_no_ruling_is_open(self):
        assert cx.outcome(self.EXAMS, {}) == {1: cx.OPEN, 2: cx.OPEN}

    def test_a_lane_that_did_not_answer_cannot_stand(self):
        ruled = cx.rulings("RULING report=1: **stands** — ok\n"
                           "RULING report=2: stands — ok")
        assert cx.outcome(self.EXAMS, ruled) == {1: cx.STANDS, 2: cx.OPEN}
        ruled = cx.rulings("RULING report=2: falls — it never answered")
        assert cx.outcome(self.EXAMS, ruled)[2] == cx.FALLS

    def test_the_old_words_rule_nothing(self):
        # Council run 9: "upheld" was read both ways. Not a ruling now.
        ruled = cx.rulings("RULING report=1: upheld — the claims are reproduced")
        assert cx.outcome(self.EXAMS, ruled)[1] == cx.OPEN

    def test_the_lanes_own_word_caps_the_ruling(self):
        # Council run 9: lane 2 conceded "grounded" and the critic ruled
        # the claim upheld-as-verified.
        ex = [{"lane": 2, "reports": {3: ["x"], 4: ["y"], 5: ["z"]}, "ok": True,
               "answer": "ANSWER report=3 (A6): conceded — I ran nothing\n"
                         "ANSWER report=4: **narrowed** — only the ADD path\n"
                         "ANSWER report=5: settled — C9 printed it"}]
        ruled = cx.rulings("RULING report=3: stands — read the code\n"
                           "RULING report=4: stands — fine\n"
                           "RULING report=5: stands — C9")
        assert cx.outcome(ex, ruled) == {3: cx.FALLS, 4: cx.NARROWED, 5: cx.STANDS}
        assert cx.lane_answers(ex[0]["answer"]) == {
            3: "conceded", 4: "narrowed", 5: "settled"}

    def test_the_rulings_are_recorded_for_the_synthesis_audits(self, tmp_path):
        out = self._rule(tmp_path, "RULING report=1: narrowed — only csf-12's "
                                   "column\nDECISION: ready")
        [r] = out["cross_exam_rulings"]
        assert (r["report"], r["lane"], r["ruling"], r["round"]) == (1, 0, "narrowed", 1)
        assert r["challenges"] == ["x"] and r["answer"] == "ran it"

    def _rule(self, tmp_path, text, exams=None):
        class _C:
            def chat(self, payload, **kw):
                return _reply(text)
        state = {"question": "q", "critique": "verdict", "research_rounds_used": 1,
                 "cross_exam": [{**e, "round": 1} for e in (exams or self.EXAMS[:1])]}
        return cx.ruling_node(state, chat_client=_C(), model="m")

    def test_all_settled_is_ready_and_refunds_the_reroute(self, tmp_path):
        out = self._rule(tmp_path, "RULING report=1: falls — conceded\nDECISION: ready")
        assert (out["critic_decision"], out["critic_reroutes_used"],
                out["cross_exam_round"]) == ("ready", -1, 1)
        assert "FALLS — conceded" in out["critique"] and "ran it" in out["critique"]

    def test_an_open_one_reroutes_whatever_the_decision_line_says(self, tmp_path):
        out = self._rule(tmp_path, "RULING report=1: open — unclear\nDECISION: ready")
        assert (out["critic_decision"], out["critic_reroutes_used"]) == (
            "needs_more_research", 0)

    def test_settled_but_more_to_research_still_reroutes(self, tmp_path):
        out = self._rule(tmp_path, "RULING report=1: stands — ok\n"
                                   "DECISION: needs_more_research")
        assert out["critic_decision"] == "needs_more_research"

    def test_a_failed_ruling_leaves_everything_open(self):
        class _Boom:
            def chat(self, payload, **kw):
                raise ValueError("down")
        state = {"question": "q", "critique": "v", "research_rounds_used": 1,
                 "cross_exam": [{**self.EXAMS[0], "round": 1}]}
        out = cx.ruling_node(state, chat_client=_Boom(), model="m")
        assert out["critic_decision"] == "needs_more_research"


def test_the_lane_thread_is_its_last_call(tmp_path):
    rec = MessageRecorder(tmp_path / "t.db", meta=RecorderMeta(
        sid="s", cwd=str(tmp_path), question="q", effort="high",
        topology="council", models={}))
    for lane, word in ((1, "old"), (2, "other"), (1, "new")):
        rec.record_llm(role="researcher", round=1, lane_idx=lane, model=f"m{lane}",
                       request={"model": f"m{lane}", "messages": [
                           {"role": "user", "content": word}]},
                       response=_reply(f"report {word}"),
                       prompt_tokens=1, completion_tokens=1, duration_ms=1)
    rec.record_llm(role="researcher:compaction", round=1, lane_idx=1, model="m1",
                   request={"model": "m1", "messages": []}, response=_reply("x"),
                   prompt_tokens=1, completion_tokens=1, duration_ms=1)
    rec.close()
    model, thread = load_lane_thread(tmp_path / "t.db", "researcher", 1)
    assert model == "m1" and [m["content"] for m in thread] == ["new", "report new"]
    assert load_lane_thread(tmp_path / "t.db", "researcher", 7) is None
    assert load_lane_thread(tmp_path / "missing.db", "researcher", 1) is None


def test_examine_resumes_the_thread_on_the_lanes_model_and_view(tmp_path):
    rec = MessageRecorder(tmp_path / "t.db", meta=RecorderMeta(
        sid="s", cwd=str(tmp_path), question="q", effort="high",
        topology="council", models={}))
    rec.record_llm(role="researcher", round=1, lane_idx=3, model="lane-m",
                   request={"model": "lane-m", "messages": [
                       {"role": "system", "content": "sys"},
                       {"role": "user", "content": "item"}]},
                   response=_reply("my report"), prompt_tokens=1,
                   completion_tokens=1, duration_ms=1)
    seen = []

    class _C:
        def chat(self, payload, **kw):
            seen.append(payload)
            return _reply("ANSWER report=4: conceded — it was a guess")
    out = cx.examine_node(
        {"research_rounds_used": 1,
         "cross_exam_item": {"lane": 3, "reports": {4: ["blind?"]}, "round": 1}},
        chat_client=_C(), recorder=rec)
    rec.close()
    [p] = seen
    assert p["model"] == "lane-m"
    assert [m["content"].split()[0] for m in p["messages"][:3]] == ["sys", "item", "my"]
    assert p["messages"][-1]["content"].startswith("[SYSTEM MESSAGE]")
    assert out["cross_exam"][0]["answer"].startswith("ANSWER report=4: conceded")
    assert "Answer with report=4" in p["messages"][-1]["content"]


def test_the_note_names_the_tools_the_exchange_really_has():
    # Council run 9: lane 2 had run_command offered and said it had none.
    note = cx.lane_note({3: ["x"]}, ["read_file", "run_command"])
    assert "Your tools in this exchange: read_file, run_command." in note
    assert "whatever an earlier turn of yours said" in note
    assert "run_command is among" not in cx.lane_note({3: ["x"]}, ["read_file"])
    assert "Your tools" not in cx.lane_note({3: ["x"]})


def test_examine_names_the_offered_tools(tmp_path):
    rec = MessageRecorder(tmp_path / "t.db", meta=RecorderMeta(
        sid="s", cwd=str(tmp_path), question="q", effort="high",
        topology="council", models={}))
    rec.record_llm(role="researcher", round=1, lane_idx=0, model="m",
                   request={"model": "m", "messages": [
                       {"role": "user", "content": "item"}]},
                   response=_reply("r"), prompt_tokens=1, completion_tokens=1,
                   duration_ms=1)
    seen = []

    class _C:
        def chat(self, payload, **kw):
            seen.append(payload)
            return _reply("ANSWER report=1: settled — ok")
    specs = [{"type": "function", "function": {"name": n, "parameters": {}}}
             for n in ("read_file", "run_command")]
    cx.examine_node({"research_rounds_used": 1,
                     "cross_exam_item": {"lane": 0, "reports": {1: ["x"]}}},
                    chat_client=_C(), recorder=rec, tool_specs=specs,
                    tool_executor=lambda *a, **k: "")
    rec.close()
    note = [m for m in seen[0]["messages"] if cx.LANE_NOTE in str(m.get("content"))][-1]
    assert "run_command is among them" in note["content"]


def test_a_lane_with_no_conversation_leaves_it_open(tmp_path):
    out = cx.examine_node({"cross_exam_item": {"lane": 0, "reports": {1: ["x"]}}},
                          chat_client=object(), recorder=None)
    assert out["cross_exam"][0]["ok"] is False


class TestConfig:

    def test_default_shell_and_round_trip(self, tmp_path):
        from consultants import config as cc
        assert cc.ConsultantsConfig().critic.cross_examination == "shell"
        cfg = cc.set_cross_examination(enabled=True, scope="project", cwd=tmp_path)
        assert cfg.critic.cross_examination is True
        text = (tmp_path / ".claude-hooks" / "consultants.toml").read_text()
        assert "[critic]" in text and "cross_examination = true" in text

    def test_cli(self):
        from consultants import cli
        args = cli.build_parser().parse_args(
            ["config", "set-cross-examination", "on"])
        assert args.fn is cli.cmd_config_set_cross_examination and args.state == "on"

    def test_seeded_only_when_on(self):
        from consultants import config as cc
        from consultants.engine.control import runtime_control_defaults
        cfg = cc.ConsultantsConfig()
        assert "cross_examination" not in runtime_control_defaults(cfg, effort="high")
        cfg.critic.cross_examination = True
        assert runtime_control_defaults(cfg, effort="high")["cross_examination"] is True

    def test_the_runner_wires_it(self):
        import inspect
        from consultants.server import runner
        src = inspect.getsource(runner)
        # The graph and the seed both take the value resolved for this
        # run's shell; a follow-up graph has no node and seeds it off.
        assert src.count('cc.cross_examination_on(\n                cfg, runner_input.get("shell"))') == 1
        assert src.count('cross_exam=cc.cross_examination_on(cfg, runner_input.get("shell"))') == 1
        assert "cfg, enabled_t, cross_exam=False)" in src


def test_the_note_is_absent_when_off():
    msgs = [{"role": "user", "content": "x"}]
    assert council._with_cross_exam_note(msgs, {}) is msgs
    out = council._with_cross_exam_note(msgs, {"runtime_control": {"cross_examination": True}})
    assert out[-1]["content"].endswith(cx.CRITIC_NOTE)


def test_a_wrap_up_skips_both_nodes():
    from consultants.engine.run_control import WRAP_UP_SKIPPED_ROLES
    assert {graph.CROSS_EXAM_NODE, graph.CROSS_EXAM_RULING_NODE} <= WRAP_UP_SKIPPED_ROLES


def test_state_declares_the_channels():
    from consultants.engine.state_v2 import CouncilStateV2
    for cls in (graph.CouncilState, CouncilStateV2):
        ann = cls.__annotations__
        assert {"research_lanes", "cross_exam", "cross_exam_round"} <= set(ann), cls
    json.dumps({"ok": True})


def test_a_reroute_is_shown_what_the_critic_asked():
    """Before 2026-10-18 a reroute said "address the gaps" and never
    showed them."""
    fresh = council.build_researcher_messages("q", "p", [], [], critique="GAP X")
    assert "GAP X" not in fresh[-1]["content"]
    again = council.build_researcher_messages("q", "p", ["r1"], [], critique="GAP X")
    assert "THE CRITIC'S VERDICT ON THE ROUNDS ABOVE:\nGAP X" in again[-1]["content"]
    assert council.build_researcher_messages("q", "p", ["r1"], []) == \
        council.build_researcher_messages("q", "p", ["r1"], [], critique="  ")
