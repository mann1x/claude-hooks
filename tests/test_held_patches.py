"""Held patches in the answer (council run 6, mail #1648): what each lane's
patch fixes, two lanes on one function, comments against the diff."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from consultants.engine import evidence
from consultants.engine import held_patches as hp
from tests.test_evidence_audit import _Seq, _run3_like

if not shutil.which("git"):
    pytest.skip("git", allow_module_level=True)

from claude_hooks.sandbox import views as V  # noqa: E402

SRC = "sdk/packages/core/src/extensions/tools/delimiter-balance.ts"
AP = "sdk/packages/core/src/extensions/tools/executors/apply-patch.ts"


def _diff(path, start, n, added, removed=()):
    body = "".join(f"-{x}\n" for x in removed) + "".join(f"+{x}\n" for x in added)
    return (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
            f"@@ -{start},{n} +{start},{n + len(added) - len(removed)} @@\n"
            + body)


def _lane(name, *diffs):
    d = "".join(diffs)
    return hp.LanePatch(lane=name, diff=d, hunks=hp.parse_diff(name, d))


#: Run 6, shortened: two lanes rewrite offsetIn; researcher-100's
#: apply-patch comment claims a lock its hunk does not add.
CODER_1 = _lane("coder-1", _diff(SRC, 485, 6, [
    "// A CRLF line ends at its `\\r`, and that is the append position too.",
    "const end = line.endsWith('\\r') ? line.length - 1 : line.length;"]))
R100 = _lane(
    "researcher-100",
    _diff(SRC, 488, 4, [
        "// Walked over the raw body rather than rebuilt from lines[i].length + 1,",
        "// which put every position one character early per CRLF line.",
        "for (let i = 0, at = 0; i < body.length; i++) {"]),
    _diff(AP, 386, 2, [
        "// `editor` and `sed -i` both hold the read gate and the per-path lock.",
        "// A patch is the same read-modify-write and had neither: two concurrent",
        "// patches on one file lost an update.",
        "const requireRead = (absPath: string, content: string) => {",
        "  if (!receipts) return;",
        "};"]))
TESTS_ONLY = _lane("researcher-101", _diff(
    "sdk/packages/core/src/repro-d1.test.ts", 0, 0, ["it('x', () => {})"]))


class TestReadingThePatches:

    def test_two_lanes_on_the_same_lines_overlap(self):
        o = hp.overlaps([CODER_1, R100])
        assert [(x["path"], x["lanes"]) for x in o] == [
            (SRC, ("coder-1", "researcher-100"))]
        assert o[0]["lines"] == (485, 492)

    def test_other_lines_tests_and_one_lane_do_not(self):
        far = _lane("coder-0", _diff(SRC, 900, 3, ["x();"]))
        assert hp.overlaps([CODER_1, far]) == []
        t2 = _lane("coder-0", _diff("sdk/packages/core/src/repro-d1.test.ts",
                                    0, 0, ["it('y')"]))
        assert hp.overlaps([TESTS_ONLY, t2]) == []
        assert not TESTS_ONLY.source_files

    def test_the_block_shows_the_diff_and_the_overlap(self):
        b = hp.held_block([CODER_1, R100], hp.overlaps([CODER_1, R100]))
        assert b.startswith("HELD CHANGES")
        assert "### lane `researcher-100` — source: " in b
        assert "+const requireRead" in b and hp.HELD_RULES in b
        assert f"- `{SRC}` lines 485–491: `coder-1` and `researcher-100`" in b
        assert hp.held_block([], []) == ""



def _rows(tmp_path):
    return evidence.commands_ran(_run3_like(tmp_path))


def _answer(held: str) -> str:
    return ("## Defects\n\n1. grep laundering (C3)\n2. apply_patch (C3)\n\n"
            f"## Held patches\n\n{held}\n\n## Unverified\n\n(none)\n")


class TestTheHeldPatchesSection:

    def test_a_lane_with_no_entry_and_an_unchosen_overlap(self, tmp_path):
        rows = _rows(tmp_path)
        text = _answer("- `coder-1`: addresses #3, not verified.")
        a = hp.audit(text, [CODER_1, R100], hp.overlaps([CODER_1, R100]), rows)
        assert [p.lane for p in a.missing] == ["researcher-100"]
        assert len(a.open) == 1 and not a.ok
        note = hp.revision_note(a)
        assert "`researcher-100`: " in note and "conflict: <lane> vs <lane>" in note
        out = hp.enforce(text, a)
        held = out[out.index("## Held patches"):out.index("## Unverified")]
        assert "`researcher-100`" in held and hp.NOT_ASSESSED in held
        assert "conflict, not adjudicated (harness): `coder-1` and " \
               "`researcher-100` both change" in held

    def test_a_fix_needs_a_green_run_of_that_lane(self, tmp_path):
        rows = _rows(tmp_path)    # C3: researcher lane 100; C5: lane 4, green
        r4 = _lane("researcher-4", _diff(AP, 10, 2, ["x();"]))
        ok = _answer("- researcher lane 4: fixes #2 (C5); does not fix #1.")
        assert hp.audit(ok, [r4], [], rows).ok
        other = _answer("- researcher-4: fixes #2 (C3); does not fix #1.")
        a = hp.audit(other, [r4], [], rows)
        assert a.unbacked == [("researcher-4", 2)]
        out = hp.enforce(other, a)
        line = next(ln for ln in out.split("\n") if "researcher-4" in ln)
        assert hp.UNBACKED_FIX.format(n=2) in line
        assert hp.UNBACKED_FIX.format(n=1) not in out    # "does not fix #1"
        bare = _answer("- researcher-4: fixes #2.")
        assert hp.audit(bare, [r4], [], rows).unbacked == [("researcher-4", 2)]

    def test_naming_both_lanes_in_one_entry_settles_the_overlap(self, tmp_path):
        rows = _rows(tmp_path)
        text = _answer(
            "- coder-1: addresses #3, not verified. Apply it, not "
            "researcher-100's: its offset rewrite rests on a false claim.\n"
            "- researcher-100: addresses #1, not verified; does not fix #3.")
        a = hp.audit(text, [CODER_1, R100], hp.overlaps([CODER_1, R100]), rows)
        assert a.ok, a

    def test_a_fix_for_a_defect_the_patch_does_not_touch(self, tmp_path):
        # Council run 9: "coder-1 fixes #4 (C322)"; #4 is grep.ts, which
        # only coder-2 changed.
        rows = _rows(tmp_path)
        grep = "sdk/packages/core/src/extensions/tools/executors/grep.ts"
        r4 = _lane("researcher-4", _diff(AP, 10, 2, ["x();"]))
        g = _lane("coder-2", _diff(grep, 260, 2, ["y();"]),
                  _diff(AP, 400, 2, ["z();"]))
        g.touches = [{"id": "csf-1", "status": "candidate", "title": "t",
                      "where": f"{grep}:260"}]
        text = ("## Defects\n\n| # | Defect | Where |\n| --- | --- | --- |\n"
                "| 1 | patch launders | `apply-patch.ts:403` (C5) |\n"
                "| 2 | CRLF anchor | `grep.ts:266` (C5) |\n\n## Held patches\n\n"
                "- researcher lane 4: fixes #1 (C5), fixes #2 (C5), fixes csf-1 (C5).\n"
                "- coder-2: addresses #2, not verified; addresses csf-1, not verified.\n"
                "\n## Unverified\n\n(none)\n")
        a = hp.audit(text, [r4, g], [], rows)
        assert a.elsewhere == [("researcher-4", 2, ("grep.ts",)),
                               ("researcher-4", "csf-1", (grep,))]
        assert "fixes #2, but changes none of grep.ts" in hp.revision_note(a)
        out = hp.enforce(text, a)
        line = next(ln for ln in out.split("\n") if "researcher lane 4" in ln)
        assert hp.ELSEWHERE.format(files="grep.ts", ref="#2") in line
        assert hp.ELSEWHERE.format(files=grep, ref="csf-1") in line
        assert "#1 is" not in line                     # apply-patch.ts: its own
        assert hp.UNBACKED_FIX.format(n=2) not in line  # C5 is green and its own

    def test_every_ref_of_one_fixes_is_a_claim(self):
        # Council run 9's Held patches, verbatim.
        got = hp._fix_claims(
            "- `coder-0`: fixes #1 (C288), #6 (addresses, not verified), #7 "
            "(addresses, not verified), #10 (C46). Fixes known csf-1, csf-12 "
            "(leaves as is), csf-7 (addresses, not verified).")
        assert [r for r, _ in got] == [1, 10, "csf-1"]
        assert [r for r, _ in hp._fix_claims(
            "- `coder-1`: fixes #2 (C322), #4 (C322), #8 (C322).")] == [2, 4, 8]
        [(r, w)] = hp._fix_claims("fixes #1, does not fix #2 (C9)")
        assert r == 1
        assert [r for r, w in hp._fix_claims("fixes #1, #2 (C5)")] == [1, 2]
        assert all("C5" in w for _, w in hp._fix_claims("fixes #1, #2 (C5)"))

    def test_a_held_patches_table_is_neither_ranked_nor_a_matrix(self):
        text = ("## Held patches\n\n| Lane | Fixes |\n| --- | --- |\n"
                "| coder-1 | ✓ #3 (C3) |\n\n## Unverified\n\n(none)\n")
        assert evidence.ranked_items(text) == []
        assert evidence.matrix_rows(text) == []


class TestCommentsAgainstTheDiff:

    def test_each_added_comment_comes_with_its_hunks_code(self):
        items = hp.comment_items([CODER_1, R100])
        ap = next(i for i in items if i["path"] == AP)
        assert ap["lane"] == "researcher-100" and ap["line"] == 386
        assert "had neither" in ap["comment"] and "//" not in ap["comment"]
        assert any("requireRead" in x for x in ap["added"])
        assert not any(x.lstrip().startswith("//") for x in ap["added"])
        p = hp.comment_audit_prompt(items)
        assert "ITS HUNK ADDS:\n+ const requireRead" in p

    def test_a_short_comment_is_not_a_claim(self):
        assert hp.comment_items([CODER_1])[0]["path"] == SRC
        tiny = _lane("coder-0", _diff(SRC, 1, 1, ["// fix CRLF", "x();"]))
        assert hp.comment_items([tiny]) == []

    def test_every_file_is_reached_before_a_second_comment(self, monkeypatch):
        monkeypatch.setattr(hp, "COMMENT_MAX_ITEMS", 2)
        many = _lane("coder-0", *[_diff(SRC, 10 * k + 1, 1, [
            f"// comment number {k} says the scan now splits like the editor",
            "x();"]) for k in range(5)])
        got = hp.comment_items([many, R100])
        assert {i["path"] for i in got} == {SRC, AP}

    def test_a_comment_not_shown_is_noted_under_held_patches(self):
        items = hp.comment_items([R100])
        k = next(n for n, i in enumerate(items, 1) if i["path"] == AP)
        text = _answer("- researcher-100: addresses #2, not verified.")
        out, n = hp.apply_comment_audit(text, items, evidence.parse_claim_audit(
            f"A{k}: NOT SHOWN — the hunk adds requireRead, no withFileLock", len(items)))
        held = out[out.index("## Held patches"):out.index("## Unverified")]
        assert n == 1 and "⚠ `researcher-100` `" + AP + ":386`" in held
        assert "does not contain it: the hunk adds requireRead, no withFileLock" in held


def _git(repo, *args) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    (r / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (r / ".gitignore").write_text(".claude-hooks/consultants/\n")
    _git(r, "add", "-A")
    _git(r, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    return r


@pytest.mark.skipif(sys.platform != "linux", reason="the Linux sandbox's views")
class TestLiveViews:

    def test_a_worktree_preview_has_edits_and_new_files_and_writes_nothing(
            self, repo):
        v = V.session_views(sid="csl-h", cwd=str(repo))
        v.ensure("coder-0")
        v.ensure("researcher-101")
        wt = Path(v.lanes()["coder-0"]["wt"])
        (wt / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        (wt / "new_mod.py").write_text("X = 1\n")
        t = Path(v.lanes()["researcher-101"]["wt"])
        (t / "test_calc.py").write_text("def test_x(): pass\n")
        state = v.state_path.read_bytes()
        d = v.preview("coder-0")
        assert "+    return a + b" in d and "+++ b/new_mod.py" in d
        assert v.state_path.read_bytes() == state
        assert v.lanes()["coder-0"]["state"] == "active"
        got = hp.held_changes(str(repo), "csl-h")
        assert [p.lane for p in got] == ["coder-0"]       # tests only: no entry
        assert got[0].source_files == ["calc.py", "new_mod.py"]
        v.freeze_all()
        assert v.preview("coder-0") == "" and hp.held_changes(str(repo), "csl-h") == []

    def test_a_clone_preview_names_the_scope(self, tmp_path):
        from claude_hooks.sandbox import clone_views as C
        r = tmp_path / "models"
        (r / "scripts").mkdir(parents=True)
        (r / "scripts" / "run.py").write_text("print('v1')\n")
        v = V.session_views(sid="csl-hc", cwd=str(r), scopes=["scripts"])
        assert isinstance(v, C.CloneViews)
        v.ensure("coder-0")
        clone = Path(v.lanes()["coder-0"]["clones"][os.path.join(v.repo, "scripts")])
        (clone / "run.py").write_text("print('v2')\n")
        d = v.preview("coder-0")
        assert "+++ b/scripts/run.py" in d and "+print('v2')" in d

    def test_no_session_or_no_views_is_nothing(self, tmp_path):
        assert hp.held_changes("", "x") == []
        assert hp.held_changes(str(tmp_path), "csl-none") == []
        assert hp.hand_over(str(tmp_path), "csl-none", "researcher-100") == ""

    def test_a_later_lane_is_handed_the_other_lanes_patches(self, repo):
        # Council run 9: the round after the brief spent 220 calls looking
        # for the coders' patches under the masked session store.
        v = V.session_views(sid="csl-ho", cwd=str(repo))
        for lane in ("coder-0", "coder-1", "researcher-100", "researcher-101"):
            v.ensure(lane)
        wt = {lane: Path(r["wt"]) for lane, r in v.lanes().items()}
        (wt["coder-0"] / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        (wt["coder-1"] / "calc.py").write_text("def add(a, b):\n    return b + a\n")
        (wt["coder-1"] / "test_calc.py").write_text("def test_add(): pass\n")
        (wt["researcher-101"] / "calc.py").write_text("sibling\n")
        note = hp.hand_over(str(repo), "csl-ho", "researcher-100",
                            skip=lambda lane: lane == "researcher-101")
        held = v.lane_dir("researcher-100") / "scratch" / hp.HANDOVER_DIR
        assert sorted(p.name for p in held.iterdir()) == ["coder-0.patch",
                                                          "coder-1.patch"]
        assert (held / "coder-0.patch").read_text() == v.preview("coder-0")
        assert note.startswith(hp.HANDOVER_HEAD) and hp.HANDOVER_RULES in note
        assert "- /scratch/held/coder-1.patch — calc.py, test_calc.py" in note
        assert "OVERLAP: coder-0 and coder-1 both change calc.py lines 1-2" in note
        # It applies in the later lane's own view, and the copy left there
        # is not that lane's patch.
        env = {**os.environ, **V.SessionViews._host_env(v.lanes()["researcher-100"])}
        subprocess.run(["git", "apply", str(held / "coder-0.patch")],
                       cwd=wt["researcher-100"], check=True, env=env,
                       capture_output=True)
        assert "return a + b" in (wt["researcher-100"] / "calc.py").read_text()
        lanes = [p.lane for p in hp.held_changes(str(repo), "csl-ho")]
        assert "researcher-100" not in lanes and "coder-0" in lanes

    def test_nothing_to_hand_over_writes_nothing(self, repo):
        v = V.session_views(sid="csl-h0", cwd=str(repo))
        v.ensure("coder-0")
        assert hp.hand_over(str(repo), "csl-h0", "researcher-100") == ""
        assert not (v.lane_dir("researcher-100") / "scratch").exists()


def test_the_synthesizer_is_shown_the_patches_and_owes_the_section(
        tmp_path, monkeypatch):
    from consultants.engine.council import synthesizer_node
    db = _run3_like(tmp_path)
    monkeypatch.setattr(hp, "held_changes",
                        lambda cwd, sid: [CODER_1, R100] if sid == "csl-6" else [])
    first = ("## Defects\n1. CRLF prescription, delimiter-balance.ts:485 (C3)\n\n"
             "## Dropped\n- C5-C6: reruns of the receipts suite\n\n"
             "## Unverified\n(none)")
    c = _Seq(first, first, "A1: SHOWN — printed it",
             "A1: SHOWN — ok\nA2: SHOWN — ok\nA3: NOT SHOWN — no withFileLock")
    rec = SimpleNamespace(db_path=db, record_node=lambda **k: None,
                          record_llm_call=lambda **k: None,
                          _meta=SimpleNamespace(sid="csl-6"))
    state = {"question": "audit", "plan": "p", "research": ["REPORT"],
             "critique": "ok", "cwd": str(tmp_path)}
    out = synthesizer_node(state, chat_client=c, model="m", recorder=rec,
                           shell=True, cwd=str(tmp_path))
    assert "HELD CHANGES" in c.calls[0]["messages"][-1]["content"]
    revision = c.calls[1]["messages"][-1]["content"]
    assert "`## Held patches` has no entry for these lanes" in revision
    assert "`coder-1` and `researcher-100`" in revision
    assert c.calls[3]["messages"][0]["content"].startswith(hp.COMMENT_AUDIT_SYSTEM)
    ans = out["final_answer"]
    held = ans[ans.index("## Held patches"):]
    assert hp.NOT_ASSESSED in held and "conflict, not adjudicated" in held
    assert "⚠ `researcher-100`" in held


def test_a_comment_audit_sees_every_hunk_whole_and_batches_by_budget():
    code = [f"const v{i} = scan(line{i});" for i in range(3000)]
    big = _lane("coder-0", _diff(SRC, 1, 1, [
        "// The scan now splits every line the way the editor splits it, CRLF too.",
        *code]))
    items = hp.comment_items([big, R100])
    p = hp.comment_audit_prompt(items)
    assert all(f"+ {x}" in p for x in code)              # nothing cut
    assert "had neither: two concurrent" in p           # the whole comment
    groups = hp.comment_audit_batches(items, 5_000)
    assert sorted(i for g in groups for i in g) == list(range(len(items)))
    assert [0] in groups                                 # the big hunk alone
    assert hp.comment_audit_batches(items, 10**7) == [list(range(len(items)))]


def test_only_a_later_lane_on_a_shell_run_is_handed_patches(monkeypatch):
    from consultants.engine import council, graph
    calls = []
    monkeypatch.setattr(hp, "hand_over", lambda cwd, sid, lane, skip=None: (
        calls.append((cwd, sid, lane, skip)) or "HELD NOTE"))
    shell = [{"function": {"name": "run_command"}}]
    deps = SimpleNamespace(sid="csl-x", cwd="/r", tool_specs=shell)
    later = {"research": ["r1"], "lane_idx": 101, "cwd": "/r"}
    assert graph._held_note(deps, later) == "HELD NOTE"
    _, _, lane, skip = calls[-1]
    assert lane == "researcher-101"
    assert skip("researcher-100") and skip("researcher-102")     # siblings
    assert not skip("researcher-3") and not skip("coder-0")
    assert graph._held_note(deps, {**later, "plan_item": "x"}) == ""   # round 1
    assert graph._held_note(deps, {"lane_idx": 0}) == ""
    assert graph._held_note(SimpleNamespace(sid="s", cwd="/r", tool_specs=[]),
                            later) == ""                             # no shell
    n = len(calls)
    msgs = council.build_researcher_messages("q", "p", ["r1"], [],
                                             held_patches="HELD NOTE")
    assert msgs[-1]["content"].count("HELD NOTE") == 1
    assert "HELD" not in council.build_researcher_messages(
        "q", "p", ["r1"], [])[-1]["content"]
    assert len(calls) == n
