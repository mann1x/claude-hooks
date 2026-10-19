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
    assert "confidence" not in out                  # never the channel value
    assert out["answer_confidence"]["p"] == 0.2
    assert "AUC 0.85" in out["answer_confidence"]["meaning"]


def test_synthesizer_seams_give_the_answer_score_to_the_reviewer(
        monkeypatch):
    monkeypatch.setattr(dc, "evaluate", Fake({
        "next": {"type": "choice", "choice": "accept", "confidence": 0.9,
                 "probabilities": {"accept": 0.95}},
        "sound": {"type": "noul", "noul": 0.0}}))
    emitted = []
    monkeypatch.setattr(council, "_emit_confidence",
                        lambda s, **kw: emitted.append(s))
    text, review = council._synthesizer_decision_seams(
        _state(seams=("review", "confidence")), "answer")
    assert text == "answer" and review["choice"] == "accept"
    assert review["answer_confidence"]["p"] == 0.0   # a 0.0 is kept
    assert emitted == []                             # nothing on the channel


def test_synthesizer_seams_are_inert_without_decision(monkeypatch):
    fake = Fake()
    monkeypatch.setattr(dc, "evaluate", fake)
    text = "answer with `a.py:3`"
    assert council._synthesizer_decision_seams({}, text) == (text, None)
    assert not fake.calls


def test_a_judge_gets_the_score_as_labelled_advice(monkeypatch):
    fake = Fake({"enough": {"type": "noul", "noul": 0.31}})
    monkeypatch.setattr(dc, "evaluate", fake)
    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "Q"}]
    st = _state(seams=("confidence",), research=["found the unit bug"])
    out = council._with_decision_advice(msgs, st, "critic")
    note = out[-1]["content"]
    assert note.startswith("Q\n\n" + decision.ADVICE_HEADER)
    assert "p = 0.31" in note and "`jev-1.13.0`" in note
    assert "accepted averaged p 0.46" in note       # what Jev's scale means
    assert "CONFIDENCE line stays yours" in note
    assert msgs[-1]["content"] == "Q"               # the input is not mutated
    assert fake.calls[0]["questions"].keys() == {"enough"}
    off = _state(seams=("review",), research=["x"])
    assert council._with_decision_advice(msgs, off, "critic") is msgs
    assert council._with_decision_advice(
        msgs, _state(seams=("confidence",)), "critic") is msgs   # no research


def test_score_meaning_per_server():
    assert "AUC 0.85" in decision.score_meaning("jev-latest")
    for m in ("tev1:latest", "nimble:latest", "clef-flash:latest"):
        assert "barely ranked" in decision.score_meaning(m)
    assert decision.score_meaning("other:7b") == decision.UNMEASURED_MEANING


def test_critic_node_keeps_its_own_confidence(monkeypatch):
    monkeypatch.setattr(dc, "evaluate", Fake({
        "enough": {"type": "noul", "noul": 0.05}}))
    seen = {}

    def turn(chat_client, model, msgs, **kw):
        seen["msgs"] = msgs
        return "DECISION: ready\nCONFIDENCE: 0.8", 1, 1
    monkeypatch.setattr(council, "_role_turn", turn)
    st = _state(seams=("confidence",), research=["found it"], plan="1. look")
    out = council.critic_node(st, chat_client=None, model="m")
    assert out["confidence"] == [0.8]               # the critic's, not 0.05
    assert "p = 0.05" in seen["msgs"][-1]["content"]


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
    out, _ = council._synthesizer_decision_seams(st, text)
    assert out.count("[unsupported") == 1
    again = citation_linter.annotate_unsupported(out, ["src/net.py:2"])
    assert again == out
    assert citation_linter.semantic_items(
        out, allowed_roots=[str(tmp_path)]) == []


# --- shadow (plan H6): asked and recorded, never acted on ----------------- #

def _shadow(shadow, seams=(), **extra):
    st = _state(seams=seams, **extra)
    st["runtime_control"]["decision"]["shadow"] = list(shadow)
    return st


def test_shadow_is_seeded_only_when_set_and_round_trips():
    cfg = cc.ConsultantsConfig()
    cfg.decision.enabled = True
    cfg.decision.seams = ["citations"]
    assert "shadow" not in decision_settings(cfg)       # M12 parity
    assert "shadow" not in cc._render(cfg)
    cfg.decision.seams = []
    assert decision_settings(cfg) == {}                 # nothing asked: off
    cfg.decision.shadow = ["confidence"]
    seeded = decision_settings(cfg)
    assert seeded["seams"] == [] and seeded["shadow"] == ["confidence"]
    assert 'shadow = ["confidence"]' in cc._render(cfg)


def test_a_shadow_seam_is_asked_and_recorded(monkeypatch):
    fake = Fake({"sound": {"type": "noul", "noul": 0.42}})
    monkeypatch.setattr(dc, "evaluate", fake)
    rec = Rec()
    st = _shadow(["confidence"])
    assert decision.enabled(st, "confidence") and decision.shadowed(
        st, "confidence")
    assert not decision.enabled(st, "review")
    out = decision.review_answer(st, "q", "an answer", recorder=rec)
    assert out == {}                                      # nothing to act on
    assert set(fake.calls[0]["questions"]) == {"sound"}
    ev = rec.events[0]
    assert ev["role"] == "synthesizer" and ev["payload"]["shadow"] == [
        "confidence"]
    assert ev["payload"]["answers"]["sound"]["noul"] == 0.42


def test_shadow_confidence_leaves_the_channel_alone(monkeypatch):
    monkeypatch.setattr(dc, "evaluate", Fake({
        "sound": {"type": "noul", "noul": 0.1},
        "enough": {"type": "noul", "noul": 0.1}}))
    emitted = []
    monkeypatch.setattr(council, "_emit_confidence",
                        lambda s, **kw: emitted.append((s, kw["source"])))
    st = _shadow(["confidence"], research=["found it"])
    assert council._synthesizer_decision_seams(st, "answer") == (
        "answer", None)
    msgs = [{"role": "user", "content": "Q"}]
    assert council._with_decision_advice(msgs, st, "critic") is msgs
    assert emitted == []


def test_shadow_review_gives_no_recommendation(monkeypatch):
    monkeypatch.setattr(dc, "evaluate", Fake({
        "next": {"type": "choice", "choice": "research", "confidence": 0.9,
                 "probabilities": {"research": 0.95}},
        "sound": {"type": "noul", "noul": 0.9}}))
    out = decision.review_answer(_shadow(["review"], seams=("confidence",)),
                                 "q", "a")
    assert set(out) == {"answer_confidence"}              # confidence acts
    assert out["answer_confidence"]["p"] == 0.9


def test_shadow_struggle_never_nudges(monkeypatch):
    fake = Fake({"stuck": {"type": "noul", "noul": 0.99}})
    monkeypatch.setattr(dc, "evaluate", fake)
    watch = council._struggle_watch(_shadow(["struggle"]), None)
    payload = {"messages": _tool_turns(6)}
    watch(payload)
    assert fake.calls and not any(m.get("_harness") for m in payload["messages"])


def test_shadow_citations_leave_the_answer_as_written(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "net.py").write_text("def retry():\n    time.sleep(1.0)\n")
    text = "It backs off exponentially (`src/net.py:2`)."
    fake = Fake({"q0": {"type": "noul", "noul": 0.02}})
    monkeypatch.setattr(dc, "evaluate", fake)
    out, _ = council._synthesizer_decision_seams(
        _shadow(["citations"], cwd=str(tmp_path)), text)
    assert fake.calls and out == text


def test_shadow_wins_over_seams(monkeypatch):
    monkeypatch.setattr(dc, "evaluate", Fake({
        "enough": {"type": "noul", "noul": 0.2}}))
    fake = Fake({"enough": {"type": "noul", "noul": 0.2}})
    monkeypatch.setattr(dc, "evaluate", fake)
    st = _shadow(["confidence"], seams=("confidence",), research=["x"])
    msgs = [{"role": "user", "content": "Q"}]
    assert council._with_decision_advice(msgs, st, "critic") is msgs
    assert fake.calls                                # asked and recorded


def test_set_decision_shadow(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    cfg = cc.set_decision(enabled=True, seams="", shadow="confidence",
                          scope="project", cwd=tmp_path)
    assert cfg.decision.seams == [] and cfg.decision.shadow == ["confidence"]
    with pytest.raises(ValueError):
        cc.set_decision(shadow="vibes", scope="project", cwd=tmp_path)
    from consultants import cli
    args = cli.build_parser().parse_args(
        ["config", "set-decision", "--shadow", "confidence"])
    assert args.shadow == "confidence"


def test_synthesizer_node_keeps_its_own_confidence(monkeypatch):
    monkeypatch.setattr(dc, "evaluate", Fake({
        "enough": {"type": "noul", "noul": 0.05},
        "sound": {"type": "noul", "noul": 0.07}}))
    seen = {}

    def turn(chat_client, model, msgs, **kw):
        seen["msgs"] = msgs
        return "The answer.\nCONFIDENCE: 0.9", 1, 1
    monkeypatch.setattr(council, "_role_turn", turn)
    st = _state(seams=("confidence",), research=["found it"], plan="1. look")
    out = council.synthesizer_node(st, chat_client=None, model="m")
    assert out["confidence"] == [0.9]               # its own, not 0.07
    assert "p = 0.05" in seen["msgs"][-1]["content"]
    assert out["decision_review"]["answer_confidence"]["p"] == 0.07


def test_meta_critic_node_gets_the_advice(monkeypatch):
    monkeypatch.setattr(dc, "evaluate", Fake({
        "enough": {"type": "noul", "noul": 0.12}}))
    seen = {}

    def turn(chat_client, model, msgs, **kw):
        seen["msgs"] = msgs
        return "DECISION: ready\nCONFIDENCE: 0.7", 1, 1
    monkeypatch.setattr(council, "_role_turn", turn)
    st = _state(seams=("confidence",), research=["found it"], plan="1. look")
    out = council.meta_critic_node(st, chat_client=None, model="m")
    assert out["confidence"] == [0.7]
    assert "p = 0.12" in seen["msgs"][-1]["content"]
