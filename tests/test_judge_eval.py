"""judge_eval: style variants must change the look and never the logic,
and the statistics must mean what the report says they mean."""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from benchmarks.consultants import judge_eval as je  # noqa: E402


def test_c_comments_go_but_strings_that_look_like_them_stay():
    code = 'int f() {\n    // gone\n    char *u = "http://x/*y*/";\n    return 1; /* gone */\n}\n'
    out = je.strip_comments(code, "c")
    assert "gone" not in out
    assert '"http://x/*y*/"' in out
    assert "return 1;" in out


def test_rust_lifetimes_are_not_char_literals():
    code = "fn f<'a>(s: &'a str) -> &'a str {\n    // c\n    let q = '\\'';\n    s\n}\n"
    out = je.strip_comments(code, "rust")
    assert "// c" not in out
    assert "<'a>" in out and "'\\''" in out


def test_python_comments_go_and_the_code_still_compiles():
    code = "def f(x):  # trailing\n    # whole line\n    return '#not'  # c\n"
    out = je.strip_comments(code, "python")
    compile(out, "x", "exec")
    assert "trailing" not in out and "whole line" not in out
    assert "'#not'" in out


def test_raw_strings_are_skipped_not_guessed():
    assert je.strip_comments('auto s = R"(// x)";\n', "cpp") is None


def test_relayout_moves_braces_except_in_go():
    code = "int f() {\n\n    return 1;\n}\n"
    assert je.relayout(code, "c") == "int f()\n{\n    return 1;\n}\n"
    go = "func f() int {\n\n\treturn 1\n}\n"
    assert je.relayout(go, "go") == "func f() int {\n\treturn 1\n}\n"


def test_an_unchanged_variant_is_not_a_variant():
    assert je.make_variant("int f();\n", "c", "no_comments") is None


def test_auc_counts_ties_as_half():
    assert je.auc([5, 1], [True, False]) == 1.0
    assert je.auc([3, 3], [True, False]) == 0.5
    assert je.auc([3], [True]) is None


def test_spearman_is_rank_based():
    assert round(je.spearman([1, 2, 3, 4], [10, 20, 30, 1000]), 6) == 1.0


def test_family_groups_the_tags_the_bias_check_needs():
    assert je.family("glm-5.3-flash:cloud") == je.family("glm-5.3:cloud") == "glm"
    assert je.family("deepseek-v4-pro:cloud") == "deepseek"


def test_every_judge_gets_the_same_samples(monkeypatch):
    class Q:
        task, sandbox_path = "t", "solution.py"

    rows = [{"question_id": f"python-med-{i:02d}", "model": "m",
             "passes_tests": True, "sandbox_dir": ""} for i in range(10)]
    qmap = {r["question_id"]: Q() for r in rows}
    monkeypatch.setattr(je, "_resolve_source", lambda r: ("x = 1  # c\n", ""))
    a = je.build_work(rows, qmap, judge="a", kinds={"retest", "variant"},
                      models=None, retest_n=3, variant_n=4)
    b = je.build_work(rows, qmap, judge="b", kinds={"retest", "variant"},
                      models=None, retest_n=3, variant_n=4)
    def shape(w):
        return [(x["kind"], x["variant"], x["question_id"]) for x in w]

    assert shape(a) == shape(b)
    assert sum(x["kind"] == "retest" for x in a) == 3


def test_synth_settles_paired_member_verdicts_once(monkeypatch, tmp_path):
    import argparse
    import json as _json

    class Q:
        task, sandbox_path = "t", "solution.py"

    rows = [{"question_id": "python-med-01", "model": "m",
             "passes_tests": True, "sandbox_dir": ""}]
    monkeypatch.setattr(je, "_resolve_source", lambda r: ("x = 1\n", ""))
    monkeypatch.setattr(je, "load_raw_trials", lambda p: rows)
    monkeypatch.setattr(je, "_question_map", lambda p: {"python-med-01": Q()})
    calls = []

    class Stub:
        def chat(self, payload, **kw):
            calls.append(payload["model"])
            return {"choices": [{"message": {"content":
                    "SCORE: 3\nCLAIMS: A=HOLDS, B=REFUTED\nok"}}]}
    monkeypatch.setattr(je, "_make_client", lambda *a: Stub())
    base = {"kind": "base", "variant": "", "rep": 0,
            "question_id": "python-med-01", "subject": "m", "usage": {}}
    (tmp_path / "verdicts.jsonl").write_text(
        _json.dumps({**base, "judge": "a", "score": 3.0, "rationale": "x"}) + "\n"
        + _json.dumps({**base, "judge": "b", "score": 5.0, "rationale": "y"}) + "\n"
        + _json.dumps({**base, "judge": "a", "score": 3.0, "kind": "retest",
                       "rep": 1}) + "\n")  # b has no retest: not paired
    args = argparse.Namespace(out=str(tmp_path), members="a,b", synth="s",
                              trials="x", questions_dir="x", kinds="base,retest",
                              ollama_base="x", timeout_s=1.0, concurrency=1)
    je.cmd_synth(args)
    je.cmd_synth(args)  # resumable: nothing left to settle
    vs = je._load_verdicts(tmp_path / "verdicts.jsonl")
    panel = [v for v in vs if v["judge"] == "s#panel=a+b"]
    assert calls == ["s"] and len(panel) == 1
    assert panel[0]["score"] == 3.0 and panel[0]["member_scores"] == [3.0, 5.0]
    assert panel[0]["source"] == "synth" and panel[0]["discordant"] is True
