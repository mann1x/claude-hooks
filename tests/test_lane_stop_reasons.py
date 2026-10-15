"""Plan D7: how a lane ended — stop_reason and failure_class on its turn,
its node_exit row, its NodeFinished event and `show`."""
from __future__ import annotations

import json
from types import SimpleNamespace

from consultants import cli
from consultants.engine import council, events
from consultants.engine.recorder import MessageRecorder, RecorderMeta
from claude_hooks.get_advice.chat_client import ChatFault


def test_failure_classes():
    f = lambda c: events.failure_class_of(ChatFault("x", fault_class=c))
    assert f("transport") == f("refusal") == "infra"
    assert f("model_missing") == "model_missing"
    assert f("context_overflow") == "context_overflow"
    assert f("tool_call_unparsable") == "tool_call_unparsable"
    assert f("bad_request") == "task_error"
    from consultants.engine.stall import CancelledByOrchestrator, StallError
    assert events.failure_class_of(CancelledByOrchestrator()) == "cancelled"
    assert events.failure_class_of(StallError(
        "stalled", outcome=None, attempts=3, tokens_emitted=0,
        elapsed_s=1.0)) == "infra"
    assert events.failure_class_of(
        council.ContextOverflowUnrecovered("x")) == "context_overflow"
    assert events.failure_class_of(ValueError("bug")) == "task_error"


def test_loop_stop():
    assert events.loop_stop({}) == ("completed", None)
    assert events.loop_stop({"_stop_reason": "iteration_cap",
                             "_failure_class": "iteration_cap"}) \
        == ("iteration_cap", "iteration_cap")


_CLIENT = SimpleNamespace(chat=lambda payload: {})


def _state():
    s = council.initial_state(question="q", cwd="/proj",
                              models={"researcher": "m"},
                              topology="council", effort="medium")
    s["plan"] = "p"
    return s


def _recorder(tmp_path):
    return MessageRecorder(tmp_path / "transcript.db", meta=RecorderMeta(
        sid="csl-d7", cwd=str(tmp_path), question="q", effort="medium",
        topology="council", models={"researcher": "m"}))


def _finished(monkeypatch):
    seen = []
    monkeypatch.setattr(events, "emit", lambda e: seen.append(e) or True)
    return seen


def test_a_capped_lane_carries_its_stop_reason(tmp_path, monkeypatch):
    seen = _finished(monkeypatch)

    def capped_loop(payload, cwd, **kw):
        return {"choices": [{"message": {"role": "assistant",
                                         "content": "report at f.py:1"},
                             "finish_reason": "stop"}],
                "_stop_reason": "iteration_cap",
                "_failure_class": "iteration_cap"}

    rec = _recorder(tmp_path)
    try:
        update = council.researcher_node(
            _state(), chat_client=_CLIENT, recorder=rec,
            tool_executor=lambda *a, **k: "", tool_specs=[],
            grounding_msgs=[], model="m", cwd="/proj", loop_runner=capped_loop)
        turn = update["turns"][0]
        assert (turn.stop_reason, turn.failure_class) == ("iteration_cap",
                                                          "iteration_cap")
        done = [e for e in seen if isinstance(e, events.NodeFinished)]
        assert done[-1].stop_reason == "iteration_cap" and done[-1].ok
        lanes = cli._session_lane_stops(tmp_path / "transcript.db")
        assert lanes == [{"role": "researcher", "round": 1, "lane_idx": None,
                          "stop_reason": "iteration_cap",
                          "failure_class": "iteration_cap"}]
    finally:
        rec.close()


def test_a_completed_lane_says_completed(tmp_path, monkeypatch):
    seen = _finished(monkeypatch)
    update = council.researcher_node(
        _state(), chat_client=_CLIENT,
        tool_executor=lambda *a, **k: "", tool_specs=[], grounding_msgs=[],
        model="m", cwd="/proj", loop_runner=lambda p, c, **k: {
            "choices": [{"message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}]})
    assert update["turns"][0].stop_reason == "completed"
    assert update["turns"][0].failure_class is None


def test_a_tombstoned_lane_names_its_class(tmp_path, monkeypatch):
    seen = _finished(monkeypatch)

    def dead(payload, cwd, **kw):
        raise ChatFault("HTTP 502", fault_class="transport")

    rec = _recorder(tmp_path)
    try:
        update = council.researcher_node(
            _state(), chat_client=_CLIENT, recorder=rec,
            tool_executor=lambda *a, **k: "", tool_specs=[],
            grounding_msgs=[], model="m", cwd="/proj", loop_runner=dead)
        assert update["turns"][0].failure_class == "infra"
        done = [e for e in seen if isinstance(e, events.NodeFinished)]
        assert not done[-1].ok and done[-1].failure_class == "infra"
        lanes = cli._session_lane_stops(tmp_path / "transcript.db")
        assert lanes[0]["failure_class"] == "infra"
        assert "HTTP 502" in lanes[0]["error"]
    finally:
        rec.close()


def test_show_lists_the_lanes(tmp_path, monkeypatch):
    from consultants.engine import storage
    sdir = tmp_path / "sess"
    sdir.mkdir()
    (sdir / storage.SUMMARY_FILENAME).write_text("# s")
    rec = MessageRecorder(sdir / storage.TRANSCRIPT_DB_FILENAME,
                          meta=RecorderMeta(sid="csl-d7", cwd=str(tmp_path),
                                            question="q", effort="medium",
                                            topology="council",
                                            models={"researcher": "m"}))
    rec.record_node(role="researcher", kind="node_exit", lane_idx=1,
                    stop_reason="loop_guard", failure_class="loop_guard")
    rec.record_node(role="planner", kind="node_exit")
    rec.close()
    monkeypatch.setattr(storage, "session_dir", lambda cwd, sid: sdir)
    printed = []
    monkeypatch.setattr("builtins.print", lambda s: printed.append(s))
    cli.cmd_show(SimpleNamespace(cwd=str(tmp_path), sid="csl-d7", raw=False),
                 base="")
    out = json.loads(printed[0])
    assert out["lanes"] == [{"role": "researcher", "round": 1, "lane_idx": 1,
                             "stop_reason": "loop_guard",
                             "failure_class": "loop_guard"}]


def test_a_tool_executor_lane_carries_its_stop_reason(tmp_path, monkeypatch):
    from consultants.engine.state_v2 import ToolPlanItem
    from consultants.engine.tool_executor import tool_executor_node
    seen = _finished(monkeypatch)
    item = ToolPlanItem(intent="find X", why="audit", lane_idx=0,
                        parent_round=1, suggested_tools=["grep"])
    rec = _recorder(tmp_path)
    try:
        tool_executor_node(
            {"tool_plan_item": item, "lane_idx": 0, "question": "q"},
            chat_client=_CLIENT, tool_executor=lambda *a: "", tool_specs=[],
            grounding_msgs=[], model="m", cwd="/tmp", recorder=rec,
            loop_runner=lambda p, c, **k: {
                "choices": [{"message": {"role": "assistant", "content": "x"},
                             "finish_reason": "stop"}],
                "_stop_reason": "loop_guard", "_failure_class": "loop_guard"})
        done = [e for e in seen if isinstance(e, events.NodeFinished)]
        assert done[-1].role == "tool_executor"
        assert done[-1].stop_reason == "loop_guard"
        lanes = cli._session_lane_stops(tmp_path / "transcript.db")
        assert lanes[-1]["role"] == "tool_executor"
        assert lanes[-1]["stop_reason"] == "loop_guard"
    finally:
        rec.close()


def test_each_coder_attempt_carries_how_it_ended(tmp_path, monkeypatch):
    from consultants.engine.coder import coder_node
    from consultants.engine.state_v2 import CoderTaskItem
    seen = _finished(monkeypatch)

    def loop(payload, cwd, **kw):
        if payload["model"] == "a":
            raise ChatFault("gone", fault_class="model_missing")
        return {"choices": [{"message": {"role": "assistant",
                                         "content": "done"},
                             "finish_reason": "stop"}]}

    rec = _recorder(tmp_path)
    try:
        out = coder_node(
            {"coder_task_item": CoderTaskItem(task="write x", lane_idx=0),
             "lane_idx": 0, "question": "q"},
            grounding_msgs=[], cwd=str(tmp_path), sid="csl-d7",
            recorder=rec, loop_runner=loop,
            model_chain_resolver=lambda lang: [(_CLIENT, "a"), (_CLIENT, "b")])
        assert out["coder_artifacts"][0].error
        done = [e for e in seen if isinstance(e, events.NodeFinished)]
        assert [(e.stop_reason, e.failure_class) for e in done] == [
            ("model_missing", "model_missing"), ("no_artifacts", "task_error")]
        lanes = cli._session_lane_stops(tmp_path / "transcript.db")
        assert [(r["role"], r["stop_reason"]) for r in lanes[-2:]] == [
            ("coder", "model_missing"), ("coder", "no_artifacts")]
    finally:
        rec.close()
