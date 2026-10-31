"""Plan Milestone C5: overflow compaction and history hygiene."""
from __future__ import annotations

import json

import pytest

from claude_hooks.agent_loop import runner
from claude_hooks.get_advice.chat_client import ChatFault
from consultants.engine import budget, compaction, council, history
from consultants.engine import compaction_prompts as P


def _read(i, path, start=None, end=None, size=2_000):
    args = {"path": path}
    if start is not None:
        args.update(start_line=start, end_line=end)
    return [
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": f"r{i}", "type": "function",
                         "function": {"name": "read_file",
                                      "arguments": json.dumps(args)}}]},
        {"role": "tool", "tool_call_id": f"r{i}", "name": "read_file",
         "content": f"body {i} " * size},
    ]


def _history(n=24, size=900):
    msgs = [{"role": "system", "content": "you are a researcher"},
            {"role": "user", "content": "find the bug"}]
    for i in range(n):
        msgs += _read(i, f"f{i}.py", size=size)
    return msgs


#: Every compaction step's system prompt: the no-tail writer's opens
#: "You write", so a prefix test sent it the role's scripted refusal.
COMPACTION_SYSTEMS = (P.WRITER_SYSTEM_PROMPT, P.FULL_SYSTEM_PROMPT,
                      P.RETROSPECTIVE_SYSTEM_PROMPT, P.REVIEWER_SYSTEM_PROMPT,
                      P.CRITIC_SYSTEM_PROMPT, P.SYNTHESIZER_SYSTEM_PROMPT)


class Backend:
    def __init__(self, ctx=32_768, script=None):
        self.ctx = ctx
        self.calls: list[dict] = []
        self.script = list(script or [])

    def context_length(self, model):
        return self.ctx

    def chat(self, payload):
        self.calls.append(payload)
        system = (payload["messages"][0].get("content") or "")
        if payload.get("think") is False and system in COMPACTION_SYSTEMS:
            return {"choices": [{"message": {"content": "I am reading the "
                                             "file. It is fine."},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 10}}
        step = self.script.pop(0) if self.script else None
        if isinstance(step, Exception):
            raise step
        if isinstance(step, dict):
            return step
        return {"choices": [{"message": {"content": "answer"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 5}}


def _overflow():
    return ChatFault("context length exceeded", fault_class="context_overflow",
                     status=400)


def _role_calls(backend):
    return [c for c in backend.calls if c.get("think") is not False]


# --- history hygiene ------------------------------------------------------ #

def test_oversized_tool_results_are_middle_truncated_with_the_marker():
    msgs = [{"role": "tool", "name": "grep", "content": "g" * 40_000},
            {"role": "tool", "name": "read_file", "content": "f" * 45_000},
            {"role": "tool", "name": "read_file", "content": "h" * 60_000}]
    out = history.truncate_tool_results(msgs)
    assert len(out[0]["content"]) <= 32_000 + 50
    assert "the harness removed them from the middle" in out[0]["content"]
    assert out[1] is msgs[1]                      # under the file limit
    assert "Read the missing range again" in out[2]["content"]
    assert history.truncate_tool_results(out) is out   # idempotent
    small = [{"role": "tool", "content": "x"}]
    assert history.truncate_tool_results(small) is small


def test_stale_reads_are_rewritten_only_under_pressure_and_all_at_once():
    msgs = [{"role": "user", "content": "q"}]
    msgs += _read(1, "a.py", 1, 50) + _read(2, "b.py") + _read(3, "a.py")
    msgs += _read(4, "c.py", 10, 20) + _read(5, "c.py", 1, 5)
    roomy = history.rewrite_stale_reads(msgs, context_length=1_000_000)
    assert roomy is msgs                         # below 40%: a pure append
    tight = history.rewrite_stale_reads(msgs, context_length=12_000)
    contents = {m["tool_call_id"]: m["content"] for m in tight
                if m.get("role") == "tool"}
    assert contents["r1"] == history.OUTDATED_FILE_CONTENT  # superseded
    assert contents["r3"].startswith("body 3")             # the latest
    assert contents["r2"].startswith("body 2")             # never re-read
    assert contents["r4"].startswith("body 4")             # not covered by r5


def test_a_small_reclaim_is_not_worth_the_cache_break():
    msgs = [{"role": "user", "content": "q" * 50_000}]
    msgs += _read(1, "a.py", size=10) + _read(2, "a.py", size=10)
    assert history.rewrite_stale_reads(msgs, context_length=20_000) is msgs


def test_plan_and_compact_applies_the_hygiene_before_sizing():
    backend = Backend(ctx=262_144)
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"},
            {"role": "tool", "name": "grep", "content": "z" * 90_000}]
    out, plan = council._plan_and_compact(backend, "m", msgs, role="researcher")
    assert len(out[-1]["content"]) < 33_000 and not backend.calls


# --- provider-refused overflow ------------------------------------------- #

def test_single_shot_recovers_from_one_refused_overflow():
    backend = Backend(script=[_overflow()])
    text, _, _ = council._single_shot(backend, "m", _history(), role="critic",
                                      think=True)
    sent = _role_calls(backend)
    assert text == "answer" and len(sent) == 2
    assert len(sent[1]["messages"]) < len(sent[0]["messages"])


def test_a_second_overflow_is_a_named_failure():
    backend = Backend(script=[_overflow(), _overflow()])
    with pytest.raises(council.ContextOverflowUnrecovered):
        council._single_shot(backend, "m", _history(), role="critic")


def test_overflow_with_nothing_to_fold_is_a_named_failure():
    backend = Backend(ctx=262_144, script=[_overflow()])
    with pytest.raises(council.ContextOverflowUnrecovered):
        council._single_shot(backend, "m", [{"role": "user", "content": "q"}],
                             role="critic")


def test_tool_loop_chat_fn_recovers_from_one_refused_overflow():
    backend = Backend(script=[_overflow()])
    prepare = council._loop_payload_budgeter(backend, "m", role="researcher")
    fn = prepare.recover(backend.chat)
    payload = {"model": "m", "messages": _history()}
    prepare(payload)
    resp = fn(payload)
    assert resp["choices"][0]["message"]["content"] == "answer"
    sent = _role_calls(backend)
    assert len(sent[1]["messages"]) < len(sent[0]["messages"]) or any(
        m.get("_compaction_marker") for m in sent[1]["messages"])


def test_require_smaller_keeps_basic_when_the_digest_does_not_beat_it():
    class Bloated(Backend):
        def chat(self, payload):
            if payload.get("think") is False:
                return {"choices": [{"message": {"content": "x " * 60_000}}]}
            return super().chat(payload)
    backend = Bloated()
    msgs = _history()
    plan = budget.plan(backend, "m", msgs)
    scope = compaction.Scope(chat_client=backend, model="m", role="critic",
                             settings=compaction.Settings(review=False,
                                                          writer_attempts=1))
    out = compaction.compact(scope, msgs, plan=plan, reason="overflow",
                             require_smaller=True)
    assert out.changed and out.report["overflow"]["taken"] == "basic"
    marker = next(m for m in out.messages if m.get("_compaction_marker"))
    assert "elided to fit" in marker["content"]


def test_a_refusal_right_after_a_small_window_fold_still_has_a_floor():
    # ch-25: below 64k a fold keeps less tail, so the next refused request
    # may hold nothing a tail fold can take. The digest is rejected here
    # (bloated), so the deterministic floor must fold the tail itself.
    class Bloated(Backend):
        def chat(self, payload):
            if payload.get("think") is False:
                return {"choices": [{"message": {"content": "x " * 60_000}}]}
            return super().chat(payload)
    backend = Bloated()
    settings = compaction.Settings(flow="basic")
    first = compaction.compact(
        compaction.Scope(chat_client=backend, model="m", role="critic",
                         settings=settings),
        _history(), plan=budget.plan(backend, "m", _history()))
    assert first.changed and first.report.get("tail_keep")
    msgs = first.messages
    scope = compaction.Scope(chat_client=backend, model="m", role="critic",
                             settings=compaction.Settings(review=False,
                                                          writer_attempts=1))
    out = compaction.compact(scope, msgs, plan=budget.plan(backend, "m", msgs),
                             reason="overflow", require_smaller=True)
    assert out.changed and out.report["overflow_no_tail"] is True
    assert out.report["overflow"]["taken"] == "basic"
    assert len(out.messages) < len(msgs)


# --- silent truncation ---------------------------------------------------- #

def test_silent_truncation_rule():
    assert council.silently_truncated(40_000, 30_000, 32_768)
    assert not council.silently_truncated(31_000, 30_000, 32_768)  # agrees
    assert not council.silently_truncated(40_000, 10_000, 32_768)  # far from edge
    assert not council.silently_truncated(40_000, 30_000, None)


def test_single_shot_reasks_after_a_silently_truncated_prompt(monkeypatch):
    seen = []
    monkeypatch.setattr(council, "_observe_usage",
                        lambda *a, **k: seen.append(a[2]))
    truncated = {"choices": [{"message": {"content": "partial"},
                              "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 30_000, "completion_tokens": 5}}
    backend = Backend(script=[truncated])
    msgs = _history(n=30)
    monkeypatch.setattr(council, "silently_truncated",
                        lambda est, pt, ctx: pt == 30_000)
    text, _, _ = council._single_shot(backend, "m", msgs, role="critic")
    assert text == "answer"
    assert 30_000 not in seen          # never calibrated on the cut count
    assert len(_role_calls(backend)) == 2


def test_loop_observe_flags_the_next_call_for_compaction(monkeypatch):
    backend = Backend()
    prepare = council._loop_payload_budgeter(backend, "m", role="researcher")
    monkeypatch.setattr(council, "silently_truncated", lambda *a: True)
    prepare.observe({"messages": [{"role": "user", "content": "q"}]},
                    {"usage": {"prompt_tokens": 1}})
    assert council._FORCE_NEXT.get(prepare.session) == "silent-truncation"


# --- starved cap ---------------------------------------------------------- #

def test_a_thin_window_bound_cap_forces_one_compaction_per_descent():
    knobs = budget.Knobs()
    thin = budget.Budget(32_768, 25_000, 4_096, False,
                         cap_source="remaining-context")
    roomy = budget.Budget(32_768, 1_000, 24_000, False)
    assert council._starved_cap(thin, knobs, "s1")
    assert not council._starved_cap(thin, knobs, "s1")      # latched
    assert not council._starved_cap(roomy, knobs, "s1")     # recovered
    assert council._starved_cap(thin, knobs, "s1")          # a new descent


# --- duplicate-call stub --------------------------------------------------- #

def test_duplicate_stub_reserves_every_fourth_repeat_and_clears_on_write():
    calls = []

    def executor(name, args, cwd):
        calls.append(name)
        return f"{name} ran"

    seen: dict = {}
    repeats: dict = {}
    tc = {"id": "a", "function": {"name": "read_file",
                                  "arguments": '{"path": "x", "end_line": 3}'}}
    reordered = {"id": "b", "function": {"name": "read_file",
                                         "arguments": '{"end_line":3,"path":"x"}'}}
    outs = [runner.execute_tool_calls([t], "", seen, executor,
                                      repeats=repeats)[0]["content"]
            for t in (tc, reordered, tc, tc, tc)]
    assert calls == ["read_file", "read_file"]           # 1st + 4th repeat
    assert outs[1].startswith("(duplicate") and outs[4] == "read_file ran"
    write = {"id": "w", "function": {"name": "write_file",
                                     "arguments": '{"path": "x"}'}}
    runner.execute_tool_calls([write], "", seen, executor, repeats=repeats)
    out = runner.execute_tool_calls([tc], "", seen, executor, repeats=repeats)
    assert out[0]["content"] == "read_file ran"          # a write cleared it


def test_run_loop_forgets_stubs_once_history_is_compacted():
    executed = []
    turns = iter(range(10))

    def chat_fn(payload):
        n = next(turns)
        if n < 3:
            return {"choices": [{"finish_reason": "tool_calls", "message": {
                "tool_calls": [{"id": f"t{n}", "function": {
                    "name": "read_file", "arguments": '{"path": "a"}'}}]}}]}
        return {"choices": [{"finish_reason": "stop",
                             "message": {"content": "done"}}]}

    folds = []

    def prepare(payload):
        if len(payload["messages"]) >= 3:   # fold every earlier result away
            folds.append(1)
            payload["messages"] = [payload["messages"][0],
                                   {"role": "system", "_compaction_marker": True,
                                    "_compaction_generation": len(folds),
                                    "content": "[elided]"}]

    cfg = runner.LoopConfig(force_answer_after=0, prepare_payload=prepare,
                            capped_thinking_enabled=False)
    runner.run_loop({"messages": [{"role": "user", "content": "q"}]}, "",
                    config=cfg, tool_specs=[], chat_fn=chat_fn,
                    tool_executor=lambda n, a, c: executed.append(n) or "body")
    assert len(executed) == 3   # each repeat re-ran: its prior copy was folded


def test_dead_calibration_globals_are_gone():
    from claude_hooks import token_calib
    for name in ("note_output_cap", "last_output_cap", "note_context_overflow",
                 "consume_context_overflow"):
        assert not hasattr(token_calib, name)


def test_the_trigger_leaves_the_output_room_after_the_reserve():
    # A prompt at the trigger must still get ``output_room_min`` of cap once
    # the estimate's reserve is taken; otherwise the starved-cap latch fires
    # on every descent, ~3k before the trigger (seen on the C bench: 8 of
    # 12 folds on a 32k window were starved-cap, not trigger).
    class Client:
        base_url = "x"

        def context_length(self, model):
            return 32_768

    knobs = budget.Knobs()
    body = "def x():\n    a = b + 1  # step\n" * 120
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]
    seen_below_trigger = 0
    for i in range(60):
        msgs += _read(i, f"f{i}.py", size=1)[:1] + [
            {"role": "tool", "tool_call_id": f"r{i}", "name": "read_file",
             "content": body}]
        plan = budget.plan(Client(), "m", msgs)
        if plan.needs_compaction:
            break
        seen_below_trigger += 1
        assert not council._starved_cap(plan, knobs, f"s{i}"), (
            plan.prompt_tokens, plan.output_tokens, plan.trigger_tokens)
    assert seen_below_trigger > 5
