"""judge_panel: two peer judges, a synthesizer that settles the score and
checks the claims, and the fallbacks when a call fails."""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from benchmarks.consultants import judge_panel as jp  # noqa: E402

A, B = "glm-5.3-flash:cloud", "deepseek-v4.1-flash:cloud"


def _key(swap: bool) -> str:
    return next(k for k in (f"q{i}" for i in range(64)) if jp.swapped(k) == swap)


def test_one_score_stands_and_no_synth_is_called():
    def boom(msgs):
        raise AssertionError("synth must not run")
    out = jp.resolve([(A, 4.0, "ok"), (B, None, "raised")], key="k",
                     synth=boom, task="t", code="c")
    assert (out["score"], out["source"]) == (4.0, "single")
    assert jp.resolve([(A, None, ""), (B, None, "")], key="k", synth=boom,
                      task="t", code="c")["source"] == "none"


def test_the_synth_settles_and_claims_map_back_through_the_swap():
    for swap in (False, True):
        seen = []

        def synth(msgs):
            seen.append(msgs[1]["content"])
            return "SCORE: 2\nCLAIMS: A=HOLDS, B=REFUTED\nOff by one on empty input."
        out = jp.resolve([(A, 2.0, "empty input crashes"), (B, 5.0, "clean")],
                         key=_key(swap), synth=synth, task="t", code="c")
        assert out["score"] == 2.0 and out["source"] == "synth"
        assert out["discordant"] is True
        first_shown = B if swap else A
        other = A if swap else B
        assert out["claims"] == {first_shown: "HOLDS", other: "REFUTED"}
        # The synth never sees model names.
        assert "glm" not in seen[0] and "deepseek" not in seen[0]
        assert seen[0].index("REVIEW A: SCORE " + ("5" if swap else "2")) >= 0


def test_a_failed_synth_falls_back_to_agreement_or_the_mean():
    def boom(msgs):
        raise TimeoutError("slow")
    agreed = jp.resolve([(A, 4.0, "x"), (B, 4.0, "y")], key="k", synth=boom,
                        task="t", code="c")
    assert (agreed["score"], agreed["source"]) == (4.0, "agreed")
    assert "TimeoutError" in agreed["synth_error"]
    split = jp.resolve([(A, 3.0, "x"), (B, 5.0, "y")], key="k",
                       synth=lambda m: "no score here", task="t", code="c")
    assert (split["score"], split["source"]) == (4.0, "mean")
    assert "unparseable" in split["synth_error"]


def test_silence_is_retried_once():
    replies = iter(["", "SCORE: 3\nCLAIMS: A=HOLDS, B=HOLDS\nFine."])
    out = jp.resolve([(A, 3.0, "x"), (B, 4.0, "y")], key="k",
                     synth=lambda m: next(replies), task="t", code="c")
    assert out["score"] == 3.0


def test_the_label_files_a_panel_under_its_synth_family():
    from benchmarks.consultants.judge_eval import family
    lab = jp.label([A, B], B)
    assert lab == f"{B}#panel={A}+{B}"
    assert family(lab) == family(B)


def test_coder_bench_records_every_panel_call_under_its_own_role(tmp_path):
    from benchmarks.consultants import coder_bench as cb

    class Stub:
        def __init__(self, text):
            self.text = text

        def chat(self, payload, **kw):
            return {"choices": [{"message": {"content": self.text}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    (tmp_path / "a.py").write_text("def f(): return 1\n")
    usage: dict = {}
    out = cb._panel_trial_quality(
        panel=[(A, Stub("SCORE: 4\nfine")), (B, Stub("SCORE: 2\nbug"))],
        synth=(B, Stub("SCORE: 4\nCLAIMS: A=HOLDS, B=REFUTED\nno bug")),
        usage=usage, task="t", sandbox=tmp_path, sandbox_path="a.py",
        key="q|m")
    assert out["score"] == 4.0 and out["source"] == "synth"
    assert set(usage) == {"judge_a", "judge_b", "judge_synth"}
    assert usage["judge_b"]["model"] == B and usage["judge_synth"]["calls"] == 1
    assert [v["score"] for v in out["verdicts"]] == [4.0, 2.0]
