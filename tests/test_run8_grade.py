"""Council run 8's grade (mail #1672): the patch audit judged hunks alone,
lane sub-headings hid the held entries, a fix through an option nothing
passes, a mis-cited fix, matrix cells as findings, a by_design finding
ranked again, self-superseding drops, and a patch that widens a finding
it touches."""
from __future__ import annotations

from types import SimpleNamespace

from claude_hooks.findings import Ledger
from consultants.engine import evidence, held_patches as hp, memory

AP = "sdk/packages/core/src/extensions/tools/executors/apply-patch.ts"
TC = "sdk/packages/core/src/runtime/atomic/transaction-controller.ts"
SED = "sdk/packages/core/src/extensions/tools/executors/sed.ts"


def _diff(path, start, n, added, removed=()):
    body = "".join(f"-{x}\n" for x in removed) + "".join(f"+{x}\n" for x in added)
    return (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
            f"@@ -{start},{n} +{start},{n + len(added) - len(removed)} @@\n"
            + body)


def _lane(name, *diffs, post=None):
    d = "".join(diffs)
    return hp.LanePatch(lane=name, diff=d, hunks=hp.parse_diff(name, d),
                        post=post or {})


def _row(n, role="researcher", lane=0, command="bun x vitest run a.test.ts",
         output="Tests 1 passed", ran=True, ts=0.0):
    return {"id": f"C{n}", "n": n, "role": role, "lane": lane, "ts": ts,
            "command": command, "exit": "exit 0", "output": output,
            "printed": output, "tail": output, "ran": ran}


# 1 ------------------------------------------- the patch audit sees the file
SED_LANE = _lane(
    "coder-0",
    _diff(SED, 418, 2, [
        "// A CRLF file's lines end \"\\r\\n\": split on both and join with the "
        "file's own EOL, so a $ anchor matches.",
        "const lines = text.split(/\\r\\n|\\n/);"]),
    _diff(SED, 554, 2, ["return out.join(eol);"], ["return out.join('\\n');"]),
    post={SED: "line 1\nconst lines = text.split(/\\r\\n|\\n/);\nreturn out.join(eol);\n"})


class TestItem1TheAuditSeesTheWholeFile:

    def test_each_comment_comes_with_its_files_whole_patch_and_post_file(self):
        items = hp.comment_items([SED_LANE])
        assert len(items) == 1
        p = hp.comment_audit_prompt(items)
        assert "THE PATCH TO THIS FILE (every hunk)" in p
        assert "return out.join(eol);" in p.split("COMMENTS:")[0]
        assert "THE FILE AS PATCHED" in p and "    3  return out.join(eol);" in p
        assert "file F1" in p
        assert "default value is not an assignment" in hp.COMMENT_AUDIT_SYSTEM

    def test_a_file_is_shown_once_for_all_its_comments(self):
        two = _lane("coder-0",
                    _diff(TC, 148, 1, ["/** Called for every file a rollback "
                                       "puts back, deletes or recreates. */",
                                       "forgetReads?: (p: string) => void;"]),
                    _diff(TC, 692, 1, ["// The rollback rewrites files with a "
                                       "plain write; forget their reads here.",
                                       "this.options.forgetReads?.(path);"]),
                    post={TC: "x\n"})
        items = hp.comment_items([two])
        p = hp.comment_audit_prompt(items)
        assert len(items) == 2 and p.count("THE FILE AS PATCHED") == 1
        assert p.count("F1.") == 1 and p.count("file F1") == 2
        assert hp.comment_audit_batches(items, 10_000) == [[0, 1]]

    def test_the_post_file_is_read_from_the_view(self, tmp_path):
        (tmp_path / "v").mkdir()
        (tmp_path / "v" / "a.ts").write_text("patched\n")
        views = SimpleNamespace(repo="/repo",
                                host_path=lambda lane, p: str(tmp_path / "v" / "a.ts"))
        assert hp._post_files(views, "coder-0", ["a.ts"]) == {"a.ts": "patched\n"}


# 2 ------------------------------------------------- lane sub-headings count
RUN8_HELD = """## Defects

1. grep launders (C3)

### Held Patches

**lane `coder-0`**
*   `fixes #csf-1, #csf-2` (Verified via C3).
*   `does not fix #csf-4`.

**lane `researcher-5`**
*   `does not fix any ranked defects` (Test probe only).

### Dropped
*   C9: Superseded by C3 after the rerun of the same test.
"""


class TestItem2LaneSubHeadings:

    def test_a_bold_lane_heading_under_held_patches_is_that_lanes_entry(self):
        a = hp.audit(RUN8_HELD, [_lane("coder-0", _diff(AP, 400, 2, ["x();"])),
                                 _lane("researcher-5", _diff(AP, 10, 1, ["y();"]))],
                     [], [_row(3, role="coder", lane=0)])
        assert a.missing == [] and a.unbacked == []

    def test_a_ledger_id_fix_needs_that_lanes_green_run(self):
        a = hp.audit(RUN8_HELD, [_lane("coder-0", _diff(AP, 400, 2, ["x();"]))],
                     [], [_row(3, role="researcher", lane=4)])
        assert a.unbacked == [("coder-0", "csf-1")]
        out = hp.enforce(RUN8_HELD, a)
        line = next(ln for ln in out.split("\n") if "fixes #csf-1" in ln)
        assert hp.UNBACKED_FIX.format(n="csf-1") in line

    def test_the_held_section_stops_at_a_heading_naming_no_lane(self):
        lines = RUN8_HELD.split("\n")
        spans = hp._held_spans(lines)
        assert [h for h, _, _ in spans] == ["", "lane `coder-0`", "lane `researcher-5`"]


# 3 -------------------------------------------- an option nothing passes
class TestItem3UnwiredOptions:

    LANE = _lane("coder-0",
                 _diff(TC, 148, 1, ["forgetReads?: (p: string) => void;"]),
                 _diff(TC, 692, 1, ["this.options.forgetReads?.(path);"]))

    def test_an_option_no_changed_production_file_passes_is_unwired(self):
        grep = lambda repo, name: [  # noqa: E731
            "sdk/packages/core/src/runtime/atomic/session-protocol.ts:224:  forgetReads,",
            f"{TC}:150: forgetReads",
            "sdk/packages/core/src/runtime/atomic/x.test.ts:3: forgetReads",
            ".claude-hooks/consultants/s/metadata.json:2: forgetReads"]
        u = hp.unwired_options(self.LANE, "/repo", grep=grep)
        assert u == [{"name": "forgetReads", "where": f"{TC}:148",
                      "mentions": ["sdk/packages/core/src/runtime/atomic/"
                                   "session-protocol.ts:224"]}]

    def test_a_production_file_the_patch_changes_wires_it(self):
        lane = _lane("coder-0", _diff(TC, 148, 1, ["forgetReads?: () => void;"]),
                     post={"sdk/packages/core/src/runtime/atomic/session-protocol.ts":
                           "new TransactionController({ forgetReads })"})
        assert hp.unwired_options(lane, "/repo", grep=lambda r, n: []) == []

    def test_the_answer_owes_the_option_by_name(self):
        lane = _lane("coder-0", _diff(AP, 400, 2, ["x();"]))
        lane.unwired = [{"name": "forgetReads", "where": f"{TC}:148",
                         "mentions": []}]
        block = hp.held_block([lane], [])
        assert "NEW OPTIONS NO PRODUCTION CALLER PASSES" in block
        assert "`forgetReads`" in block
        a = hp.audit(RUN8_HELD, [lane], [], [_row(3, role="coder", lane=0)])
        assert [u["name"] for _, u in a.unwired] == ["forgetReads"]
        assert "forgetReads" in hp.revision_note(a)
        assert "not wired into the product" in hp.enforce(RUN8_HELD, a)


# 4 --------------------------------------------- the cap says what was said
def test_item4_the_cap_note_says_what_the_critic_rated():
    rows = [_row(1, lane=0), _row(2, lane=1, ran=False)]
    d, text, held = evidence.hold_to_evidence_bar(
        "ready", "DECISION: ready\nok\nCONFIDENCE: 0.95", rows, [0, 1, 2])
    assert d == "needs_more_research"            # 2 of 3 unrun: held
    d, text, held = evidence.hold_to_evidence_bar(
        "ready", "DECISION: ready\nok\nCONFIDENCE: 0.95", rows + [_row(3, lane=2)],
        [0, 1, 2])
    assert d == "ready" and "CONFIDENCE: 0.6" in text
    assert "(the critic rated it 0.95)" in text
    _, low, _ = evidence.hold_to_evidence_bar(
        "ready", "DECISION: ready\nCONFIDENCE: 0.5", rows + [_row(3, lane=2)],
        [0, 1, 2])
    assert "rated it" not in low


# 5 -------------------------------------------------- a second look
class TestItem5SecondLook:

    ANSWER = ("## Defects\n\n| # | ID | Defect |\n|---|---|---|\n"
              "| 1 | `csf-2` | apply_patch launders an outside write (C1) |\n\n"
              "## Unverified\n\n(none)\n")
    ROWS = [_row(1, command="grep probe", output="EDIT after outside write: REFUSED"),
            _row(2, role="coder", command="bun x vitest run csf-receipt-hygiene.test.ts",
                 output="✓ csf-2 apply_patch ADD launders (red→green)\nTests 3 passed"),
            _row(3, command="ls", output="csf-2.md", ran=False)]

    def test_other_runs_naming_the_finding_are_offered(self):
        items = evidence.claim_audit_items(self.ANSWER, self.ROWS)
        second = evidence.second_look_items(items, {1: ("not shown", "grep only")},
                                            self.ROWS)
        assert [(k, [r["id"] for r in it["runs"]], it["recite_from"])
                for k, it in second] == [(1, ["C2"], ["C1"])]
        assert evidence.second_look_items(items, {1: ("shown", "")}, self.ROWS) == []

    def test_shown_by_another_run_stays_with_a_note(self):
        items = evidence.claim_audit_items(self.ANSWER, self.ROWS)
        (k, it), = evidence.second_look_items(items, {1: ("not shown", "x")}, self.ROWS)
        items[k - 1] = it
        out, n = evidence.apply_claim_audit(self.ANSWER, items, {1: ("shown", "")})
        assert n == 1 and "harness: shown by C2, not by the cited C1" in out
        assert "moved here" not in out


# 6 ----------------------------------------------- cells are not rows
class TestItem6DefectCellsOnly:

    ANSWER = ("## Edge-case matrix\n\n| Tool | CRLF | Empty | BOM |\n|---|---|---|---|\n"
              "| `grep` | ✗ (C1) | ✓ (C2) | not tested (audit: not shown by C2) |\n\n"
              "## Unverified\n\n(none)\n")
    ROWS = [_row(1, output="H19 grep 'alpha$' -> no match"),
            _row(2, output="H20 read = ''")]

    def test_only_a_defect_cell_is_recorded_titled_by_row_and_column(self, tmp_path):
        out = memory.record_answer(str(tmp_path), "s1", self.ANSWER, self.ROWS)
        assert len(out) == 1
        f = Ledger(tmp_path).read(out[0][0])
        assert f.title == "grep — CRLF: mishandles this case: a defect"
        assert "not tested" not in f.title

    def test_one_run_is_one_evidence_line_and_one_log_per_council(self, tmp_path):
        lg = Ledger(tmp_path / "f")
        f, _ = lg.record("t one two three", "claim words here", evidence="C1 x",
                         council="s1")
        lg.record("t one two three", "claim words here", evidence="C1 x",
                  council="s1", known=f.id)
        lg.record("t one two three", "claim words here", evidence="C1 x",
                  council="s1", known=f.id)
        f = lg.read(f.id)
        assert len(f.evidence_lines) == 1
        assert f.section("Log").count("seen again in s1") == 1


# 7 ------------------------------------------------ settled, then listed
class TestItem7SettledFindingsRestated:

    ANSWER = ("## Ranked defects\n\n1. grep launders (C1)\n\n"
              "### Usefulness Improvements\n"
              "1.  **Narrow Grep Credit:** record noteRead only for the spans shown.\n"
              "2.  **BOM Card Sync:** use splitBom in the card.\n\n"
              "### Unverified\n- csf-12 restated here is skipped\n")

    def test_items_outside_unverified_and_dropped(self):
        items = memory.answer_items(self.ANSWER)
        assert [i["text"][:16] for i in items] == [
            "1. grep launders", "1.  **Narrow Gre", "2.  **BOM Card S"]

    def test_a_restating_item_gets_the_recorded_reason(self):
        f = SimpleNamespace(id="csf-12", status="by_design",
                            title="grep credits a full-file read",
                            decision="The full-file read credit is deliberate.")
        items = memory.answer_items(self.ANSWER)
        p = memory.settled_audit_prompt([f], items)
        assert "S1. csf-12 [by_design]" in p and "I2. 1.  **Narrow" in p
        hits = memory.parse_settled_audit("I2: S1 — narrows the credit\nI9: S1",
                                          len(items), 1)
        assert hits == {2: 1}
        out, n = memory.apply_settled_audit(self.ANSWER, [f], items, hits)
        line = next(ln for ln in out.split("\n") if "Narrow Grep" in ln)
        assert n == 1 and "restates csf-12, settled by_design" in line
        assert "deliberate." in line
        again, n2 = memory.apply_settled_audit(out, [f], items, hits)
        assert n2 == 0 and again == out

    def test_the_synthesizer_is_told_not_to_list_it(self):
        assert "improvement" in memory._DO["synthesizer"]["by_design"]
        assert "improvement" in memory._DO["synthesizer"]["refuted"]


# 8 ------------------------------------------------ circular drops
def test_item8_a_run_superseded_by_itself_or_a_dropped_run_is_thin():
    t = ("## Dropped\n- C49: Superseded by C49.\n- C64, C65: Superseded by C64.\n"
         "- C47: Superseded by C78.\n")
    assert [e["ids"] for e in evidence.thin_dropped(t)] == ["C49", "C64, C65"]


# 9 ------------------------------------- what a patch does to what it touches
class TestItem9TouchedFindings:

    def test_a_finding_within_the_window_of_a_hunk_is_touched(self):
        lane = _lane("coder-0", _diff(AP, 419, 9, ["move(dest);"]))
        found = [SimpleNamespace(id="csf-8", status="confirmed",
                                 title="move keeps the old spans",
                                 paths=[f"{AP}:413"]),
                 SimpleNamespace(id="csf-13", status="refuted", title="x",
                                 paths=[f"{AP}:420"]),
                 SimpleNamespace(id="csf-4", status="confirmed", title="far",
                                 paths=[f"{AP}:900"]),
                 SimpleNamespace(id="csf-9", status="confirmed", title="bare",
                                 paths=["apply-patch.ts:425"]),
                 SimpleNamespace(id="csf-5", status="candidate", title="below",
                                 paths=[f"{AP}:460"])]
        assert [t["id"] for t in hp.touched_findings(lane, found)] == [
            "csf-8", "csf-9", "csf-5"]

    def test_the_answer_must_say_what_the_patch_does_to_it(self):
        lane = _lane("coder-0", _diff(AP, 400, 2, ["x();"]))
        lane.touches = [{"id": "csf-8", "status": "confirmed",
                         "title": "move keeps the old spans", "where": f"{AP}:413"}]
        assert "KNOWN FINDINGS THIS PATCH TOUCHES" in hp.held_block([lane], [])
        a = hp.audit(RUN8_HELD, [lane], [], [_row(3, role="coder", lane=0)])
        assert [t["id"] for _, t in a.touched] == ["csf-8"]
        assert "touches csf-8" in hp.revision_note(a)
        assert "changes the code of csf-8" in hp.enforce(RUN8_HELD, a)
        said = RUN8_HELD.replace("`does not fix #csf-4`.", "`widens csf-8`.")
        assert hp.audit(said, [lane], [], [_row(3, role="coder", lane=0)]).touched == []

    def test_a_finding_the_brief_named_that_no_later_run_names_is_owed(self):
        docs = [SimpleNamespace(text="Re-verify csf-8 and csf-1 on the patched tree.",
                                ts=100.0)]
        rows = [_row(1, output="csf-8 before the brief", ts=50.0),
                _row(2, output="csf-1 MOVE red", ts=150.0),
                _row(3, output="csf-8 listed", ran=False, ts=160.0)]
        out = evidence.brief_owed("## Answer\n\n## Unverified\n\n(none)\n", docs, rows)
        assert "csf-8: the checkpoint brief named it" in out
        assert "csf-1: the checkpoint brief" not in out
        assert evidence.brief_owed(out, docs, rows) == out
        assert evidence.brief_owed("x", docs, None) == "x"


# ---------------------------------------------------- the council's calls
def _state():
    from consultants.engine import council
    return council.initial_state(question="audit the tools", cwd="/p",
                                 models={"synthesizer": "m"},
                                 topology="council", effort="high")


def test_item5_the_council_asks_a_second_time_and_keeps_the_item():
    from consultants.engine import council
    sent = []

    def chat(payload):
        sent.append(payload["messages"][-1]["content"])
        reply = "A1: NOT SHOWN — grep only" if len(sent) == 1 else "A1: SHOWN — red→green"
        return {"choices": [{"message": {"role": "assistant", "content": reply},
                             "finish_reason": "stop"}]}
    t = TestItem5SecondLook
    text, _, _ = council._claim_audit(SimpleNamespace(chat=chat), "m", t.ANSWER,
                                      t.ROWS, state=_state(), recorder=None)
    assert len(sent) == 2 and "[C2]" in sent[1] and "[C1]" not in sent[1]
    assert "shown by C2, not by the cited C1" in text
    assert "csf-2" in text.split("## Unverified")[0]


def test_item7_the_council_notes_a_settled_finding_listed_again(tmp_path, monkeypatch):
    from consultants.engine import council
    f = SimpleNamespace(id="csf-12", status="by_design", title="grep full-file credit",
                        decision="Deliberate.")
    monkeypatch.setattr(memory, "settled_findings", lambda cwd, q: [f])
    sent = []

    def chat(payload):
        sent.append(payload["messages"])
        return {"choices": [{"message": {"role": "assistant", "content": "I2: S1 — narrows it"},
                             "finish_reason": "stop"}]}
    text, _, _ = council._settled_audit(
        SimpleNamespace(chat=chat), "m", TestItem7SettledFindingsRestated.ANSWER,
        state=_state(), cwd=str(tmp_path), recorder=None)
    assert sent[0][0]["content"].startswith(memory.SETTLED_AUDIT_SYSTEM)
    assert "restates csf-12" in next(ln for ln in text.split("\n") if "Narrow" in ln)
    monkeypatch.setattr(memory, "settled_findings", lambda cwd, q: [])
    sent.clear()
    same, _, _ = council._settled_audit(
        SimpleNamespace(chat=chat), "m", "x", state=_state(), cwd=str(tmp_path),
        recorder=None)
    assert same == "x" and sent == []


def test_item7_the_synthesizer_runs_the_settled_audit_and_the_brief_check():
    import inspect
    from consultants.engine import council
    src = inspect.getsource(council.synthesizer_node)
    assert src.index("_settled_audit(") < src.index("record_answer(")
    assert "text = evidence.brief_owed(" in src
