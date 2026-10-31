"""The evidence bar at the synthesizer, the critic and the held tests
(council run 3, csl-2026-10-18-1257-42d3, mail #1633)."""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

from consultants.engine import evidence as E

OK = "exit=0 wall=1.0s killed_by=none\n--- stdout ({n} chars) ---\n{body}"


def _out(body, code=0):
    return (f"exit={code} wall=1.0s killed_by=none\n"
            f"--- stdout ({len(body)} chars) ---\n{body}")


# Lane 100's P-probe of run 3, shortened: the evidence is in what the
# tests printed; the tail says only that they passed.
VITEST_PRINTS = "\n".join([
    "", " RUN  v4.1.11 /x/sdk/packages/core", "",
    "stdout | src/zz-probe.test.ts > P3 writers > apply_patch Add File, then editor line 1",
    'P3b= "THREW: Read before editing: added.txt has not been read"', "",
    "stdout | src/zz-probe.test.ts > P3 writers > apply_patch Move over a file the model HAD read",
    'P3d dst content= "one\\nBETA\\nthree\\n" edit= "Replaced line 2"', "",
    " ✓ src/zz-probe.test.ts (11 tests) 50ms", "",
    " Test Files  1 passed (1)", "      Tests  11 passed (11)",
    "   Start at  13:13:04",
    "   Duration  654ms (transform 412ms, setup 0ms, import 488ms, tests 50ms)",
    ""])
CONFIG_FAIL = ("failed to load config from /x/vitest.config.ts\n\n"
               "⎯⎯⎯ Startup Error ⎯⎯⎯\nError: Cannot find module 'vitest/config'")
SUITE_FAIL = ("❯ src/a.test.ts (0 test)\n⎯⎯ Failed Suites 1 ⎯⎯\n FAIL  src/a.test.ts\n"
              "Error: Cannot find module '../../runtime/atomic/file-revisions'\n"
              " Test Files  1 failed (1)\n      Tests  no tests\n")


def _db(tmp_path, rows):
    """rows: (role, lane, tool, args, output)."""
    p = tmp_path / "transcript.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE events (event_id INTEGER PRIMARY KEY, ts REAL, "
              "kind TEXT, role TEXT, round INT, lane_idx INT, tool TEXT, "
              "args TEXT, output TEXT)")
    for role, lane, tool, args, out in rows:
        c.execute("INSERT INTO events (kind, role, lane_idx, tool, args, "
                  "output) VALUES ('tool_call', ?, ?, ?, ?, ?)",
                  (role, lane, tool, json.dumps(args), out))
    c.commit()
    c.close()
    return p


def _cmd(role, lane, command, out):
    return (role, lane, "run_command", {"command": command}, out)


def _run3_like(tmp_path):
    return _db(tmp_path, [
        _cmd("researcher", 5, "bun x vitest run src/a.test.ts", _out(CONFIG_FAIL, 1)),
        _cmd("researcher", 6, "cat vitest.config.ts", _out("export default {}")),
        _cmd("researcher", 100, "cd core && bun x vitest run src/zz-probe.test.ts",
             _out(VITEST_PRINTS)),
        _cmd("researcher", 100, "git show abc", _out("diff")),
        _cmd("researcher", 4, "bun x vitest run src/read-receipts.test.ts",
             _out(" ✓ read-receipts (10 tests)\n Tests  10 passed (10)\n")),
        _cmd("researcher", 4, "bun x vitest run src/read-receipts.test.ts",
             _out(" ✓ read-receipts (10 tests)\n Tests  10 passed (10)\n")),
    ])


class TestRunsThatReachedTheCode:

    def test_classification(self, tmp_path):
        rows = {r["id"]: r for r in E.commands_ran(_run3_like(tmp_path))}
        assert not rows["C1"]["ran"]                 # config did not load
        assert not E.is_probe("cat vitest.config.ts")
        assert rows["C3"]["ran"] and rows["C5"]["ran"]
        assert not rows["C4"]["ran"]                 # git is not a probe

    def test_a_suite_that_does_not_load_did_not_reach_the_code(self):
        assert not E._reached_the_code("bun x vitest run a.test.ts",
                                       _out(SUITE_FAIL, 1))
        assert E._reached_the_code("node probe.js", _out("P1 ok"))
        assert not E._reached_the_code("node probe.js",
                                       _out("Error: Cannot find module 'x'", 1))
        assert E._reached_the_code("bun test", _out(" 3 pass\n 0 fail"))

    def test_the_block_shows_what_a_test_printed(self, tmp_path):
        db = _run3_like(tmp_path)
        b = E.commands_block(db)
        assert "· apply_patch Move over a file the model HAD read: P3d dst" in b
        assert "Tests  11 passed (11)" in b          # the tail stays
        printed = b.split("C3 [")[1].split("end of output")[0]
        assert "Duration" not in printed and "P3b=" in printed
        assert "C6 [researcher lane 4]" in b and "also ran as C5" in b
        assert "never reached the code" in b
        assert "C1 [researcher lane 5]" in b and "failed to load config" in b
        assert E.EVIDENCE_RULES in b
        assert E.EVIDENCE_RULES not in E.commands_block(db, rules=False)


ANSWER = """### Correctness Defects

| Rank | Defect | Trigger | Fix |
| :--- | :--- | :--- | :--- |
| **1** | **Grep laundering** (C3) | grep after a write | check first |
| **2** | **Coordinate mismatch** | end of line | align |

### Other findings
1. Sed address leak at `sed.ts:585`.
   It applies to lines never read.
2. Rollback without forgetReads (C2).

### Usefulness Improvements
1. Divergence windows.

### Unverified
(none)
"""


class TestAudit:

    def test_ranked_items_are_rows_and_top_level_items(self):
        items = [E._label(i["text"]) for i in E.ranked_items(ANSWER)]
        assert items == ["Grep laundering (C3) — grep after a write",
                         "Coordinate mismatch — end of line",
                         "Sed address leak at `sed.ts:585`",
                         "Rollback without forgetReads (C2)"]

    def test_unbacked_and_uncited(self, tmp_path):
        rows = E.commands_ran(_run3_like(tmp_path))
        a = E.audit_answer(ANSWER, rows)
        # C2 is `cat`, not a run that can show a defect.
        assert [E._label(i["text"])[:12] for i in a.unbacked] == [
            "Coordinate m", "Sed address ", "Rollback wit"]
        # C1 never reached the code: nothing is owed for it.
        assert [[r["id"] for r in g] for g in a.uncited] == [["C5", "C6"]]
        assert not a.missing_unverified and not a.ok

    def test_a_range_cites_every_run_in_it(self, tmp_path):
        rows = E.commands_ran(_run3_like(tmp_path))
        text = ANSWER.replace("(none)", "## Dropped\n- C4–C6: reruns")
        assert E.audit_answer(text, rows).uncited == []
        assert E.cited("C3-C5, C9") == {3, 4, 5, 9}

    def test_no_shell_checks_only_the_section(self):
        a = E.audit_answer("## Defects\n1. x", None)
        assert a.missing_unverified and not a.unbacked and not a.uncited

    def test_the_note_names_what_is_owed(self, tmp_path):
        rows = E.commands_ran(_run3_like(tmp_path))
        note = E.revision_note(E.audit_answer(ANSWER, rows))
        assert "Coordinate mismatch — end of line" in note
        assert "C5, C6 [researcher lane 4]" in note and "Tests 10 passed" in note
        assert "`## Dropped`" in note and "`## Read, not run`" in note

    def test_enforce_moves_and_lists(self, tmp_path):
        rows = E.commands_ran(_run3_like(tmp_path))
        out = E.enforce(ANSWER, E.audit_answer(ANSWER, rows))
        ranked = out.split("### Usefulness")[0]
        assert "Grep laundering" in ranked
        assert "Coordinate mismatch" not in ranked
        assert "Sed address leak" not in ranked
        unverified = out.split("### Unverified")[1].split("## Dropped")[0]
        assert "(none)" not in unverified
        assert ("- Coordinate mismatch — end of line: " + E.MOVED_NOTE) in unverified
        assert "## Dropped by omission" in out and "C5, C6 [researcher lane 4]" in out
        # Nothing left to enforce on the result.
        again = E.audit_answer(out, rows)
        assert not again.unbacked
        assert not again.uncited

    def test_enforce_adds_a_section_when_there_is_none(self, tmp_path):
        rows = E.commands_ran(_run3_like(tmp_path))
        text = "## Defects\n1. drift\n"
        out = E.enforce(text, E.audit_answer(text, rows))
        assert "## Unverified" in out and "- drift: moved here" in out

    def test_an_answer_that_meets_the_bar_is_untouched(self, tmp_path):
        rows = E.commands_ran(_run3_like(tmp_path))
        text = ("## Defects\n1. grep laundering (C3)\n\n## Dropped\n"
                "- C5, C6: the stock suite, passes before and after\n\n"
                "## Unverified\n(none)\n")
        a = E.audit_answer(text, rows)
        assert a.ok and E.enforce(text, a) == text


def _resp(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


class _Seq:
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def chat(self, payload, *, think=True, **_kw):
        self.calls.append(payload)
        return _resp(self.replies.pop(0) if self.replies else "")


def _rec(db):
    return SimpleNamespace(db_path=db, record_node=lambda **k: None,
                           record_llm_call=lambda **k: None)


def _state():
    return {"question": "q", "plan": "p", "research": ["REPORT"],
            "critique": "ok", "cwd": ""}


class TestSynthesizer:

    def test_an_unbacked_defect_is_asked_about_then_moved(self, tmp_path):
        from consultants.engine.council import synthesizer_node
        db = _run3_like(tmp_path)
        first = "## Defects\n1. grep laundering\n\n## Unverified\n(none)"
        c = _Seq(first, first)                    # the revision changes nothing
        out = synthesizer_node(_state(), chat_client=c, model="m",
                               recorder=_rec(db), shell=True)
        assert len(c.calls) == 2
        note = c.calls[1]["messages"][-1]["content"]
        assert "cite no run that shows them" in note and "grep laundering" in note
        assert "C3 [researcher lane 100]" in note     # P3d's run is named
        ans = out["final_answer"]
        assert "## Unverified\n- grep laundering: moved here" in ans
        assert "## Dropped by omission" in ans

    def test_a_revision_that_cites_the_runs_stands(self, tmp_path):
        from consultants.engine.council import synthesizer_node
        db = _run3_like(tmp_path)
        good = ("## Defects\n1. grep laundering (C3)\n2. apply_patch move keeps "
                "dst spans (C3, P3d)\n\n## Dropped\n- C5, C6: stock suite\n\n"
                "## Unverified\n(none)")
        c = _Seq("## Defects\n1. grep laundering", good)
        out = synthesizer_node(_state(), chat_client=c, model="m",
                               recorder=_rec(db), shell=True)
        assert out["final_answer"].strip() == good

    def test_an_answer_that_meets_the_bar_is_not_re_asked(self, tmp_path):
        """No revision; the only other call is the claim audit (run 4)."""
        from consultants.engine.council import synthesizer_node
        db = _run3_like(tmp_path)
        good = ("## Defects\n1. x (C3)\n\n## Dropped\n- C5-C6: stock\n\n"
                "## Unverified\n(none)")
        c = _Seq(good, "A1: SHOWN — it printed it")
        out = synthesizer_node(_state(), chat_client=c, model="m",
                               recorder=_rec(db), shell=True)
        assert len(c.calls) == 2
        assert c.calls[1]["messages"][0]["content"].startswith(E.CLAIM_AUDIT_SYSTEM)
        assert out["final_answer"].strip() == good


class TestCritic:

    def test_the_critic_sees_the_runs_on_a_shell_run_only(self, tmp_path):
        from consultants.engine.council import critic_node
        db = _run3_like(tmp_path)
        for shell in (False, True):
            c = _Seq("DECISION: needs_more_research")
            critic_node(_state(), chat_client=c, model="m", recorder=_rec(db),
                        shell=shell)
            last = c.calls[0]["messages"][-1]["content"]
            assert ("COMMANDS THE LANES RAN" in last) is shell
            assert (E.CRITIC_LEDGER_NOTE in last) is shell
            assert E.EVIDENCE_RULES not in last


class TestHeldTests:

    def test_never_run_failed_and_green(self, tmp_path):
        db = _db(tmp_path, [
            ("coder", 0, "write_file", {"path": "src/audit.test.ts",
                                        "content": "x"}, "wrote"),
            _cmd("coder", 0, "bun x vitest run src/other.test.ts",
                 _out(" Tests  3 passed (3)")),
            _cmd("researcher", 101, "cat << 'EOF' > src/gate.test.ts\nx\nEOF\n"
                 "bun x vitest run src/gate.test.ts", _out(SUITE_FAIL, 1)),
            _cmd("researcher", 100, "cat > src/zz-probe.test.ts <<'EOF'\nx\nEOF\n"
                 "bun x vitest run src/zz-probe.test.ts", _out(VITEST_PRINTS, 1)),
            _cmd("researcher", 100, "bun x vitest run src/zz-probe.test.ts",
                 _out(VITEST_PRINTS)),
        ])
        assert E.held_test_notes(db, "coder-0", ["src/audit.test.ts", "src/a.ts"]) \
            == {"src/audit.test.ts": "⚠ never run after it was written"}
        assert E.held_test_notes(db, "researcher-101", ["src/gate.test.ts"]) == {
            "src/gate.test.ts": "⚠ its last run failed (exit 1)"}
        assert E.held_test_notes(db, "researcher-100", ["src/zz-probe.test.ts"]) == {}
        assert E.held_test_notes(db, "nolane", ["src/audit.test.ts"]) == {}

    def test_the_summary_flags_them(self):
        from claude_hooks.sandbox.views import HELD, summary
        views = SimpleNamespace(sid="csl-x", kind="git", lanes=lambda: {
            "coder-0": {"state": HELD, "patch": "p", "files": [
                {"status": "A", "path": "a.test.ts", "added": 1, "removed": 0},
                {"status": "M", "path": "b.ts", "added": 1, "removed": 1}]}})
        text = summary(views, file_notes=lambda lane, paths: {
            "a.test.ts": "⚠ never run after it was written"})
        assert "`a.test.ts` (+1/-0) — ⚠ never run after it was written" in text
        assert "`b.ts` (+1/-1)\n" in text
        assert "1 held test file(s) did not run green" in text
        assert "did not run green" not in summary(views)

        def boom(*_):
            raise RuntimeError("x")
        assert "`a.test.ts` (+1/-0)\n" in summary(views, file_notes=boom)


def test_the_critic_checks_case_labels_against_the_runs():
    """Run 5 (mail #1641): a lane's BLIND label went unchallenged."""
    assert "label a report gives a case" in E.CRITIC_LEDGER_NOTE
    assert "BLIND" in E.CRITIC_LEDGER_NOTE
