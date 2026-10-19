"""Plan G5: on the typed protocol a follow-up is routed by one tiny call
(``council.follow_up_route_node``); off, the follow-up graph is unchanged."""
from __future__ import annotations

import threading

from consultants.engine import council
from consultants.engine.graph import (FOLLOW_UP_ROUTE_NODE, GraphDeps,
                                      build_follow_up_graph)


class _Seq:
    """A chat client answering from a list, then repeating the last."""

    def __init__(self, *contents, fail=False):
        self.contents, self.fail, self.payloads = list(contents), fail, []
        self._lock = threading.Lock()

    def chat(self, payload, *, think=True):
        with self._lock:
            self.payloads.append(payload)
            if self.fail:
                raise RuntimeError("down")
            text = self.contents.pop(0) if len(self.contents) > 1 \
                else self.contents[0]
        return {"choices": [{"message": {"role": "assistant",
                                         "content": text}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2}}


PRIOR = [{"role": "system", "content": "S"},
         {"role": "user", "content": "Why do retries stop?"},
         {"role": "assistant", "content": "EARLIER ANSWER: the cap is 3."}]


def test_parse_fails_toward_research():
    assert council.parse_follow_up_route('{"route": "synthesize"}') == "synthesize"
    assert council.parse_follow_up_route('route: Fresh') == "fresh"
    assert council.parse_follow_up_route("I think we should look again") == "research"
    assert council.parse_follow_up_route("") == "research"


def test_the_node_asks_once_with_the_earlier_answer():
    chat = _Seq('{"route": "synthesize"}')
    out = council.follow_up_route_node({"question": "Summarise that"},
                                       chat_client=chat, model="m",
                                       prior_messages=PRIOR)
    assert out["follow_up_route"] == "synthesize"
    [p] = chat.payloads
    assert "EARLIER ANSWER: the cap is 3." in p["messages"][-1]["content"]
    assert "Summarise that" in p["messages"][-1]["content"]
    assert p["think"] is False


def test_no_earlier_answer_or_a_failed_call_researches():
    chat = _Seq('{"route": "synthesize"}')
    assert council.follow_up_route_node(
        {"question": "q"}, chat_client=chat, model="m",
        prior_messages=[])["follow_up_route"] == "research"
    assert chat.payloads == []                       # nothing to route on
    assert council.follow_up_route_node(
        {"question": "q"}, chat_client=_Seq("x", fail=True), model="m",
        prior_messages=PRIOR)["follow_up_route"] == "research"


def _deps(tmp_path, *, route: bool, synth):
    roles = ("researcher", "synthesizer")
    return GraphDeps(chat_clients={"researcher": _Seq("NEW FINDING"),
                                   "synthesizer": synth},
                     models={r: "m" for r in roles}, enabled_roles=roles,
                     cwd=str(tmp_path), tool_executor=lambda *a, **kw: "",
                     tool_specs=[], grounding_msgs=[], disable_cache=True,
                     prior_messages_by_role={"synthesizer": PRIOR},
                     follow_up_route=route)


def _state(tmp_path):
    st = council.initial_state(question="Summarise that in one line",
                               cwd=str(tmp_path), models={},
                               topology="council", effort="medium")
    st.update(plan="(follow-up)", plan_item="Summarise that in one line",
              lane_idx=0, research=[])
    return st


def test_a_synthesize_route_skips_the_researcher(tmp_path):
    synth = _Seq('{"route": "synthesize"}', "ONE LINE: cap 3.")
    deps = _deps(tmp_path, route=True, synth=synth)
    out = build_follow_up_graph(deps).invoke(_state(tmp_path))
    assert deps.chat_clients["researcher"].payloads == []
    assert out["final_answer"].startswith("ONE LINE")
    assert out["follow_up_route"] == "synthesize"


def test_a_research_route_runs_as_before(tmp_path):
    synth = _Seq('{"route": "research"}', "ANSWER")
    deps = _deps(tmp_path, route=True, synth=synth)
    build_follow_up_graph(deps).invoke(_state(tmp_path))
    assert deps.chat_clients["researcher"].payloads


def test_off_the_graph_has_no_route_node(tmp_path):
    g = build_follow_up_graph(_deps(tmp_path, route=False, synth=_Seq("A")))
    assert FOLLOW_UP_ROUTE_NODE not in g.get_graph().nodes
    g = build_follow_up_graph(_deps(tmp_path, route=True, synth=_Seq("A")))
    assert FOLLOW_UP_ROUTE_NODE in g.get_graph().nodes


# --- already tried (plan G5) ------------------------------------------------ #

import pytest  # noqa: E402


def test_tried_parses_and_renders_verbatim():
    items = council.parse_tried([{"what": " ran pytest ", "output": "E  boom\n1 failed"}])
    assert items == [{"what": "ran pytest", "output": "E  boom\n1 failed"}]
    text = council.render_tried(items)
    assert text.startswith(council.ALREADY_TRIED_HEADER)
    assert "1. ran pytest" in text and "   E  boom\n   1 failed" in text
    assert council.parse_tried(None) == []


@pytest.mark.parametrize("bad", [
    "ran it", [{"output": "x"}], [{"what": ""}], [{"what": "x", "output": 3}],
    [{"what": "x"}] * (council.TRIED_MAX_ITEMS + 1)])
def test_a_wrong_tried_shape_is_refused(bad):
    with pytest.raises(ValueError):
        council.parse_tried(bad)


def test_every_role_gets_the_tried_doc():
    from consultants.server.runner import _tried_docs
    assert _tried_docs({}) == []
    [doc] = _tried_docs({"tried": [{"what": "read a.py", "output": "ok"}]})
    assert doc.role == "any" and doc.source == "already_tried"
    assert "1. read a.py" in doc.text


def test_the_ask_route_refuses_a_bad_tried_and_passes_a_good_one(
        isolated_home, tmp_path):
    from fastapi.testclient import TestClient
    from consultants.server.app import create_app
    seen = []
    app = create_app(run_council=lambda state, ri: seen.append(ri))
    proj = tmp_path / "p"
    proj.mkdir()
    with TestClient(app) as client:
        r = client.post("/v1/consult", json={"message": "q", "cwd": str(proj),
                                             "tried": "not a list",
                                             "skip_preflight": True})
        assert r.status_code == 400 and "tried" in r.json()["detail"]
        r = client.post("/v1/consult", json={
            "message": "q", "cwd": str(proj), "skip_preflight": True,
            "tried": [{"what": "ran x", "output": "y"}]})
        assert r.status_code == 200
    deadline = __import__("time").time() + 5
    while not seen and __import__("time").time() < deadline:
        __import__("time").sleep(0.05)
    assert seen and seen[0]["tried"] == [{"what": "ran x", "output": "y"}]


def test_the_runner_wires_both():
    """The runner turns the route on from ``[protocol] typed`` (never for
    a retry-lanes child) and seeds the tried doc into the council."""
    import inspect
    from consultants.server import runner
    src = inspect.getsource(runner)
    assert ('deps.follow_up_route = bool(getattr(getattr(cfg, "protocol", None),\n'
            '                                            "typed", False)) and not retry_lanes') in src
    assert '_tried = _tried_docs(runner_input)' in src
    assert 'initial["additional_context"] = _tried' in src
