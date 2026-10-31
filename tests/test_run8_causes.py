"""Council run 8 (csl-2026-10-18-1911-d11b): the coder lost write_file at
turn 54 having written nothing, after one ignored nudge at turn 12, on one
task naming six findings; four researcher lanes wrote probes in /scratch
whose relative imports resolved from /scratch."""
from __future__ import annotations

import inspect

from claude_hooks.agent_loop import runner
from claude_hooks.test_reruns import (NOTHING_WRITTEN, NOTHING_WRITTEN_AGAIN,
                                      nothing_written_check)
from claude_hooks.tool_registry import shell_provider
from claude_hooks.tool_registry.shell_provider import import_hint
from consultants.engine import coder
from consultants.engine.coder import parse_coder_tasks, split_coder_tasks
from consultants.engine.state_v2 import CoderTaskItem

REPO = "/repo/cline-sync"


# --- researcher probes ----------------------------------------------------

def test_relative_import_from_scratch_gets_the_absolute_path():
    out = ("error: Cannot find module './sdk/core/grep' from "
           "'/scratch/crlf_probe.ts'")
    hint = import_hint(out, REPO, exists=lambda p: p == f"{REPO}/sdk/core/grep.ts")
    assert "resolved from /scratch" in hint
    assert f'import it as "{REPO}/sdk/core/grep"' in hint
    assert hint.startswith("\n[harness]")


def test_no_existing_target_still_says_why():
    out = "Cannot find module '../src/x' from '/tmp/p.ts'"
    hint = import_hint(out, REPO, exists=lambda p: False)
    assert "resolved from /tmp" in hint
    assert "absolute path" in hint


def test_other_failures_get_no_hint():
    assert import_hint("Cannot find module 'vitest' from '/scratch/p.ts'",
                       REPO) == ""
    assert import_hint("Cannot find module './x' from '/repo/a.ts'", REPO) == ""
    assert import_hint("", REPO) == ""


def test_recipe_says_where_relative_imports_resolve():
    assert "relative imports resolve from the script's own" in \
        shell_provider.JS_RECIPE
    src = inspect.getsource(shell_provider)
    assert "out += import_hint(" in src


# --- the nudge repeats ----------------------------------------------------

def _tc(name, i):
    return {"id": f"c{i}", "type": "function",
            "function": {"name": name, "arguments": f'{{"path": "f{i}"}}'}}


def test_nudge_repeats_every_twelve_turns_and_sharpens():
    check = nothing_written_check(12, every=12, strip=54, cap=60)
    fired = {i + 1: check(i, []) for i in range(60)}
    assert [t for t, n in fired.items() if n] == [12, 24, 36, 48, 60]
    assert fired[12] == NOTHING_WRITTEN.format(turn=11)
    assert "2 reminders" in fired[36]
    assert "From turn 54 only write_file is left" in fired[48]
    wrote = [{"role": "assistant", "tool_calls": [_tc("write_file", 0)]}]
    assert check(23, wrote) is None


def test_once_mode_is_unchanged():
    check = nothing_written_check(4)
    assert check(2, []) is None
    assert check(3, []) == NOTHING_WRITTEN.format(turn=3)
    assert check(9, []) == NOTHING_WRITTEN.format(turn=9)
    assert NOTHING_WRITTEN_AGAIN not in (check(3, []), check(9, []))


def _loop(cfg, script):
    sent, ran = [], []

    def chat_fn(payload):
        sent.append(dict(payload, messages=[dict(m) for m in payload["messages"]]))
        return script(len(sent) - 1, payload)

    out = runner.run_loop(
        {"model": "m", "messages": [{"role": "user", "content": "q"}]}, "/tmp",
        config=cfg,
        tool_specs=[{"type": "function", "function": {
            "name": n, "parameters": {"type": "object"}}}
            for n in ("read_file", "write_file")],
        chat_fn=chat_fn,
        tool_executor=lambda n, a, c: ran.append(n) or "ok")
    return out, sent, ran


def _calls(*tcs):
    return {"choices": [{"message": {"role": "assistant", "content": "",
                                     "tool_calls": list(tcs)},
                         "finish_reason": "tool_calls"}]}


def _answer(text="Report."):
    return {"choices": [{"message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}]}


def test_run_loop_repeats_turn_check_when_asked():
    events = []
    cfg = runner.LoopConfig(
        max_iterations=13, force_answer_after=0, force_first_tool_call=False,
        turn_check=nothing_written_check(3, every=3), turn_check_repeat=True,
        on_guard=events.append)
    _loop(cfg, lambda n, p: _calls(_tc("read_file", n)) if n < 12 else _answer())
    assert [e["iteration"] for e in events if e["guard"] == "turn_check"] \
        == [2, 5, 8, 11]


def test_run_loop_once_by_default():
    events = []
    cfg = runner.LoopConfig(
        max_iterations=13, force_answer_after=0, force_first_tool_call=False,
        turn_check=nothing_written_check(3, every=3), on_guard=events.append)
    _loop(cfg, lambda n, p: _calls(_tc("read_file", n)) if n < 12 else _answer())
    assert len([e for e in events if e["guard"] == "turn_check"]) == 1


# --- write_file survives the strip ----------------------------------------

def _after_strip(n, p):
    if n < 3:
        return _calls(_tc("read_file", n))
    if n == 3:
        return _calls(_tc("write_file", n), _tc("read_file", 100 + n))
    return _answer()


def test_kept_tool_runs_after_the_strip_and_the_rest_are_off():
    cfg = runner.LoopConfig(max_iterations=10, force_answer_after=3,
                            force_first_tool_call=False,
                            keep_after_strip=("write_file",))
    out, sent, ran = _loop(cfg, _after_strip)
    assert [t["function"]["name"] for t in sent[3]["tools"]] == ["write_file"]
    assert ran == ["read_file"] * 3 + ["write_file"]
    tool_msgs = [m for m in sent[4]["messages"] if m["role"] == "tool"]
    assert tool_msgs[-2]["content"] == runner.TOOLS_NARROWED_ANSWER.format(
        tools="write_file")
    assert tool_msgs[-2]["tool_call_id"] == "c103"
    assert tool_msgs[-1]["content"] == "ok"
    assert out["choices"][0]["message"]["content"] == "Report."


def test_without_keep_every_call_is_refused_as_before():
    cfg = runner.LoopConfig(max_iterations=10, force_answer_after=3,
                            force_first_tool_call=False)
    out, sent, ran = _loop(cfg, _after_strip)
    assert "tools" not in sent[3]
    assert ran == ["read_file"] * 3


def test_the_coder_keeps_write_file_and_is_nudged_repeatedly():
    src = inspect.getsource(coder)
    assert 'keep_after_strip=("write_file",)' in src
    assert "turn_check_repeat=True" in src
    assert "nothing_written_check(12, every=12, strip=54, cap=60)" in src


# --- coder tasks are split ------------------------------------------------

RUN8 = ("Write failing tests for csf-1, csf-9, csf-6, csf-2, csf-7 and the "
        "unsettled csf-19, then implement the fixes as patches in the worktree.")


def _gate(*tasks):
    import json
    return "```json\n" + json.dumps({"requires_code_generation": True,
                                     "coder_tasks": list(tasks)}) + "\n```"


def test_run8_task_becomes_three_lanes_of_two():
    out = parse_coder_tasks(_gate({"task": RUN8, "path": "a.test.ts"}))
    assert [t.lane_idx for t in out] == [0, 1, 2]
    assert [t.task.split("THIS LANE: ")[1].split(" only")[0] for t in out] \
        == ["csf-1, csf-9", "csf-6, csf-2", "csf-7, csf-19"]
    assert all(t.task.startswith(RUN8) for t in out)
    assert all(t.path == "" for t in out)


def test_small_tasks_are_left_alone():
    tasks = [CoderTaskItem(task="fix csf-1 and csf-2", path="p", lane_idx=0),
             CoderTaskItem(task="write a parser", path="q", lane_idx=1)]
    assert split_coder_tasks(tasks) == tasks


def test_never_more_than_four_lanes():
    ids = ", ".join(f"csf-{i}" for i in range(1, 12))
    out = split_coder_tasks([CoderTaskItem(task=f"fix {ids}")])
    assert len(out) == 4
    covered = [x for t in out
               for x in t.task.split("THIS LANE: ")[1].split(" only")[0].split(", ")]
    assert covered == [f"csf-{i}" for i in range(1, 12)]


def test_planner_lanes_beyond_four_are_not_merged():
    tasks = [CoderTaskItem(task=f"write module {i}") for i in range(5)]
    assert len(split_coder_tasks(tasks)) == 5
