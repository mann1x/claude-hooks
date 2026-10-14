"""Plan Milestone C1-C3: compaction rules, flows, target, cut and rescue."""
from __future__ import annotations

import json
import threading
import time

import pytest

from consultants.engine import budget, compaction, retrospective, tool_ledger
from consultants.engine import compaction_prompts as P


# --- a scripted backend ------------------------------------------------- #

class Backend:
    """Answers each compaction step by its system prompt, and records
    every payload. ``chat`` only: no stall wrapper is built for it."""

    def __init__(self, ctx=32_768, *, writer=None, delay=0.0):
        self.ctx = ctx
        self.payloads: list[dict] = []
        self.writer = writer
        self.delay = delay
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def context_length(self, model):
        return self.ctx

    def _reply(self, payload):
        system = payload["messages"][0]["content"]
        user = payload["messages"][-1]["content"]
        if system in (P.WRITER_SYSTEM_PROMPT, P.FULL_SYSTEM_PROMPT):
            if self.writer:
                return self.writer(payload, len([p for p in self.payloads
                                                 if p["messages"][0]["content"] == system]) - 1)
            half = "\n<<<HALFWAY>>>\n" if "<<<HALFWAY>>>" in user else "\n\n"
            return ("The user is asking me to find the bug. Let me read the "
                    "file. [#1] It shows the loop." + half +
                    "Now I am checking the caller. [#2] It passes None. "
                    "I am about to fix the guard.")
        if system == P.RETROSPECTIVE_SYSTEM_PROMPT:
            return "## What worked\nReading the caller first."
        if system == P.REVIEWER_SYSTEM_PROMPT:
            return ("## Replay\nThe user is asking me to find the bug. Let me "
                    "read the file. [#1] It shows the loop. Now I am checking "
                    "the caller. [#2] It passes None, so I am fixing the "
                    "guard.\n\n## Retrospective\nReading the caller first "
                    "paid off.")
        if system == P.CRITIC_SYSTEM_PROMPT:
            return ("The user is asking me to find the bug. Let me read the "
                    "file. [#1] It shows the loop."
                    if "**first half**" in user else
                    "Now I am checking the caller. [#2] It passes None. I am "
                    "about to fix the guard.")
        if system == P.SYNTHESIZER_SYSTEM_PROMPT:
            return ("## Replay\nThe user is asking me to find the bug. Let me "
                    "read the file. [#1] It shows the loop. Now I am checking "
                    "the caller. [#2] It passes None. I am about to fix the "
                    "guard.\n\n## Retrospective\nReading the caller first.")
        return "ok"

    def chat(self, payload):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                time.sleep(self.delay)
            self.payloads.append(json.loads(json.dumps(payload, default=str)))
            text = self._reply(payload)
            return {"choices": [{"message": {"content": text},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 10,
                              "completion_tokens": max(1, len(text) // 4)}}
        finally:
            with self._lock:
                self.active -= 1


def _loop_history(n=24, size=900, question="find the bug in loop.py"):
    msgs = [{"role": "system", "content": "you are a researcher"},
            {"role": "user", "content": question}]
    for i in range(n):
        msgs.append({"role": "assistant", "content": None,
                     "reasoning": f"thinking about step {i} " * 20,
                     "tool_calls": [{"id": f"c{i}", "type": "function",
                                     "function": {"name": "read_file",
                                                  "arguments": json.dumps(
                                                      {"path": f"f{i}.py"})}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}",
                     "content": f"content {i} " * size})
    return msgs


def _scope(backend, *, flow="reduced", **kw):
    settings = compaction.Settings(flow=flow, **kw)
    return compaction.Scope(chat_client=backend, model="lane-model",
                            role="researcher", settings=settings)


def _plan(backend, messages):
    return budget.plan(backend, "lane-model", messages)


# --- C1: hard rules ----------------------------------------------------- #

@pytest.mark.parametrize("flow", ["reduced", "full"])
def test_every_compaction_call_runs_on_the_lanes_model_serially_thinking_off(flow):
    backend = Backend(delay=0.01)
    msgs = _loop_history()
    scope = _scope(backend, flow=flow)
    out = compaction.compact(scope, msgs, plan=_plan(backend, msgs))
    assert out.changed
    assert backend.payloads, "no compaction call was made"
    assert {p["model"] for p in backend.payloads} == {"lane-model"}
    assert all(p["think"] is False for p in backend.payloads)
    assert backend.max_active == 1  # one serial connection
    assert {m for _, m in scope.calls} == {"lane-model"}


def test_scope_has_no_way_to_name_another_model():
    import dataclasses
    fields = {f.name for f in dataclasses.fields(compaction.Scope)}
    assert "model" in fields
    assert not ({"summarizer", "summarizer_model", "compaction_model"} & fields)


def test_council_compaction_uses_the_role_model_end_to_end():
    from consultants.engine import council
    backend = Backend()
    msgs = _loop_history()
    out, _ = council._plan_and_compact(backend, "lane-model", msgs,
                                       role="researcher")
    assert len(out) < len(msgs)
    assert {p["model"] for p in backend.payloads} == {"lane-model"}


# --- C2: flows ------------------------------------------------------------ #

def _steps(scope):
    return [s for s, _ in scope.calls]


def test_reduced_flow_is_writer_retrospective_reviewer():
    backend = Backend()
    msgs = _loop_history()
    scope = _scope(backend)
    compaction.compact(scope, msgs, plan=_plan(backend, msgs))
    assert _steps(scope)[:3] == ["writer#1", "retrospective", "reviewer"]


def test_full_flow_is_five_steps_with_the_halfway_marker():
    backend = Backend()
    msgs = _loop_history()
    scope = _scope(backend, flow="full")
    compaction.compact(scope, msgs, plan=_plan(backend, msgs))
    assert _steps(scope)[:5] == ["writer#1", "retrospective", "critic:first",
                                 "critic:second", "synthesizer"]
    writer = backend.payloads[0]["messages"][-1]["content"]
    assert "<<<HALFWAY>>>" in writer


def test_basic_flow_makes_no_call():
    backend = Backend()
    msgs = _loop_history()
    scope = _scope(backend, flow="basic")
    out = compaction.compact(scope, msgs, plan=_plan(backend, msgs))
    assert out.changed and not backend.payloads


def test_writer_gets_the_replay_prompt_ledger_key_and_no_trailing_echo_bait():
    backend = Backend()
    msgs = _loop_history()
    compaction.compact(_scope(backend), msgs, plan=_plan(backend, msgs))
    req = backend.payloads[0]["messages"][-1]["content"]
    assert req.startswith(P.REPLAY_PROMPT[:80])
    assert "Cite these as `[#N]`" in req
    assert "1. read_file  path=f" in req


def test_replay_citations_are_spliced_and_uncited_appended():
    backend = Backend()
    msgs = _loop_history()
    out = compaction.compact(_scope(backend), msgs, plan=_plan(backend, msgs))
    marker = next(m for m in out.messages if m.get("_compaction_marker"))
    assert "1. read_file  path=f" in marker["content"]
    assert "[#1]" not in marker["content"]
    assert "recorded by the harness" in marker["content"]  # uncited block
    assert marker["_compaction_ledger"]


def test_writer_retries_empty_then_overrun_with_the_measurement():
    def writer(payload, n):
        if n == 0:
            return ""
        if n == 1:
            return "word " * 40_000
        return "The user is asking me to fix it. I am reading the file."
    backend = Backend(writer=writer)
    msgs = _loop_history()
    scope = _scope(backend, review=False)
    compaction.compact(scope, msgs, plan=_plan(backend, msgs))
    writers = [p for p in backend.payloads
               if p["messages"][0]["content"] == P.WRITER_SYSTEM_PROMPT]
    assert len(writers) == 3
    assert "Your previous attempt was about" not in writers[1]["messages"][-1]["content"]
    assert "Your previous attempt was about" in writers[2]["messages"][-1]["content"]


def test_an_over_budget_text_is_kept_on_the_last_attempt_never_an_empty_one():
    backend = Backend(writer=lambda p, n: "word " * 40_000)
    scope = _scope(backend, review=False, writer_attempts=2)
    text, info = compaction.write_replay(
        scope, "req", system=P.WRITER_SYSTEM_PROMPT, max_tokens=100,
        model="m", tail=True)
    assert text and info["attempts"] == 2
    empty = Backend(writer=lambda p, n: "")
    text, info = compaction.write_replay(
        _scope(empty), "req", system=P.WRITER_SYSTEM_PROMPT, max_tokens=100,
        model="m", tail=True)
    assert text == "" and info["attempts"] == 3


def test_echo_cut_and_fence_trim():
    text = ("I am reading the file.\n```\n[Tool result]: quoted\n```\n"
            "Done.\nConversation:\n[User]: echoed")
    cut, n = compaction.cut_echo(text)
    assert cut.endswith("Done.") and n > 0
    fenced = "intro\n```py\n" + "x = 1\n" * 500 + "```\nafter"
    trimmed, blocks = compaction.trim_fences(fenced)
    assert blocks == 1 and "elided by the harness" in trimmed
    assert trimmed.endswith("```\nafter")


def test_full_flow_falls_back_when_critics_fail():
    class Failing(Backend):
        def _reply(self, payload):
            if payload["messages"][0]["content"] == P.CRITIC_SYSTEM_PROMPT:
                raise RuntimeError("down")
            return super()._reply(payload)
    backend = Failing()
    msgs = _loop_history()
    scope = _scope(backend, flow="full")
    out = compaction.compact(scope, msgs, plan=_plan(backend, msgs))
    assert out.changed
    marker = next(m for m in out.messages if m.get("_compaction_marker"))
    assert "<<<HALFWAY>>>" not in marker["content"]
    assert "synthesizer" not in _steps(scope)


def test_a_collapsed_merge_keeps_the_original():
    class Collapsing(Backend):
        def _reply(self, payload):
            if payload["messages"][0]["content"] == P.REVIEWER_SYSTEM_PROMPT:
                return "## Replay\nshort"
            return super()._reply(payload)
    backend = Collapsing()
    msgs = _loop_history()
    out = compaction.compact(_scope(backend), msgs, plan=_plan(backend, msgs))
    marker = next(m for m in out.messages if m.get("_compaction_marker"))
    assert "It shows the loop" in marker["content"]


def test_split_at_marker_band_and_rebalance():
    body = "a" * 100 + "\n<<<HALFWAY>>>\n" + "b" * 100
    assert compaction.split_at_marker(body)[2] == "marker"
    lopsided = "a" * 10 + "\n<<<HALFWAY>>>\n" + "b" * 300 + "\n\n" + "c" * 300
    first, second, source = compaction.split_at_marker(lopsided)
    assert source == "rebalanced" and "<<<HALFWAY>>>" not in first + second


def test_placement_user_puts_the_marker_in_a_user_turn():
    backend = Backend()
    msgs = _loop_history()
    out = compaction.compact(_scope(backend, placement="user"), msgs,
                             plan=_plan(backend, msgs))
    marker = next(m for m in out.messages if m.get("_compaction_marker"))
    assert marker["role"] == "user"


# --- verbatim user requests ---------------------------------------------- #

def test_follow_up_requests_survive_verbatim_and_are_never_dropped():
    msgs = _loop_history(n=10)
    follow = "Also check whether `retry()` swallows KeyboardInterrupt."
    msgs.append({"role": "user", "content": follow})
    msgs += _loop_history(n=14)[2:]
    backend = Backend()
    out = compaction.compact(_scope(backend), msgs, plan=_plan(backend, msgs))
    assert out.changed
    texts = [str(m.get("content") or "") for m in out.messages]
    assert any(follow in t for t in texts), "the follow-up was folded away"
    writer = backend.payloads[0]["messages"][-1]["content"]
    assert f"<user_request>\n{follow}\n</user_request>" in writer


def test_requests_are_elided_in_the_middle_never_dropped():
    reqs = ["r" * 5000, "s" * 5000, "t" * 50]
    shown = compaction.render_requests(reqs, 900)
    assert len(shown) == 3
    assert "elided by the harness" in shown[0] and shown[2] == "t" * 50


def test_requests_carry_across_generations():
    first = {"role": "user", "content": "first follow-up"}
    marker = retrospective.render_marker(
        retrospective.Digest(summary="s", user_requests=["first follow-up"],
                             shown_requests=["first follow-up"]), 3)
    prev = retrospective.previous_digest([marker])
    assert prev.user_requests == ["first follow-up"]
    assert compaction.merge_requests(prev.user_requests,
                                     compaction.collect_user_requests([first])
                                     ) == ["first follow-up", "first follow-up"][:1]


# --- C3: target, trigger, cut, rescue ------------------------------------ #

def test_trigger_defaults_follow_the_fork():
    assert budget.COMPACTION_TRIGGER_RATIO == 0.9
    assert budget.MIN_OUTPUT_ROOM_TOKENS == 8_000
    lo = budget.compaction_trigger_tokens(context_length=100_000, ratio=0.5,
                                          observed_output=10)
    assert lo == 50_000


def test_target_is_a_quarter_of_free_content_below_the_trigger():
    assert budget.compaction_target_tokens(32_768, overhead_tokens=2_768,
                                           trigger_tokens=29_000) == 7_500
    assert budget.compaction_target_tokens(10_000, trigger_tokens=1_000) == 999


def test_the_cut_never_opens_the_tail_on_a_tool_result():
    msgs = _loop_history()
    cut = budget.plan_cut(msgs, keep_tokens=3_000, context_length=32_768)
    assert cut.folds
    assert msgs[cut.cut_end]["role"] == "assistant"


def test_a_long_last_turn_is_cut_with_its_prompt_pinned():
    msgs = _loop_history(n=4, size=50)
    prompt = {"role": "user", "content": "now fix it"}
    msgs.append(prompt)
    msgs += _loop_history(n=20)[2:]
    cut = budget.plan_cut(msgs, keep_tokens=4_000, context_length=32_768)
    assert cut.pinned_index == msgs.index(prompt)
    assert cut.reason in ("last-turn-ceiling", "pinned")
    res = budget.compact_messages(msgs, keep_tokens=4_000,
                                  context_length=32_768)
    assert res.pinned is prompt and prompt in res.messages


def test_one_prompt_plus_a_loop_still_compacts_on_the_second_pass():
    msgs = _loop_history()
    first = budget.compact_messages(msgs, keep_tokens=3_000,
                                    context_length=32_768)
    grown = first.messages + _loop_history(n=12)[2:]
    second = budget.compact_messages(grown, keep_tokens=3_000,
                                     context_length=32_768)
    assert second.changed and second.generation == 2


def test_no_tail_rescue_is_taken_only_when_smaller():
    class Wordy(Backend):
        def _reply(self, payload):
            if payload["messages"][0]["content"] == P.WRITER_SYSTEM_PROMPT:
                return "I am working. " * 6_000  # a keep-tail replay that is huge
            if payload["messages"][0]["content"] == P.FULL_SYSTEM_PROMPT:
                return "## Goal\nfix\n## Next\nread"
            return super()._reply(payload)
    backend = Wordy()
    msgs = _loop_history()
    scope = _scope(backend, review=False, writer_attempts=1)
    out = compaction.compact(scope, msgs, plan=_plan(backend, msgs))
    assert out.report.get("rescue", {}).get("taken") is True
    assert any(p["messages"][0]["content"] == P.FULL_SYSTEM_PROMPT
               for p in backend.payloads)
    assert out.report["after"] < out.report["before"]


def test_a_fold_that_does_not_shrink_is_discarded():
    class Bloated(Backend):
        def _reply(self, payload):
            return "x " * 200_000
    backend = Bloated()
    msgs = _loop_history(n=6, size=400)
    plan = budget.Budget(32_768, 30_000, 4096, True, trigger_tokens=1_000)
    out = compaction.compact(_scope(backend, review=False, writer_attempts=1),
                             msgs, plan=plan)
    assert not out.changed and out.messages is msgs
    assert out.report["discarded"] == "no-shrink"


# --- refusals ------------------------------------------------------------ #

def test_refusals_are_waited_out_writer_unbounded_others_three(monkeypatch):
    from claude_hooks.get_advice.chat_client import ChatFault
    slept = []
    monkeypatch.setattr(compaction, "_sleep", slept.append)

    class Refusing:
        def __init__(self, refusals):
            self.left = refusals

        def chat(self, payload):
            if self.left:
                self.left -= 1
                raise ChatFault("busy", fault_class="refusal", status=429)
            return {"choices": [{"message": {"content": "done"}}]}

    r = compaction.call(_scope(Refusing(7)), "writer#1", "s", "u", 10,
                        refusal_waits=None)
    assert r.text == "done" and len(slept) == 7
    slept.clear()
    r = compaction.call(_scope(Refusing(7)), "reviewer", "s", "u", 10)
    assert r.error and len(slept) == compaction.OPTIONAL_REFUSAL_WAITS


# --- recording and stall phase ------------------------------------------- #

class _Recorder:
    def __init__(self):
        self.llm, self.events = [], []

    def record_llm(self, **kw):
        self.llm.append(kw)

    def record_event(self, **kw):
        self.events.append(kw)


def test_compaction_calls_are_recorded_apart_from_the_role():
    backend = Backend()
    rec = _Recorder()
    msgs = _loop_history()
    scope = compaction.Scope(chat_client=backend, model="lane-model",
                             role="critic", recorder=rec, lane_idx=2)
    compaction.compact(scope, msgs, plan=_plan(backend, msgs))
    assert rec.llm and {r["role"] for r in rec.llm} == {"critic:compaction"}
    assert all(r["lane_idx"] == 2 for r in rec.llm)
    ev = [e for e in rec.events if e["kind"] == "compaction"]
    assert ev and ev[0]["payload"]["phase"] == "compaction"


def test_stall_events_carry_phase_compaction():
    rec = _Recorder()

    class Streamed:
        def chat(self, payload):  # pragma: no cover — streamed path used
            raise AssertionError

        def chat_streamed(self, payload, *, on_token, cancel_check, **kw):
            on_token("x")
            return {"choices": [{"message": {"content": "ok"}}]}

    scope = compaction.Scope(chat_client=Streamed(), model="m", role="planner",
                             recorder=rec,
                             settings=compaction.Settings(stall=True))
    r = compaction.call(scope, "writer#1", "s", "u", 64)
    assert r.text == "ok"
    stall = [e for e in rec.events if e["kind"].startswith("stall")]
    assert stall and all(e["payload"]["phase"] == "compaction" for e in stall)
    assert stall[0]["payload"]["step"] == "writer#1"


# --- settings, config, parity -------------------------------------------- #

def test_settings_layer_most_specific_last():
    rc = {"compaction": {"*": {"flow": "full", "review": False},
                         "critic": {"flow": "reduced"},
                         "critic@m": {"placement": "user"}},
          "compaction_hard_s": 99}
    s = compaction.settings_from(rc, "critic", "m")
    assert (s.flow, s.review, s.placement, s.stall, s.hard_cap_s) == (
        "reduced", False, "user", True, 99.0)
    assert compaction.settings_from({}, "critic", "m") == compaction.DEFAULT_SETTINGS


def test_default_run_seeds_no_compaction_record():
    from consultants import config as cc
    from consultants.engine import control
    rc = control.runtime_control_defaults(cc.ConsultantsConfig(), effort="high")
    assert "compaction" not in rc


def test_parity_a_history_that_fits_makes_no_compaction_call():
    from consultants.engine import council
    backend = Backend(ctx=262_144)
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]
    out, plan = council._plan_and_compact(backend, "m", list(msgs),
                                          role="planner")
    assert not backend.payloads and not plan.needs_compaction
    assert [m for m in out if m.get("_compaction_marker")] == []


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_compaction_config_and_cli(home, capsys):
    from consultants import cli
    from consultants import config as cc
    from consultants.engine import control
    assert cli.main(["config", "set-compaction", "--flow", "full", "--role",
                     "researcher", "--user"]) == 0
    assert cli.main(["config", "set-compaction", "--placement", "user",
                     "--review", "off", "--user"]) == 0
    assert cli.main(["config", "set-compaction", "--role", "critic", "--model",
                     "glm:cloud", "--ledger", "off", "--user"]) == 0
    assert cli.main(["config", "set-budget", "--trigger-ratio", "0.85",
                     "--user"]) == 0
    assert cli.main(["config", "set-compaction", "--target-share", "0.9",
                     "--user"]) == 2
    capsys.readouterr()
    cfg = cc.load_config(home)
    assert cfg.compaction == {"placement": "user", "review": False}
    assert cfg.roles["researcher"].compaction == {"flow": "full"}
    assert cfg.roles["critic"].model_overrides == {"glm:cloud": {"ledger": False}}
    assert cfg.budget == {"trigger_ratio": 0.85}
    rc = control.runtime_control_defaults(cfg, effort="high")
    assert rc["compaction"] == {"*": {"placement": "user", "review": False},
                                "researcher": {"flow": "full"},
                                "critic@glm:cloud": {"ledger": False}}
    assert compaction.settings_from(rc, "researcher", "x").flow == "full"


# --- ledger -------------------------------------------------------------- #

def test_ledger_pairs_by_id_collapses_repeats_and_marks_refusals():
    msgs = [
        {"role": "assistant", "tool_calls": [
            {"id": "b", "function": {"name": "grep", "arguments": '{"pattern": "x"}'}},
            {"id": "a", "function": {"name": "read_file", "arguments": '{"path": "p"}'}}]},
        {"role": "tool", "tool_call_id": "a", "content": "file body"},
        {"role": "tool", "tool_call_id": "b", "content": "no matches"},
        {"role": "assistant", "tool_calls": [
            {"id": "c", "function": {"name": "read_file", "arguments": '{"path": "p"}'}}]},
        {"role": "tool", "tool_call_id": "c", "content": "file body"},
        {"role": "assistant", "tool_calls": [
            {"id": "d", "function": {"name": "run_command", "arguments": '{"cmd": "make"}'}}]},
        {"role": "tool", "tool_call_id": "d", "content": '{"ok": false}'},
    ]
    entries = tool_ledger.build(msgs)
    assert [(e.tool, e.result, e.repeated) for e in entries] == [
        ("grep", "no matches", 1), ("read_file", "file body", 2),
        ("run_command", '{"ok": false}', 1)]
    assert entries[0].verdict and entries[2].failed and entries[2].verdict
    kept = tool_ledger.evict(entries, 60)
    assert [e.tool for e in kept] == ["run_command"]


def test_splice_drops_invalid_citations():
    entries = tool_ledger.build([
        {"role": "assistant", "tool_calls": [
            {"id": "a", "function": {"name": "read_file", "arguments": '{"path": "p"}'}}]},
        {"role": "tool", "tool_call_id": "a", "content": "body"}])
    s = tool_ledger.splice("Reading. [#1] Then [#1] and [#9] and [#3-1].", entries)
    assert s.cited == [1] and sorted(s.invalid) == [1, 3, 9]
    assert "1. read_file" in s.text and "[#" not in s.text
