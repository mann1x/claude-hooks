"""Plan H: decision-model seams in the council (review, confidence,
struggle, citations), their config and their off-by-default parity."""
from __future__ import annotations

import json

import pytest

from claude_hooks import decision_client as dc
from consultants import config as cc
from consultants.engine import citation_linter, council, decision
from consultants.engine.control import decision_settings


@pytest.fixture(autouse=True)
def _fresh():
    dc.reset_caches()
    yield
    dc.reset_caches()


def _state(seams=("review", "confidence", "struggle", "citations"), **extra):
    return {"question": "Why do retries stop?", "runtime_control": {
        "decision": {"url": None, "model": "jev-latest", "key_file": None,
                     "floor": 0.6, "high_stakes_floor": 0.85,
                     "timeout_s": 5.0, "seams": list(seams)}}, **extra}


class Fake:
    """Patch target for ``dc.evaluate``: answers by question id."""

    def __init__(self, answers=None, fail=False):
        self.answers = answers or {}
        self.fail = fail
        self.calls = []

    def __call__(self, endpoint, state, questions, **kw):
        self.calls.append({"endpoint": endpoint, "state": state,
                           "questions": questions})
        if self.fail:
            raise dc.DecisionError("down")
        return dc.Response(model="jev-1.13.0", answers={
            k: self.answers[k] for k in questions if k in self.answers})


class Rec:
    def __init__(self):
        self.events = []

    def record_event(self, **kw):
        self.events.append(kw)


# --- config ------------------------------------------------------------- #

def test_decision_is_off_by_default_and_not_seeded():
    cfg = cc.ConsultantsConfig()
    assert cfg.decision.enabled is False
    assert decision_settings(cfg) == {}
    assert "[decision]" not in cc._render(cfg)


def test_config_round_trip_keeps_the_key_out(tmp_path, monkeypatch):
    key = tmp_path / "jev.key"
    key.write_text("api-secret-value")
    cfg = cc.ConsultantsConfig()
    cfg.decision.enabled = True
    cfg.decision.key_file = str(key)
    cfg.decision.seams = ["review", "citations"]
    text = cc._render(cfg)
    assert "api-secret-value" not in text
    seeded = decision_settings(cfg)
    assert seeded["key_file"] == str(key) and "api_key" not in seeded
    assert "api-secret-value" not in json.dumps(seeded)


def test_seams_are_validated():
    assert cc.check_decision_seams("review, struggle") == ["review", "struggle"]
    with pytest.raises(ValueError):
        cc.check_decision_seams(["review", "vibes"])


# --- the entry point ----------------------------------------------------- #

def test_ask_is_none_when_the_seam_is_off(monkeypatch):
    fake = Fake()
    monkeypatch.setattr(dc, "evaluate", fake)
    assert decision.ask({}, "review", "s", {"q": dc.noul("x")}) is None
    assert decision.ask(_state(seams=("citations",)), "review", "s",
                        {"q": dc.noul("x")}) is None
    assert not fake.calls


def test_a_failed_call_is_none_and_recorded(monkeypatch):
    monkeypatch.setattr(dc, "evaluate", Fake(fail=True))
    rec = Rec()
    assert decision.ask(_state(), "review", "s", {"q": dc.noul("x")},
                        recorder=rec) is None
    assert rec.events[0]["kind"] == "decision"
    assert rec.events[0]["payload"]["error"] == "down"


# --- review + confidence -------------------------------------------------- #

def test_review_and_confidence_share_one_call(monkeypatch):
    fake = Fake({"next": {"type": "choice", "choice": "followup",
                          "confidence": 0.7,
                          "probabilities": {"accept": 0.1, "followup": 0.8,
                                            "research": 0.1}},
                 "sound": {"type": "noul", "noul": 0.2}})
    monkeypatch.setattr(dc, "evaluate", fake)
    out = decision.review_answer(_state(), "q", "partial answer")
    assert len(fake.calls) == 1
    assert set(fake.calls[0]["questions"]) == {"next", "sound"}
    assert out["recommendation"]["choice"] == "followup"
    assert out["recommendation"]["confident"] is True
    assert out["confidence"] == 0.2


def test_synthesizer_seams_put_the_scored_confidence_on_the_channel(
        monkeypatch):
    monkeypatch.setattr(dc, "evaluate", Fake({
        "next": {"type": "choice", "choice": "accept", "confidence": 0.9,
                 "probabilities": {"accept": 0.95}},
        "sound": {"type": "noul", "noul": 0.0}}))
    text, scored, review = council._synthesizer_decision_seams(
        _state(seams=("review", "confidence")), "answer")
    assert text == "answer" and scored == 0.0   # a 0.0 is kept, not dropped
    assert review["choice"] == "accept"


def test_synthesizer_seams_are_inert_without_decision(monkeypatch):
    fake = Fake()
    monkeypatch.setattr(dc, "evaluate", fake)
    text = "answer with `a.py:3`"
    assert council._synthesizer_decision_seams({}, text) == (text, None, None)
    assert not fake.calls


def test_critic_scored_confidence(monkeypatch):
    monkeypatch.setattr(dc, "evaluate", Fake({
        "enough": {"type": "noul", "noul": 0.31}}))
    st = _state(seams=("confidence",), research=["found the unit bug"])
    assert council._scored_critic_confidence(st) == 0.31
    assert council._scored_critic_confidence(
        _state(seams=("review",), research=["x"])) is None


# --- struggle ----------------------------------------------------------- #

def _tool_turns(n):
    msgs = []
    for i in range(n):
        msgs += [{"role": "assistant", "tool_calls": [{
                     "id": f"t{i}", "function": {
                         "name": "grep", "arguments": '{"pattern": "drop"}'}}]},
                 {"role": "tool", "tool_call_id": f"t{i}",
                  "content": "0 matches"}]
    return msgs


def test_struggle_watch_is_the_budgeter_itself_when_off():
    def prepare(payload):
        return None
    assert council._struggle_watch({}, prepare) is prepare


def test_struggle_nudges_once_when_confidently_stuck(monkeypatch):
    fake = Fake({"stuck": {"type": "noul", "noul": 0.97}})
    monkeypatch.setattr(dc, "evaluate", fake)
    prepared = []

    def prepare(payload):
        prepared.append(len(payload["messages"]))
    prepare.observe = object()
    watch = council._struggle_watch(_state(seams=("struggle",)), prepare)
    assert watch.observe is prepare.observe       # budgeter hooks kept
    payload = {"messages": [{"role": "user", "content": "q"}] + _tool_turns(6)}
    watch(payload)
    nudges = [m for m in payload["messages"] if m.get("_harness")]
    assert len(nudges) == 1 and "repeating" in nudges[0]["content"]
    payload["messages"] += _tool_turns(12)
    watch(payload)
    assert len([m for m in payload["messages"] if m.get("_harness")]) == 1
    assert len(fake.calls) == 1 and prepared


def test_struggle_does_not_nudge_below_the_high_stakes_floor(monkeypatch):
    monkeypatch.setattr(dc, "evaluate", Fake({
        "stuck": {"type": "noul", "noul": 0.85}}))   # |2p-1| = 0.7 < 0.85
    watch = council._struggle_watch(_state(seams=("struggle",)), None)
    payload = {"messages": _tool_turns(6)}
    watch(payload)
    assert not any(m.get("_harness") for m in payload["messages"])


# --- citations ----------------------------------------------------------- #

def test_semantic_items_pair_each_cite_with_its_code(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "net.py").write_text(
        "import time\n\ndef retry(fn):\n    time.sleep(1.0)\n    return fn()\n")
    text = ("The retry waits longer after each failure (`src/net.py:4`). "
            "Missing: `src/gone.py:9`.")
    items = citation_linter.semantic_items(text, allowed_roots=[str(tmp_path)])
    assert [i["match"] for i in items] == ["src/net.py:4"]
    assert "time.sleep(1.0)" in items[0]["code"]
    assert "waits longer" in items[0]["claim"]


def test_unsupported_cites_are_annotated_once(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "net.py").write_text("def retry():\n    time.sleep(1.0)\n")
    text = "It backs off exponentially (`src/net.py:2`)."
    monkeypatch.setattr(dc, "evaluate", Fake({
        "q0": {"type": "noul", "noul": 0.05}}))
    st = _state(seams=("citations",), cwd=str(tmp_path))
    out, _, _ = council._synthesizer_decision_seams(st, text)
    assert out.count("[unsupported") == 1
    again = citation_linter.annotate_unsupported(out, ["src/net.py:2"])
    assert again == out
    assert citation_linter.semantic_items(
        out, allowed_roots=[str(tmp_path)]) == []
