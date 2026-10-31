"""An inject binds the final answer (consultants/engine/directives.py)."""
from __future__ import annotations

from consultants.engine import directives
from consultants.engine.council import (_additional_context_block,
                                         synthesizer_node)
from consultants.engine.state_v2 import Doc

RULE = "Do not attribute the rise to rounding before the control result."
# An inject turns the evidence bar on, which owes an Unverified section.
BAD = "The rise is an instrument artefact of rounding.\n\n## Unverified\n(none)"
GOOD = "The rise is unexplained until the control result.\n\n## Unverified\n(none)"


class Client:
    """Answers the synthesizer from ``answers`` and the auditor by whether
    the audited answer breaks the directive (``verdict`` overrides)."""

    def __init__(self, answers, verdict=None, audit_raises=False):
        self.answers, self.verdict = list(answers), verdict
        self.audit_raises = audit_raises
        self.synth = []
        self.audits = []

    def chat(self, payload, *, think=True, **_kw):
        system = str(payload["messages"][0].get("content"))
        if directives.AUDIT_SYSTEM in system:
            body = payload["messages"][-1]["content"]
            self.audits.append(body)
            if self.audit_raises:
                raise OSError("audit down")
            answer = body.split("FINAL ANSWER:", 1)[1]
            reply = self.verdict or (
                '1 VIOLATED: attributes it -- "instrument artefact"'
                if "artefact" in answer else "1 MET")
        else:
            self.synth.append(payload["messages"])
            reply = self.answers.pop(0)
        return {"choices": [{"message": {"content": reply},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


def _state(*docs):
    return {"question": "why", "plan": "p", "research": ["r"], "critique": "c",
            "additional_context": list(docs)}


def _run(client, **kw):
    state = _state(Doc("synthesizer", RULE))
    return synthesizer_node(state, chat_client=client, model="m", **kw
                            )["final_answer"]


def test_the_prompt_says_a_directive_binds():
    assert directives.BINDING_NOTE in _additional_context_block(
        [Doc("any", RULE)])


def test_engine_context_alone_is_not_a_directive():
    assert directives.BINDING_NOTE not in _additional_context_block(
        [Doc("any", "tried x", source="already_tried")])
    assert directives.directives_of(
        [Doc("any", "tried x", source="already_tried"), Doc("any", "  ")]) == []


def test_an_answer_that_follows_is_not_asked_again():
    c = Client([GOOD])
    out = _run(c)
    assert GOOD in out and directives.HEADING not in out
    assert len(c.synth) == 1


def test_a_violation_is_re_asked_with_the_directive_and_the_fix_is_kept():
    c = Client([BAD, GOOD])
    out = _run(c)
    assert GOOD in out and "artefact" not in out
    assert directives.HEADING not in out
    assert len(c.synth) == 2
    note = str(c.synth[1][-1]["content"])
    assert RULE in note and "instrument artefact" in note


def test_a_violation_that_survives_is_stated_never_hidden():
    c = Client([BAD, BAD])
    out = _run(c)
    assert BAD in out
    assert directives.HEADING in out and RULE in out
    assert len(c.synth) == 2


def test_a_failed_audit_is_unchecked_not_passed():
    out = _run(Client([GOOD], audit_raises=True))
    assert directives.HEADING in out and "Not checked" in out


def test_a_directive_with_no_verdict_is_unchecked():
    assert directives.parse_audit("1 MET", 2).unchecked == [2]
    assert directives.parse_audit("nonsense", 1).unchecked == [1]


def test_read_both_ways_counts_as_broken():
    a = directives.parse_audit("1 MET\n1 VIOLATED: x", 1)
    assert [i for i, _ in a.violated] == [1]


def test_a_verdict_past_the_directives_is_ignored():
    assert directives.parse_audit("1 MET\n5 VIOLATED: x", 1).violated == []


def test_no_directive_means_no_audit():
    c = Client([GOOD])
    synthesizer_node(_state(), chat_client=c, model="m")
    assert c.audits == [] and len(c.synth) == 1


def test_the_answer_audited_last_is_the_one_delivered():
    c = Client([BAD, GOOD])
    _run(c)
    assert GOOD in c.audits[-1].split("FINAL ANSWER:", 1)[1]


def test_a_warm_follow_up_gets_the_inject_and_is_held_to_it():
    c = Client([BAD, GOOD])
    state = _state(Doc("synthesizer", RULE))
    prior = [{"role": "system", "content": "s"},
             {"role": "user", "content": "q"},
             {"role": "assistant", "content": "a"}]
    out = synthesizer_node(state, chat_client=c, model="m",
                           prior_messages=prior)["final_answer"]
    assert RULE in str(c.synth[0][-1]["content"])
    assert GOOD in out
