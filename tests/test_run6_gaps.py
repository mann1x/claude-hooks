"""Council run 6's gaps (mails #1647 / #1648), on the run's own rows."""
from __future__ import annotations

from consultants.engine import evidence

HEAD = ("| Case | Editor | Restore | Scan | Apply Patch | Bash | Result | Evidence |\n"
        "| :--- | :---: | :---: | :---: | :---: | :---: | :--- | :--- |\n")
BOM = "| **BOM** | ✓ | ✓ | ✓ | ✓ | ✓ | Correctly handled via `splitBom`. | report 1 |"
CRLF = "| **CRLF** | ✓ | ✓ | ✗ | ✓ | ✓ | Scan columns are offset by 1. | (C387) |"
RANKED_2 = ("| 2 | **Unprotected `apply_patch`** | `apply-patch.ts:381` | "
            "Concurrent `apply_patch` calls or `apply_patch` after outside "
            "write. | Lacks `withFileLock` and `changedSince` checks. "
            "Overwrites files silently, clobbering concurrent writers. | Wrap "
            "`applyChanges` in `withFileLock`. | (C372) |")


def _answer(*matrix: str) -> str:
    return ("## Correctness Defects\n\n"
            "| Rank | Defect | Path:Line | Trigger | Failure | Fix | Evidence |\n"
            "| :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n"
            f"{RANKED_2}\n\n## Edge-Case Matrix\n\n{HEAD}"
            + "\n".join(matrix) + "\n\n## Unverified\n\n(none)\n")


ROWS = [{"id": f"C{n}", "n": n, "command": "bun x vitest run x.test.ts",
         "exit": "exit 1", "ran": True, "role": "researcher", "lane": 100,
         "printed": p, "tail": ""}
        for n, p in [(372, "apply_patch applied after the outside write; "
                           "next stale edit accepted"),
                     (387, "insert_column 390 with CRLF, 389 with LF")]]


class TestEveryCellOfARow:
    """Gap 1: "not tested" reached only the first column."""

    def test_a_row_citing_no_run_loses_every_mark(self):
        text, n = evidence.mark_untested_cells(_answer(BOM), ROWS)
        row = next(ln for ln in text.split("\n") if "**BOM**" in ln)
        assert n == 1 and "✓" not in row
        assert row.count("not tested") == 5
        assert row.count("not tested (no run cited)") == 1
        assert row.strip().startswith("|") and row.strip().endswith("|")
        assert row.count("|") == BOM.count("|")

    def test_each_cell_of_a_cited_row_is_its_own_claim(self):
        items = [i for i in evidence.claim_audit_items(_answer(CRLF), ROWS)
                 if i.get("kind") == "cell"]
        assert [i["header"] for i in items] == [
            "Editor", "Restore", "Scan", "Apply Patch", "Bash"]
        prompt = evidence.claim_audit_prompt(items)
        assert "the column 'Bash' says '✓'" in prompt

    def test_a_cell_not_shown_loses_only_its_own_mark(self):
        text = _answer(CRLF)
        items = evidence.claim_audit_items(text, ROWS)
        k = next(k for k, i in enumerate(items, 1) if i.get("header") == "Bash")
        out, n = evidence.apply_claim_audit(
            text, items, {k: ("not shown", "C387 runs no bash")})
        row = next(ln for ln in out.split("\n") if "**CRLF**" in ln)
        cells = [c.strip() for c in row.strip().strip("|").split("|")]
        assert cells[1:5] == ["✓", "✓", "✗", "✓"]
        assert cells[5] == "not tested (audit: not shown by C387)"
        assert n == 1


class TestEveryClauseOfAClaim:
    """Gap 2: ranked #2 said two things; C372 showed one."""

    def test_the_auditor_is_told_to_judge_every_clause(self):
        assert "PARTLY" in evidence.CLAIM_AUDIT_SYSTEM
        assert "every clause" in evidence.CLAIM_AUDIT_SYSTEM
        got = evidence.parse_claim_audit(
            "A1: PARTLY — clobbering concurrent writers is never run", 1)
        assert got == {1: ("partly", "clobbering concurrent writers is never run")}

    def test_a_partly_shown_row_stays_ranked_and_says_what_is_not_shown(self):
        text = _answer(CRLF)
        items = evidence.claim_audit_items(text, ROWS)
        assert items[0].get("kind") != "cell"
        out, n = evidence.apply_claim_audit(
            text, items, {1: ("partly", "concurrent writers never run")})
        lines = out.split("\n")
        row = next(ln for ln in lines if "Unprotected" in ln)
        assert "not shown: concurrent writers never run" in row
        assert row.rstrip().endswith("|") and row.count("|") == RANKED_2.count("|")
        unv = out[out.index("## Unverified"):]
        assert "the part its runs C372 do not show: concurrent writers never run" in unv
        assert "(none)" not in unv
        # The table is still one table: the row after the header separator
        # is still a row.
        i = lines.index(row)
        assert lines[i - 1].strip().startswith("|")
        assert n == 1

    def test_a_partly_list_item_gets_the_note_under_it(self):
        text = ("## Defects\n\n1. grep launders and clobbers, grep.ts:260 "
                "(C372)\n2. other, x.ts:1 (C387)\n\n## Unverified\n\n(none)\n")
        items = evidence.claim_audit_items(text, ROWS)
        out, _ = evidence.apply_claim_audit(
            text, items, {1: ("partly", "clobbering never run")})
        lines = out.split("\n")
        at = next(i for i, ln in enumerate(lines) if ln.startswith("1. grep"))
        assert "not shown: clobbering never run" in lines[at + 1]
        assert lines[at + 2].startswith("2. other")
        assert [it["text"].split("\n")[0][:8]
                for it in evidence.ranked_items(out)] == ["1. grep ", "2. other"]


DROPPED_RUN6 = """## Dropped
- C19: Superseded by C387
- C37: Not needed for final result
- C55: Superseded by C387
- C59: Superseded by C387
- C62: Superseded by C387
- C66: General test run
- C68: Superseded by C387
- C103: Superseded by report 1
- C120: rerun of C119 after fixing the import
- C121: harness error (killed by timeout)
- C130: printed only `11 passed`; C372 shows the same editor path with the outside write in between
"""


class TestThinDropped:
    """Gap 7: ~25 runs dropped as "General test run" / "Not needed"."""

    def test_stock_and_copied_reasons_are_thin_real_ones_are_not(self):
        got = {e["ids"]: e["reason"] for e in evidence.thin_dropped(
            "## Answer\n\nx\n\n" + DROPPED_RUN6)}
        assert set(got) == {"C19", "C37", "C55", "C59", "C62", "C66", "C68",
                            "C103"}

    def test_dropped_by_omission_is_the_harness_own_and_never_thin(self):
        text = ("## Dropped by omission\n\n- C1: General test run\n"
                "- C2: General test run\n- C3: General test run\n")
        assert evidence.thin_dropped(text) == []

    def test_three_thin_reasons_ask_a_revision_and_then_are_marked(self):
        text = ("## Defects\n\n(none)\n\n## Unverified\n\n(none)\n\n"
                + DROPPED_RUN6)
        a = evidence.audit_answer(text, [])
        assert not a.ok and len(a.thin) == 8
        note = evidence.revision_note(a)
        assert "do not say what the run showed" in note
        assert "  - C37: Not needed for final result" in note
        out = evidence.enforce(text, a)
        marked = [ln for ln in out.split("\n")
                  if evidence.THIN_DROPPED_NOTE in ln]
        assert len(marked) == 8
        assert not any("C120" in ln or "C130" in ln for ln in marked)
        # Marked lines are not counted again on a second audit.
        assert evidence.thin_dropped(out) == []

    def test_two_thin_reasons_pass(self):
        text = ("## Unverified\n\n(none)\n\n## Dropped\n- C1: stock\n"
                "- C2: General test run\n")
        assert evidence.audit_answer(text, []).ok
