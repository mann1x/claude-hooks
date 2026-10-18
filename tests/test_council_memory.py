"""The council reads and writes the findings ledger
(docs/PLAN-council-memory.md, M2)."""
from __future__ import annotations

from types import SimpleNamespace

from claude_hooks.findings import Ledger
from consultants.engine import memory
from tests.test_evidence_audit import _Seq, _run3_like

PATH = "src/extensions/tools/executors/apply-patch.ts:412"
ROWS = [{"id": "C1", "n": 1, "command": "bun x vitest run a.test.ts",
         "exit": "exit 1", "ran": True, "role": "researcher", "lane": 0,
         "printed": "AssertionError: edit refused as unread", "tail": ""}]
ANSWER = ("## Ranked defects\n\n"
          f"1. **Create then edit refused** — the editor refuses a file "
          f"apply_patch created, `{PATH}` (C1).\n\n## Unverified\n\n(none)\n")


def _msgs():
    return [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}]


class TestReading:

    def test_no_ledger_changes_nothing(self, tmp_path):
        state = {"cwd": str(tmp_path), "question": "audit the tools"}
        for role in ("planner", "critic", "meta_critic", "synthesizer"):
            assert memory.with_known_findings(_msgs(), state, role) == _msgs()

    def test_each_role_hears_what_to_do(self, tmp_path):
        lg = Ledger(tmp_path)
        a, _ = lg.record("Create then edit refused", "the editor refuses a "
                         "file apply_patch created", paths=[PATH],
                         reproduce="bun x vitest run a.test.ts")
        lg.settle(a.id, "confirmed", "re-run on ac1ee35", by="user")
        b, _ = lg.record("Read ledger unwired", "the read ledger is never "
                         "consulted by the editor tools",
                         paths=["src/extensions/tools/executors/index.ts:167"])
        lg.settle(b.id, "by_design", "unwired on purpose, see the comment at "
                  "index.ts:160", by="user")
        state = {"cwd": str(tmp_path),
                 "question": f"audit the editor tools, {PATH} and index.ts"}
        plan = memory.with_known_findings(_msgs(), state, "planner")[-1]["content"]
        assert plan.startswith("U\n\nKNOWN FINDINGS")
        assert f"{a.id} [confirmed]" in plan and "re-verify it" in plan
        assert "reproduce: bun x vitest run a.test.ts" in plan
        crit = memory.with_known_findings(_msgs(), state, "critic")[-1]["content"]
        assert "unwired on purpose" in crit
        assert "only with behaviour that contradicts that reason" in crit
        syn = memory.with_known_findings(_msgs(), state, "synthesizer")[-1]["content"]
        assert "`(known: <id>)`" in syn and syn.rstrip().endswith("else `(new)`.")

    def test_an_unreadable_ledger_changes_nothing(self, tmp_path):
        d = tmp_path / ".claude-hooks" / "findings"
        d.mkdir(parents=True)
        (d / "config.toml").write_text('prefix = "xf"\n')
        (d / "xf-1.md").write_text("not a finding")
        state = {"cwd": str(tmp_path), "question": "q"}
        assert memory.with_known_findings(_msgs(), state, "planner") == _msgs()


class TestRecording:

    def test_ranked_items_citing_a_run_become_candidates(self, tmp_path):
        got = memory.record_answer(str(tmp_path), "csl-1", ANSWER, ROWS)
        assert len(got) == 1 and got[0][1] is True
        f = Ledger(tmp_path).read(got[0][0])
        assert f.status == "candidate" and f.councils == ["csl-1"]
        assert f.paths == [PATH] and f.reproduce == "bun x vitest run a.test.ts"
        assert "C1 [researcher lane 0]" in f.evidence_lines[0]
        assert "edit refused as unread" in f.evidence_lines[0]

    def test_a_second_council_adds_evidence_and_settled_stays(self, tmp_path):
        fid = memory.record_answer(str(tmp_path), "csl-1", ANSWER, ROWS)[0][0]
        Ledger(tmp_path).settle(fid, "confirmed", "re-run", by="user")
        again = memory.record_answer(str(tmp_path), "csl-2", ANSWER, ROWS)
        assert again == [(fid, False)]
        f = Ledger(tmp_path).read(fid)
        assert f.status == "confirmed" and f.councils == ["csl-1", "csl-2"]

    def test_nothing_without_a_shell_or_a_run(self, tmp_path):
        assert memory.record_answer(str(tmp_path), "s", ANSWER, None) == []
        no_run = ANSWER.replace("(C1)", "")
        assert memory.record_answer(str(tmp_path), "s", no_run, ROWS) == []
        assert memory.record_answer("/nowhere/at/all", "s", ANSWER, ROWS) == []
        assert not (tmp_path / ".claude-hooks").exists()


def test_the_synthesizer_reads_and_records(tmp_path):
    from consultants.engine.council import synthesizer_node
    db = _run3_like(tmp_path)
    lg = Ledger(tmp_path)
    old, _ = lg.record("grep launders read stamps", "grep's read credit "
                       "stamps a file it never returned", paths=["src/grep.ts:300"])
    good = ("## Defects\n1. grep launders read stamps, src/grep.ts:300 (C3)\n\n"
            "## Dropped\n- C5-C6: stock\n\n## Unverified\n(none)")
    c = _Seq(good, "A1: SHOWN — it printed it")
    rec = SimpleNamespace(db_path=db, record_node=lambda **k: None,
                          record_llm_call=lambda **k: None,
                          _meta=SimpleNamespace(sid="csl-9"))
    state = {"question": "audit grep, src/grep.ts", "plan": "p",
             "research": ["REPORT"], "critique": "ok", "cwd": str(tmp_path)}
    synthesizer_node(state, chat_client=c, model="m", recorder=rec, shell=True,
                     cwd=str(tmp_path))
    assert "KNOWN FINDINGS" in c.calls[0]["messages"][-1]["content"]
    f = Ledger(tmp_path).read(old.id)
    assert "csl-9" in f.councils and len(Ledger(tmp_path).list()) == 1


def test_the_planner_and_critics_read_it(tmp_path):
    from consultants.engine.council import (
        critic_node, meta_critic_node, planner_node,
    )
    lg = Ledger(tmp_path)
    lg.record("Create then edit refused", "the editor refuses a file "
              "apply_patch created", paths=[PATH])
    state = {"question": f"audit {PATH}", "plan": "p", "research": ["REPORT"],
             "critique": "ok", "cwd": str(tmp_path),
             "critic_verdicts": ["DECISION: ready"]}
    for node in (planner_node, critic_node, meta_critic_node):
        c = _Seq("DECISION: ready\nPLAN: x")
        node(dict(state), chat_client=c, model="m")
        assert "KNOWN FINDINGS" in c.calls[0]["messages"][-1]["content"], node


class TestRun9:

    def test_an_item_called_new_that_the_ledger_holds_is_noted(self, tmp_path):
        # Council run 9 ranked csf-20 as "(new)".
        lg = Ledger(tmp_path)
        f, _ = lg.record("Create then edit refused", "the editor refuses a "
                         "file apply_patch created", paths=[PATH])
        new = ANSWER.replace("(C1).", "(C1). (new)")
        out, n = memory.note_known_new(str(tmp_path), new)
        line = next(ln for ln in out.split("\n") if "(new)" in ln)
        assert n == 1 and memory.KNOWN_NOTE.format(
            id=f.id, status=f.status, title=f.title) in line
        assert memory.note_known_new(str(tmp_path), out) == (out, 0)   # once
        tagged = new.replace("(C1).", f"(C1, {f.id}).")
        assert memory.note_known_new(str(tmp_path), tagged)[1] == 0
        assert memory.note_known_new(str(tmp_path), ANSWER) == (ANSWER, 0)

    def test_no_ledger_or_nothing_new_changes_nothing(self, tmp_path):
        new = ANSWER.replace("(C1).", "(C1). (new)")
        assert memory.note_known_new(str(tmp_path), new) == (new, 0)
        assert memory.note_known_new("", new) == (new, 0)

    def test_a_cross_examined_claim_is_not_asked_for_a_run_again(self):
        # Council run 9: lane 0 narrowed its grep claim against csf-12 in
        # the cross-examination; the answer still demanded "a run against
        # that reason".
        f = SimpleNamespace(id="csf-12", status="by_design", title="t",
                            decision="The full-file read credit is deliberate.")
        other = SimpleNamespace(id="csf-3", status="refuted", title="u",
                                decision="not real")
        rulings = [
            {"round": 1, "report": 1, "lane": 0, "ruling": "narrowed",
             "why": "only the column axis csf-12 never decided",
             "challenges": ["restates csf-12"], "answer": "conceded spans"},
            {"round": 1, "report": 2, "lane": 1, "ruling": "falls", "why": "x",
             "challenges": ["restates csf-3"], "answer": ""},
            {"round": 1, "report": 3, "lane": 2, "ruling": "stands", "why": "y",
             "challenges": ["about csf-123"], "answer": ""}]
        ex = memory.examined_against([f, other], rulings)
        assert list(ex) == ["csf-12"]
        text = "## Defects\n\n1. grep credits a cut line (C3)\n2. lock (C3)\n"
        items = memory.answer_items(text)
        out, n = memory.apply_settled_audit(text, [f, other], items,
                                            {1: 1, 2: 2}, examined=ex)
        one, two = (ln for ln in out.split("\n") if ln.startswith(("1.", "2.")))
        assert "cross-examined against it in round 1 and ruled narrowed — only " \
               "the column axis csf-12 never decided." in one
        assert "needs a run" not in one and "needs a run" in two
        assert memory.apply_settled_audit(out, [f, other], items, {1: 1, 2: 2},
                                          examined=ex)[1] == 0
