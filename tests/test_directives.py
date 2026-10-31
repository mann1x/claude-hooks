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

    def __init__(self, answers, verdict=None, audit_raises=False,
                 audit_cut=False):
        self.answers, self.verdict = list(answers), verdict
        self.audit_raises, self.audit_cut = audit_raises, audit_cut
        self.audit_payloads = []
        self.synth = []
        self.audits = []

    def chat(self, payload, *, think=True, **_kw):
        system = str(payload["messages"][0].get("content"))
        if directives.AUDIT_SYSTEM in system:
            body = payload["messages"][-1]["content"]
            self.audits.append(body)
            self.audit_payloads.append(payload)
            if self.audit_raises:
                raise OSError("audit down")
            answer = body.split("FINAL ANSWER:", 1)[1]
            reply = self.verdict or (
                '1 VIOLATED: attributes it -- "instrument artefact"'
                if "artefact" in answer else "1 MET")
            if self.audit_cut:
                return {"choices": [{"message": {"content": reply},
                                     "finish_reason": "length"}],
                        "usage": {"prompt_tokens": 1,
                                  "completion_tokens": 1}}
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


def test_a_violation_numbered_past_the_directives_is_never_dropped():
    a = directives.parse_audit('1 MET\n8 VIOLATED: rule -- "q"', 1)
    assert a.stray == ['rule -- "q"'] and a.broken and not a.ok


def test_a_met_numbered_past_the_directives_means_nothing():
    a = directives.parse_audit("1 MET\n5 MET", 1)
    assert a.ok and a.stray == []


def test_a_stray_violation_is_re_asked_then_stated():
    c = Client([BAD, BAD], verdict='1 MET\n8 VIOLATED: rule -- "artefact"')
    out = _run(c)
    assert len(c.synth) == 2 and "rule -- " in str(c.synth[1][-1]["content"])
    assert directives.HEADING in out and "rule -- " in out


LIST = ("Binding directives. x.py:1 CONFIRMED.\n\n"
        "1. Give one recommendation.\n"
        "   1. Nested detail.\n"
        "2) Do not rule on looping.\n\n"
        "3. Say what is unmeasured.\nSecond line of it.")


def test_a_numbered_doc_is_one_directive_per_item():
    items = directives.items_of([Doc("any", LIST), Doc("any", RULE)])
    assert [d.text.split("\n")[0] for d in items] == [
        "Binding directives. x.py:1 CONFIRMED.", "Give one recommendation.",
        "Do not rule on looping.", "Say what is unmeasured.", RULE]
    assert "Nested detail" in items[1].text
    assert "Second line" in items[3].text


def test_a_doc_without_a_list_is_one_directive():
    for text in (RULE, "Fix 1. first of all", "1. only one item"):
        assert [d.text for d in directives.items_of([Doc("any", text)])] == [
            text.strip()]


def test_the_failed_run_flags_its_item():
    # csl-2026-10-31-1351-147b: one doc of 8, item 8 broken, "1 MET" first.
    doc = "\n".join(f"{i}. Rule {i}." for i in range(1, 9))
    verdicts = "\n".join(f"{i} MET" for i in range(1, 8))
    c = Client([GOOD, GOOD], verdict=verdicts + '\n8 VIOLATED: rule 8 -- "q"')
    state = _state(Doc("synthesizer", doc))
    out = synthesizer_node(state, chat_client=c, model="m")["final_answer"]
    assert "DIRECTIVE 8:\nRule 8." in c.audits[0]
    assert len(c.synth) == 2
    assert directives.HEADING in out and "Rule 8." in out


def test_only_the_last_block_of_verdicts_decides():
    reply = ("Draft:\n1 VIOLATED: maybe\n2 MET\n"
             "On reflection that was wrong.\n1 MET\n\n2 MET")
    assert directives.parse_audit(reply, 2).ok


def test_commentary_between_verdicts_leaves_the_rest_unchecked():
    a = directives.parse_audit("1 MET\nbecause\n2 MET", 2)
    assert a.unchecked == [1] and not a.ok


def test_a_cut_audit_is_incomplete_even_with_every_verdict_drafted():
    a = directives.parse_audit("1 MET", 1, cut=True)
    assert a.cut and a.unchecked == [1] and not a.ok


def test_a_cut_audit_is_stated_and_never_continued():
    c = Client([GOOD], audit_cut=True)
    out = _run(c)
    assert directives.HEADING in out and "Audit incomplete" in out
    assert len(c.synth) == 1 and len(c.audits) == 2  # one per pass


def test_the_audit_thinks_under_its_own_cap(monkeypatch):
    monkeypatch.setattr(directives, "AUDIT_OUTPUT_TOKENS", 2000)
    c = Client([GOOD])
    _run(c)
    p = c.audit_payloads[0]
    assert p["think"] is True
    assert p["options"]["num_predict"] <= 2000


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
