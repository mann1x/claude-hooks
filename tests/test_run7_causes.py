"""Council run 7's eight causes (csl-2026-10-18-1747-c73d, mail #1659),
on the run's own shapes."""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from claude_hooks import test_reruns as T
from claude_hooks.findings import Ledger
from consultants.engine import coder, council, memory
from consultants.engine import evidence as E
from tests.test_evidence_audit import _cmd, _db, _out

PASS = " ✓ a.test.ts (3 tests)\n      Tests  3 passed (3)\n"
FAIL = " × a.test.ts\n      Tests  2 failed | 1 passed (3)\n"


def _many(tmp_path, n=90):
    return _db(tmp_path, [_cmd("researcher", i % 8, f"bun x vitest run p{i}.test.ts",
                               _out(f"probe {i} printed {'x' * 900}\n" + PASS))
                          for i in range(n)])


class TestCause1TheSynthesizerSeesTheWholeRecord:

    def test_a_window_sized_list_holds_every_run(self, tmp_path):
        db = _many(tmp_path)
        small = E.commands_block(db)
        assert "older runs not shown here" in small and "C1 " not in small
        whole = E.commands_block(db, max_chars=10**7)
        assert "older runs not shown" not in whole
        assert all(f"- C{n} [" in whole for n in range(1, 91))

    def test_the_budget_comes_from_the_window(self, monkeypatch):
        monkeypatch.setattr(council.budget, "resolve_window",
                            lambda *a, **k: SimpleNamespace(tokens=200_000))
        got = council._commands_budget_chars(None, "m", {}, "synthesizer")
        assert got == int(200_000 * council.COMMANDS_WINDOW_SHARE
                          * council.budget.CHARS_PER_TOKEN)
        monkeypatch.setattr(council.budget, "resolve_window",
                            lambda *a, **k: SimpleNamespace(tokens=None))
        assert council._commands_budget_chars(None, "m", {}, "critic") >= 24_000

    def test_the_synthesizer_is_given_it(self, tmp_path, monkeypatch):
        db = _many(tmp_path)
        monkeypatch.setattr(council, "_commands_budget_chars",
                            lambda *a, **k: 10**7)
        from tests.test_evidence_audit import _Seq
        c = _Seq("## Defects\n1. x (C1)\n\n## Unverified\n(none)", "")
        rec = SimpleNamespace(db_path=db, record_node=lambda **k: None,
                              record_llm_call=lambda **k: None,
                              _meta=SimpleNamespace(sid=""))
        council.synthesizer_node(
            {"question": "q", "plan": "p", "research": ["R"], "critique": "ok",
             "cwd": ""}, chat_client=c, model="m", recorder=rec, shell=True)
        sent = c.calls[0]["messages"][-1]["content"]
        assert "- C1 [" in sent and "older runs not shown" not in sent


class TestCause2AMatrixCellSaysWhatItClaims:
    TABLE = ("## (b) Edge-Case Matrix\n\n"
             "| Case | `read_files` | `grep/sed/awk` | Evidence |\n"
             "| :--- | :---: | :---: | :--- |\n"
             "| **CRLF** | ✓ | ✗ | (C1) `alpha$` gives No match |\n")

    def _items(self, text):
        rows = [{"id": "C1", "n": 1, "command": "node p.js", "exit": "exit 0",
                 "ran": True, "role": "researcher", "lane": 0,
                 "printed": "No match for the pattern `alpha$`", "tail": ""}]
        return [i for i in E.claim_audit_items(text, rows)
                if i.get("kind") == "cell"]

    def test_the_claim_is_tool_on_case_with_what_the_mark_means(self):
        a, b = self._items(self.TABLE + "\n## Unverified\n(none)\n")
        assert E._claim_text(b).startswith(
            "grep/sed/awk on CRLF: mishandles this case: a defect")
        assert "its note for this case: (C1) `alpha$` gives No match" in E._claim_text(b)
        assert E._claim_text(a).startswith(
            "read_files on CRLF: handles this case correctly")
        assert "never about a table in the output" in E.CLAIM_AUDIT_SYSTEM

    def test_the_answers_own_legend_wins(self):
        text = (self.TABLE + "\nLegend: ✓ = refuses the edit; ✗ = accepts "
                "a stale edit\n\n## Unverified\n(none)\n")
        a, b = self._items(text)
        assert E._claim_text(b).startswith("grep/sed/awk on CRLF: accepts a stale edit")
        assert E._claim_text(a).startswith("read_files on CRLF: refuses the edit")
        assert "Give the matrix a legend line" in E.EVIDENCE_RULES


class TestCause3And7DoubledHeadings:
    TEXT = ("## Answer\n\n### ## Unverified\n\n- a: b\n\n### ## Dropped\n\n"
            "*   **C21, C35**: Superseded by `audit-repro.test.ts` and standard suite runs.\n"
            "*   **C25, C29, C30, C31, C34**: Exploratory CRLF probes superseded by (C211).\n"
            "*   **C36, C39**: Windows path probes superseded by static analysis.\n"
            "*   **C43, C44, C47**: `apply-patch` ADD probes superseded by (C216).\n"
            "*   **C56**: CRLF grep probe superseded by (C211).\n")

    def test_the_unverified_section_is_seen(self):
        assert not E.needs_unverified_revision(self.TEXT)
        assert E.with_unverified_stub(self.TEXT) == self.TEXT
        assert [h for h, _, _ in E._sections(self.TEXT.split("\n"))] == [
            "Answer", "Unverified", "Dropped"]

    def test_thin_dropped_reads_it_and_names_what_supersedes(self):
        got = {e["ids"] for e in E.thin_dropped(self.TEXT)}
        assert got == {"C21, C35", "C25, C29, C30, C31, C34", "C36, C39", "C56"}
        assert "C43, C44, C47" not in got       # three runs under one run: a reason


class TestCause4ARunCountsForTheFileItRuns:
    C161 = ("cd x && sed 's/it\\.fails(/it(/' audit-repro.test.ts > /scratch/v2.test.ts"
            " && cp /scratch/v2.test.ts src/zz-v2.test.ts && timeout 200 bun x "
            "vitest run src/zz-v2.test.ts 2>&1 | grep -E 'Tests ' ; rm -f src/zz-v2.test.ts")

    def test_a_file_read_by_sed_is_not_the_file_the_runner_ran(self):
        ev = [("write", "src/audit-repro.test.ts", ""),
              ("run", "bun x vitest run src/audit-repro.test.ts | tail -15",
               "exit=0 wall=1 killed_by=none\n Tests  5 passed | 6 expected fail (11)"),
              ("run", self.C161,
               "exit=0 wall=1 killed_by=none\n Tests  6 failed | 5 passed (11)")]
        assert "src/audit-repro.test.ts" not in T.classify(ev)
        assert T._test_segments(self.C161) == [
            " timeout 200 bun x vitest run src/zz-v2.test.ts 2>&1 "]

    def test_a_run_of_the_file_still_counts(self):
        ev = [("write", "src/a.test.ts", ""),
              ("run", "cd core && bun x vitest run src/a.test.ts 2>&1 | tail -5",
               "exit=1 wall=1 killed_by=none\n Tests  1 failed (1)")]
        assert T.classify(ev) == {"src/a.test.ts": "its last run failed (exit 1)"}


ANSWER7 = (
    "### (a) Ranked Correctness Defects\n\n"
    "| Rank | ID | Path:Line | Trigger | Defect | Fix | Evidence |\n"
    "| :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n"
    "| 5 | **csf-7** | `apply-patch.ts:383` | `apply_patch` (ADD) → `editor` edit. | "
    "Adding a file via patch writes the file but fails to call `noteRead`, so the "
    "first edit is refused as \"never read\". | Call noteRead. | (C1) |\n"
    "| 7 | **new** | `file-read.ts:295` | 2,500-char line → read → edit. | "
    "`read_files` truncates output at 2000 but records a full-line receipt. | "
    "Limit the receipt. | (C1) |\n\n## Unverified\n\n(none)\n")
ROWS7 = [{"id": "C1", "n": 1, "command": "bun x vitest run a.test.ts",
          "exit": "exit 1", "ran": True, "role": "researcher", "lane": 100,
          "printed": "AssertionError", "tail": ""}]


class TestCause5TheLedgerReadsTheAnswersTags:

    def _ledger(self, tmp_path):
        (tmp_path / "cline-sync").mkdir()
        root = tmp_path / "cline-sync"
        lg = Ledger(root)
        f, _ = lg.record("apply_patch create, then the editor refuses the file",
                         "Adding a file via patch writes the file but never "
                         "calls noteRead, so the first edit is refused as "
                         "never read.",
                         paths=["sdk/x/executors/apply-patch.ts:383"])
        return root, lg, f

    def test_a_tagged_item_is_evidence_on_that_finding(self, tmp_path):
        root, lg, f = self._ledger(tmp_path)
        far, _ = lg.record("unrelated", "grep launders stamps on outside "
                           "writes", paths=["sdk/x/executors/grep.ts:260"])
        text = ANSWER7.replace("**csf-7**", f"**{far.id}**")
        got = memory.record_answer(str(root), "csl-7", text, ROWS7)
        assert got[0] == (far.id, False)                # the tag decides
        assert Ledger(root).read(f.id).councils == []
        f = far
        g = Ledger(root).read(f.id)
        assert g.councils == ["csl-7"]

    def test_a_bare_name_of_a_path_it_has_is_not_added(self, tmp_path):
        root, lg, f = self._ledger(tmp_path)
        text = ANSWER7.replace("**csf-7**", f"**{f.id}**")
        memory.record_answer(str(root), "csl-7", text, ROWS7)
        assert Ledger(root).read(f.id).paths == ["sdk/x/executors/apply-patch.ts:383"]

    def test_a_new_items_title_is_its_defect_text(self, tmp_path):
        root, lg, f = self._ledger(tmp_path)
        got = memory.record_answer(str(root), "csl-7", ANSWER7, ROWS7)
        new = Ledger(root).read(got[1][0])
        assert got[1][1] is True
        assert new.title.startswith("read_files truncates output at 2000")
        assert "new" not in new.title.split()[:1]

    def test_an_unknown_tag_falls_back_to_matching(self, tmp_path):
        root, lg, f = self._ledger(tmp_path)
        text = ANSWER7.replace("**csf-7**", "**csf-99**")
        assert memory.record_answer(str(root), "s", text, ROWS7)[0][0] == f.id


class TestCause6TheCoderIsAskedForTestsAndFixes:

    def test_the_gate_and_the_coders_own_view_of_the_question(self):
        assert "each task writes the failing test AND the fix" in \
            coder.PLANNER_CODER_GATE_BLOCK
        assert "not a subset" in coder.PLANNER_CODER_GATE_BLOCK
        task = coder.CoderTaskItem(task="write tests", path="a.test.ts", why="")
        msgs = coder.build_coder_messages(
            task, [], question="Coder lanes: write the failing tests and the fixes.")
        user = msgs[-1]["content"]
        assert "context only" not in user
        assert "applies to you, together with TASK" in user


def _lanes_db(tmp_path, ran_lanes, lanes, critic_before=None):
    db = _db(tmp_path, [_cmd("researcher", ln, "bun x vitest run a.test.ts",
                             _out(PASS)) for ln in ran_lanes])
    c = sqlite3.connect(db)
    if critic_before is not None:
        for ln in critic_before:
            c.execute("INSERT INTO events (kind, role, lane_idx) VALUES "
                      "('node_exit', 'researcher', ?)", (ln,))
        c.execute("INSERT INTO events (kind, role) VALUES ('node_exit', 'critic')")
    for ln in lanes:
        c.execute("INSERT INTO events (kind, role, lane_idx) VALUES "
                  "('node_exit', 'researcher', ?)", (ln,))
    c.commit()
    c.close()
    return db


class TestCause8ReadyNeedsTheRoundsLanesToHaveRun:
    READY = "DECISION: ready\nall good\nCONFIDENCE: 1.0"

    def test_most_lanes_unrun_is_held(self, tmp_path):
        db = _lanes_db(tmp_path, [0, 4, 7], range(8))
        assert E.round_lanes(db) == list(range(8))
        d, text, held = E.hold_to_evidence_bar(
            "ready", self.READY, E.commands_ran(db), E.round_lanes(db))
        assert (d, held) == ("needs_more_research", True)
        assert "5 of the 8 lanes" in text and "lanes 1, 2, 3, 5, 6" in text

    def test_a_few_unrun_keep_ready_but_cap_confidence(self, tmp_path):
        db = _lanes_db(tmp_path, [100, 102], [100, 101, 102, 103],
                       critic_before=range(8))
        assert E.round_lanes(db) == [100, 101, 102, 103]
        d, text, held = E.hold_to_evidence_bar(
            "ready", self.READY, E.commands_ran(db), E.round_lanes(db))
        assert (d, held) == ("ready", False)
        assert "Lanes 101, 103 of this round" in text
        _, conf = council._extract_confidence(text)
        assert conf == E.UNRUN_CONFIDENCE_CAP

    def test_every_lane_run_changes_nothing(self, tmp_path):
        db = _lanes_db(tmp_path, [0, 1], [0, 1])
        assert E.hold_to_evidence_bar("ready", self.READY, E.commands_ran(db),
                                      E.round_lanes(db)) == ("ready", self.READY, False)
