"""Citing a run is not the run showing it (council run 4, mail #1636):
an auditor sees each ranked claim beside what its cited runs printed,
and the code moves what it finds not shown to Unverified."""
from __future__ import annotations

from types import SimpleNamespace

from consultants.engine import council
from consultants.engine import evidence as E

ROWS = [
    {"id": "C1", "n": 1, "command": "bun x vitest run a.test.ts", "exit": "exit 1",
     "ran": True, "printed": "AssertionError: expected 3 to be 2", "tail": "Tests 1 failed (1)"},
    {"id": "C2", "n": 2, "command": "bun x vitest run b.test.ts", "exit": "exit 0",
     "ran": True, "printed": "", "tail": "Tests 4 passed (4)"},
    {"id": "C3", "n": 3, "command": "bun x vitest run c.test.ts", "exit": "exit 1",
     "ran": False, "printed": "", "tail": "Cannot find module"},
]
ANSWER = (
    "## Ranked defects\n\n"
    "1. **Off-by-one in count** — the counter returns 3 for two items (C1).\n"
    "2. **Race in flush** — concurrent flushes drop a write (C2).\n"
    "3. **Leak in close** — a handle stays open (C1).\n\n"
    "## Unverified\n\n(none)\n")


def test_the_prompt_puts_each_claim_beside_its_runs():
    items = E.claim_audit_items(ANSWER, ROWS)
    assert [it["runs"][0]["id"] for it in items] == ["C1", "C2", "C1"]
    p = E.claim_audit_prompt(items)
    a2 = p.split("A2. CLAIM:")[1].split("---")[0]
    assert "Race in flush" in a2 and "Tests 4 passed" in a2
    assert "AssertionError" not in a2


def test_parse_reads_every_shape_and_ignores_strays():
    reply = ("A1: SHOWN — AssertionError: expected 3 to be 2\n"
             "**A2**: NOT SHOWN - only shows 4 passing tests\n"
             "A3. contradicted: the handle closes\nA9: SHOWN — out of range")
    v = E.parse_claim_audit(reply, 3)
    assert v == {1: ("shown", "AssertionError: expected 3 to be 2"),
                 2: ("not shown", "only shows 4 passing tests"),
                 3: ("contradicted", "the handle closes")}


def test_not_shown_and_contradicted_move_and_shown_stays():
    items = E.claim_audit_items(ANSWER, ROWS)
    out, moved = E.apply_claim_audit(ANSWER, items, {
        1: ("shown", "x"), 2: ("not shown", "only passing tests")})
    assert moved == 1
    ranked, unverified = out.split("## Unverified")
    assert "Off-by-one" in ranked and "Leak in close" in ranked   # A3: no verdict
    assert "Race in flush" not in ranked
    assert "Race in flush" in unverified and "not shown by its cited run(s) C2" in unverified
    assert "(none)" not in unverified


def test_the_synthesizer_revision_runs_the_audit():
    sent = []

    def chat(payload):
        sent.append(payload)
        return {"choices": [{"message": {"role": "assistant",
                                         "content": "A1: SHOWN — x\nA2: NOT SHOWN — y\nA3: SHOWN — z"},
                             "finish_reason": "stop"}]}
    client = SimpleNamespace(chat=chat)
    s = council.initial_state(question="q", cwd="/p", models={"synthesizer": "m"},
                              topology="council", effort="high")
    text, _, _ = council._unverified_revision(
        client, "m", [], ANSWER, state=s, think=False, recorder=None,
        tool_specs=None, tool_executor=None, cwd="/p", rows=ROWS)
    assert len(sent) == 1 and sent[0]["messages"][0]["content"].startswith(E.CLAIM_AUDIT_SYSTEM)
    assert "Race in flush" in text.split("## Unverified")[1]


def test_a_failed_audit_keeps_the_answer():
    def chat(payload):
        raise RuntimeError("down")
    s = council.initial_state(question="q", cwd="/p", models={"synthesizer": "m"},
                              topology="council", effort="high")
    text, _, _ = council._claim_audit(SimpleNamespace(chat=chat), "m", ANSWER, ROWS,
                                      state=s, recorder=None)
    assert text == ANSWER


# Council run 5's matrix, verbatim where it matters (mail #1641).
MATRIX = (
    "### Ranked Correctness Defects\n\n"
    "1. **CRLF prescription** — scan says 390, editor refuses (C1).\n\n"
    "### Edge-Case Matrix\n\n"
    "| Case | Status | Detail | Cite |\n|---|---|---|---|\n"
    "| **CRLF** | ⚠ | **High Risk**. Column mismatch. | (C1) |\n"
    "| **Multibyte** | ✓ | Consistent UTF-16 code units. | `output-limits.ts:5` |\n"
    "| **Empty Files** | ⚠ | refuses start_column 1. | Report 1:B8 |\n"
    "| **Lone CR** | ⚠ | Reader and Editor disagree. | (C3) |\n\n"
    "## Unverified\n\n(none)\n")


class TestTheMatrix:

    def test_rows_without_a_run_are_not_tested(self):
        out, n = E.mark_untested_cells(MATRIX, ROWS)
        assert n == 3                                  # C3 did not reach the code
        assert "| **Multibyte** | not tested (read, not run) |" in out
        assert "| **Empty Files** | not tested (no run cited) |" in out
        assert "| **Lone CR** | not tested (no run cited) |" in out
        assert "| **CRLF** | ⚠ |" in out
        assert "| Case | Status |" in out              # the header is no case
        assert E.mark_untested_cells(MATRIX, None) == (MATRIX, 0)   # no shell

    def test_a_row_citing_a_run_is_audited_and_marked(self):
        items = E.claim_audit_items(MATRIX, ROWS)
        assert [(it.get("kind"), it["runs"][0]["id"]) for it in items] == [
            (None, "C1"), ("cell", "C1")]
        out, n = E.apply_claim_audit(MATRIX, items, {
            1: ("shown", "x"), 2: ("not shown", "about counts")})
        assert n == 1
        assert "| **CRLF** | not tested (audit: not shown by C1) |" in out
        assert "1. **CRLF prescription**" in out       # the ranked item stays

    def test_the_synthesizer_is_told_the_rule(self):
        assert "not tested" in E.EVIDENCE_RULES and "✓" in E.EVIDENCE_RULES


def test_untested_rows_are_marked_even_when_the_audit_call_fails():
    def chat(payload):
        raise RuntimeError("down")
    s = council.initial_state(question="q", cwd="/p", models={"synthesizer": "m"},
                              topology="council", effort="high")
    text, _, _ = council._claim_audit(SimpleNamespace(chat=chat), "m", MATRIX, ROWS,
                                      state=s, recorder=None)
    assert "| **Multibyte** | not tested (read, not run) |" in text
    assert "| **CRLF** | ⚠ |" in text
