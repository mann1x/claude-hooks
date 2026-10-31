"""Council runs 10 / 10b (csl-2026-10-26-1729-bb6e, -1931-f8b3): the
engine causes, on the runs' own shapes."""
from __future__ import annotations

from types import SimpleNamespace

from claude_hooks.findings import Ledger
from claude_hooks.tool_registry.shell_provider import ShellSettings, below_floor
from claude_hooks.truncation import unterminated_construct
from consultants import config as cc
from consultants.engine import council, memory
from consultants.engine import evidence as E
from consultants.server import runner, shell_enable as se
from tests.test_evidence_audit import _cmd, _db, _out

WRITE = {"mode": "write", "net": "none", "since": "the ask"}
READ = {"mode": "read", "net": "none", "since": "the ask"}


class TestPreflightRefusesUnderTheDiskFloor:
    """Run 10: preflight passed, then all 26 commands were refused by the
    floor for an hour."""

    def test_a_floor_above_the_free_space_refuses_the_run(self, tmp_path):
        cfg = cc.ConsultantsConfig()
        cfg.tools.shell.free_floor_gib = float(1 << 14)
        err = se.preflight_error(cfg, READ, cwd=str(tmp_path))
        assert err and "under the shell's" in err and "free_floor_pct" in err

    def test_room_above_the_floor_passes_the_disk_check(self, tmp_path):
        s = ShellSettings(mode="read", net="none", free_floor_gib=0.0,
                          free_floor_pct=0.0)
        assert below_floor(str(tmp_path / "not" / "made" / "yet"), s) is None

    def test_the_per_command_check_uses_the_same_rule(self, tmp_path):
        s = ShellSettings(mode="read", net="none",
                          free_floor_gib=float(1 << 14))
        free, floor = below_floor(str(tmp_path), s)
        assert free < floor == float(1 << 14)

    def test_no_shell_never_reads_the_disk(self, tmp_path):
        cfg = cc.ConsultantsConfig()
        cfg.tools.shell.free_floor_gib = float(1 << 14)
        assert se.preflight_error(cfg, None, cwd=str(tmp_path)) is None


class TestAQuotedRunNeverOpensAFence:
    """Run 10b: a probe printed a ```diff block, the omission list quoted
    its first line, the answer held three fences and the truncation
    backstop failed a complete 27.6k answer."""

    PRINTS = ("Replaced line 1, columns 2-3 in /tmp/a.txt\n```diff\n"
              "-1: abcdef\n+1: aXdef\n```\n")

    def _rows(self, tmp_path, printed):
        db = _db(tmp_path, [_cmd(
            "researcher", 0, "bun x vitest run src/zz-cols.test.ts",
            _out(printed + " ✓ zz-cols.test.ts (1 test)\n"
                 "      Tests  1 passed (1)\n"))])
        return E.commands_ran(db)

    def test_the_fence_line_is_skipped(self):
        assert E._quotable("```diff\n-1: a\n+1: b\n```") == "-1: a"

    def test_a_fence_inside_a_line_is_defused(self):
        got = E._quotable("ok -> \"x\\n```diff\\n-1\"")
        assert "```" not in got and "'''diff" in got

    def test_the_enforced_answer_ends_clean(self, tmp_path):
        for printed in ("```diff\n-1: abcdef\n+1: aXdef\n```\n", self.PRINTS):
            rows = self._rows(tmp_path, printed)
            text = "## Defects\n1. one\n\n## Unverified\n\n(none)\n"
            out = E.enforce(text, E.audit_answer(text, rows))
            assert "Dropped by omission" in out
            assert out.count("```") % 2 == 0
            assert unterminated_construct(out) is None
            (tmp_path / "transcript.db").unlink()


class TestTheBriefNeverOwesWhatTheLedgerRuledOut:
    """Run 10b: the brief said "do not re-raise csf-11, csf-12, csf-13 or
    csf-14", and the answer listed all four as not re-verified."""

    def _ledger(self, tmp_path):
        # "cs" gives the run's own id shape (csf-N), which FINDING_TAG reads.
        root = tmp_path / "cs"
        root.mkdir()
        lg = Ledger(root)
        a, _ = lg.record("grep credits a full read", "grep credits the "
                         "whole file", paths=["src/grep.ts:260"])
        lg.settle(a.id, "by_design", "deliberate, see grep.ts:255", by="user")
        b, _ = lg.record("bash rewrites undetected", "a rewrite inside the "
                         "cap goes unseen", paths=["src/bash.ts:423"])
        lg.settle(b.id, "refuted", "the sweep sees it, C12", by="user")
        c, _ = lg.record("apply_patch has no lock", "concurrent patches "
                         "lose an update", paths=["src/apply-patch.ts:381"])
        return a.id, b.id, c.id

    def test_only_refuted_and_by_design_are_ruled_out(self, tmp_path):
        a, b, c = self._ledger(tmp_path)
        assert a.startswith("csf-")
        assert memory.ruled_out_ids(str(tmp_path / "cs")) == {a, b}
        assert memory.ruled_out_ids(str(tmp_path / "other")) == set()
        assert memory.ruled_out_ids("") == set()

    def test_a_forbidden_id_is_not_owed_and_a_live_one_still_is(self, tmp_path):
        a, b, c = self._ledger(tmp_path)
        docs = [SimpleNamespace(text=f"Run {c}. Do not re-raise {a} or {b}.",
                                ts=100.0)]
        rows = [{"ran": True, "ts": 150.0, "command": "bun x vitest run x",
                 "output": "nothing named"}]
        text = "## Answer\n\n## Unverified\n\n(none)\n"
        out = E.brief_owed(text, docs, rows,
                           memory.ruled_out_ids(str(tmp_path / "cs")))
        assert f"{c}: the checkpoint brief named it" in out
        assert f"{a}: the checkpoint brief" not in out
        assert f"{b}: the checkpoint brief" not in out
        assert f"{a}: the checkpoint brief" in E.brief_owed(text, docs, rows)


class TestCrossExaminationOnForShellRuns:
    """Run 10b graded it on a shell run; ``"shell"`` is the default."""

    def test_the_default_follows_the_shell(self):
        cfg = cc.ConsultantsConfig()
        assert cfg.critic.cross_examination == cc.CROSS_EXAM_SHELL
        assert cc.cross_examination_on(cfg, WRITE) is True
        assert cc.cross_examination_on(cfg, None) is False

    def test_on_and_off_force_it(self):
        cfg = cc.ConsultantsConfig()
        cfg.critic.cross_examination = True
        assert cc.cross_examination_on(cfg, None) is True
        cfg.critic.cross_examination = False
        assert cc.cross_examination_on(cfg, WRITE) is False

    def test_the_seed_carries_only_the_resolved_value(self):
        cfg = cc.ConsultantsConfig()
        on = runner._seed_runtime_control(cfg, ["critic"], cross_exam=True)
        off = runner._seed_runtime_control(cfg, ["critic"])
        assert on["cross_examination"] is True and "cross_examination" not in off
        cfg.critic.cross_examination = True
        assert "cross_examination" not in runner._seed_runtime_control(
            cfg, ["critic"], cross_exam=False)

    def test_the_file_round_trips_and_omits_the_default(self, tmp_path):
        cfg = cc.ConsultantsConfig()
        assert "[critic]" not in cc._render(cfg)
        for v, line in ((False, "cross_examination = false"),
                        (True, "cross_examination = true")):
            cfg.critic.cross_examination = v
            assert line in cc._render(cfg)

    def test_the_loader_takes_shell_and_refuses_anything_else(self):
        for raw, want in (("shell", "shell"), (False, False), (True, True),
                          ("maybe", "shell")):
            got = cc._merge_layer(cc.ConsultantsConfig(),
                                  {"critic": {"cross_examination": raw}})
            assert got.critic.cross_examination == want


class TestARunDefectIsRankedWithoutItsFix:
    """Run 10b: csf-5's bytes were printed, no fix ran, and the defect
    went under Unverified with the fix."""

    def test_the_synthesizer_is_told(self):
        assert ("a defect a run reproduced is ranked even when no fix for "
                "it was written or run") in council.SYNTHESIZER_SYSTEM
