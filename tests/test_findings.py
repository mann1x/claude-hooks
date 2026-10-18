"""The council findings ledger (docs/PLAN-council-memory.md, M1)."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from claude_hooks.findings import Ledger
from claude_hooks.findings.model import Finding
from claude_hooks.findings.tools import FindingTools, tool_catalog

CLAIM = ("After apply_patch creates a file, the next editor call is refused "
         "as unread.")
PATH = "sdk/packages/core/src/extensions/tools/executors/apply-patch.ts:412"


@pytest.fixture
def lg(tmp_path):
    return Ledger(tmp_path)


class TestTheFile:

    def test_round_trip_keeps_what_a_person_adds(self, lg):
        f, _ = lg.record("create then edit refused", CLAIM, paths=[PATH],
                         evidence="C212 exit 1", reproduce="bun x vitest run a.test.ts",
                         council="csl-1")
        text = lg.path_for(f.id).read_text()
        text += "\n## Notes\nby hand\n"
        lg.path_for(f.id).write_text(text.replace("severity: M", "severity: M\nowner: me"))
        g = lg.read(f.id)
        assert g.claim == CLAIM and g.reproduce == "bun x vitest run a.test.ts"
        assert g.section("Notes") == "by hand" and g.extra == {"owner": "me"}
        assert g.councils == ["csl-1"] and "C212 exit 1" in g.evidence_lines[0]
        assert Finding.from_markdown(g.to_markdown()).to_markdown() == g.to_markdown()

    def test_the_prefix_is_the_task_prefix_plus_f(self, tmp_path):
        (tmp_path / ".claude-hooks" / "tasks").mkdir(parents=True)
        (tmp_path / ".claude-hooks" / "tasks" / "config.toml").write_text(
            'prefix = "ch"\n')
        f, _ = Ledger(tmp_path).record("t", CLAIM)
        assert f.id == "chf-1"


class TestRecordAndMatch:

    def test_a_restated_claim_adds_evidence_not_a_file(self, lg):
        a, new_a = lg.record("create then edit refused", CLAIM, paths=[PATH],
                             council="csl-1")
        b, new_b = lg.record(
            "apply_patch create is unread", "Editing a file apply_patch just "
            "created is refused: the editor says it was never read.",
            paths=["executors/apply-patch.ts:430"], evidence="C9 exit 1",
            council="csl-2")
        assert (new_a, new_b) == (True, False) and a.id == b.id
        assert lg.read(a.id).councils == ["csl-1", "csl-2"]
        assert len(lg.list()) == 1

    def test_another_place_or_another_claim_is_a_new_finding(self, lg):
        lg.record("create then edit refused", CLAIM, paths=[PATH])
        _, new = lg.record("grep launders stamps", "grep's read credit "
                           "stamps a file whose content it never returned.",
                           paths=["executors/grep.ts:300"])
        assert new
        _, new = lg.record("same file far away", CLAIM,
                           paths=["sdk/packages/core/src/extensions/tools/"
                                  "executors/apply-patch.ts:900"])
        assert new

    def test_record_never_changes_a_status(self, lg):
        f, _ = lg.record("t", CLAIM, paths=[PATH])
        lg.settle(f.id, "refuted", "C40 shows the edit accepted", by="user")
        g, created = lg.record("t", CLAIM, paths=[PATH], council="csl-9")
        assert not created and g.status == "refuted"


class TestOnlyUserOrAssistantSettle:

    def test_settle_and_reopen(self, lg):
        f, _ = lg.record("t", CLAIM, paths=[PATH])
        g = lg.settle(f.id, "confirmed", "re-run on ac1ee35", by="assistant")
        assert (g.status, g.settled_by, g.decision) == (
            "confirmed", "assistant", "re-run on ac1ee35")
        assert g.settled_at
        h = lg.reopen(f.id, "run 7 shows it accepted", by="user")
        assert h.status == "candidate" and not h.settled_by
        assert "reopened by user (was confirmed)" in h.section("Log")

    @pytest.mark.parametrize("kw,msg", [
        (dict(by="council"), "only user or assistant"),
        (dict(by="researcher"), "only user or assistant"),
        (dict(by="user", reason=" "), "needs a reason"),
        (dict(by="user", status="fixed"), "names the commit"),
        (dict(by="user", status="candidate"), "use reopen"),
    ])
    def test_refused(self, lg, kw, msg):
        f, _ = lg.record("t", CLAIM, paths=[PATH])
        args = {"status": "confirmed", "reason": "because"} | kw
        by = args.pop("by")
        with pytest.raises(ValueError, match=msg):
            lg.settle(f.id, args["status"], args["reason"], by=by)
        assert lg.read(f.id).status == "candidate"

    def test_reopen_needs_a_decider_too(self, lg):
        f, _ = lg.record("t", CLAIM, paths=[PATH])
        lg.settle(f.id, "by_design", "comment at read-ledger.ts:12", by="user")
        with pytest.raises(ValueError):
            lg.reopen(f.id, "x", by="critic")


def test_relevant_ranks_a_shared_place_first(lg):
    a, _ = lg.record("grep launders stamps", "grep's read credit stamps a "
                     "file whose content it never returned.",
                     paths=["executors/grep.ts:300"])
    b, _ = lg.record("create then edit refused", CLAIM, paths=[PATH])
    got = lg.relevant("audit the editor tools", [PATH])
    assert got[0].id == b.id


class TestTools:

    def test_the_catalog_and_a_round_trip(self, lg):
        names = {t["name"] for t in tool_catalog()}
        assert names == {"finding-list", "finding-show", "finding-record",
                         "finding-settle", "finding-reopen", "finding-note"}
        ft = FindingTools(lambda: lg)
        out = ft.call("finding-record", {"title": "t", "claim": CLAIM,
                                         "paths": [PATH]})
        fid = out.split()[1]
        assert out.startswith("recorded ")
        assert "Not done: only user or assistant" in ft.call(
            "finding-settle", {"id": fid, "status": "confirmed",
                               "reason": "x", "by": "council"})
        assert ft.call("finding-settle", {"id": fid, "status": "confirmed",
                                          "reason": "x", "by": "user"}
                       ).startswith("settled ")
        assert "[confirmed M]" in ft.call("finding-list", {"status": "settled"})
        assert "no finding" in ft.call("finding-show", {"id": "zzf-9"})


def test_the_cli(tmp_path):
    lg = Ledger(tmp_path)
    f, _ = lg.record("t", CLAIM, paths=[PATH])

    def cli(*a):
        return subprocess.run([sys.executable, "-m", "consultants.cli",
                               "findings", *a, "--cwd", str(tmp_path)],
                              capture_output=True, text=True)
    r = cli("settle", f.id, "--status", "fixed", "--reason", "x")
    assert r.returncode != 0 and "names the commit" in (r.stderr + r.stdout)
    r = cli("settle", f.id, "--status", "fixed", "--reason", "x",
            "--commit", "abc123", "--by", "assistant")
    assert r.returncode == 0, r.stderr
    assert lg.read(f.id).commit == "abc123"
    assert "[fixed M]" in cli("list").stdout
