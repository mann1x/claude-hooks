"""Plan D9: an xauto run reruns its first-round lanes that failed on
infrastructure, once, before the critic — what ``retry-lanes`` does after
the run, done inside it."""
from __future__ import annotations

import threading

import pytest

pytest.importorskip("langgraph.graph")

from consultants.engine import council  # noqa: E402
from consultants.engine.graph import GraphDeps, build_council_graph  # noqa: E402
from consultants.engine.recorder import MessageRecorder, RecorderMeta  # noqa: E402

PLAN = "1. read a\n2. read b\n3. read c"


class _Stub:
    def __init__(self, content, fail_on=None, fail_times=0, exc=ConnectionError):
        self.content, self.payloads = content, []
        self.fail_on, self.fail_times, self.exc = fail_on, fail_times, exc
        self._lock = threading.Lock()

    def chat(self, payload, *, think=True):
        text = payload["messages"][-1]["content"]
        with self._lock:
            self.payloads.append(payload)
            if self.fail_on and self.fail_on in text and self.fail_times > 0:
                self.fail_times -= 1
                raise self.exc("connection reset by peer")
        return {"choices": [{"message": {"role": "assistant",
                                         "content": self.content}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


def _run(tmp_path, *, retry: bool, fail_times=1, exc=ConnectionError):
    roles = ("planner", "researcher", "critic", "synthesizer")
    clients = {"planner": _Stub(PLAN),
               "researcher": _Stub("FINDING", fail_on="read b",
                                   fail_times=fail_times, exc=exc),
               "critic": _Stub("The research is sufficient.\nVERDICT: ready"),
               "synthesizer": _Stub("ANSWER")}
    rec = MessageRecorder(tmp_path / "transcript.db", meta=RecorderMeta(
        sid="csl-x", cwd=str(tmp_path), question="q", effort="xauto",
        topology="council", models={}))
    deps = GraphDeps(chat_clients=clients, models={r: "m" for r in roles},
                     enabled_roles=roles, cwd=str(tmp_path),
                     tool_executor=lambda *a, **kw: "", tool_specs=[],
                     grounding_msgs=[], disable_cache=True, recorder=rec,
                     xauto_lane_retry=retry)
    state = council.initial_state(question="the question", cwd=str(tmp_path),
                                  models={}, topology="council", effort="xauto")
    graph = build_council_graph(deps)
    out = graph.invoke(state)
    rec.close()
    return graph, clients, out, rec


@pytest.fixture(autouse=True)
def _one_item_a_lane(monkeypatch):
    monkeypatch.setattr(council, "FANOUT_MAX_LANES", 3)


def _asked(stub, needle):
    return [p for p in stub.payloads if needle in p["messages"][-1]["content"]]


def test_an_infra_failed_lane_is_rerun_once_before_the_critic(tmp_path):
    graph, clients, out, rec = _run(tmp_path, retry=True)
    assert "lane_retry" in graph.get_graph().nodes
    assert len(_asked(clients["researcher"], "read b")) == 2
    assert len(_asked(clients["researcher"], "read a")) == 1
    critic = clients["critic"].payloads[0]["messages"][-1]["content"]
    assert critic.count("FINDING") == 3          # the retry is in before it
    assert int(out.get("research_rounds_used") or 0) == 3   # not a round
    import sqlite3
    con = sqlite3.connect(tmp_path / "transcript.db")
    rows = con.execute("SELECT role FROM runtime_events WHERE kind='lane_retry'"
                       ).fetchall()
    assert rows == [("xauto_lane_retry",)]


def test_a_lane_that_fails_again_is_not_retried_twice(tmp_path):
    _, clients, out, _ = _run(tmp_path, retry=True, fail_times=5)
    assert len(_asked(clients["researcher"], "read b")) == 2
    assert out["final_answer"].startswith("ANSWER")


def test_a_task_error_is_not_retried(tmp_path):
    _, clients, *_ = _run(tmp_path, retry=True, exc=ValueError)
    assert len(_asked(clients["researcher"], "read b")) == 1


def test_off_the_node_is_not_wired_and_nothing_reruns(tmp_path):
    graph, clients, *_ = _run(tmp_path, retry=False)
    assert "lane_retry" not in graph.get_graph().nodes
    assert len(_asked(clients["researcher"], "read b")) == 1


def test_config_and_cli():
    from consultants import cli
    from consultants import config as cc
    assert cc.ConsultantsConfig().runtime.xauto_retry_infra_lanes is True
    args = cli.build_parser().parse_args(["config", "set-xauto-retry", "off"])
    assert args.fn is cli.cmd_config_set_xauto_retry and args.state == "off"
