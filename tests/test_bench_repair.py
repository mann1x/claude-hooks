"""repair: re-ask exactly the holes an outage left, append-only, and
never rewrite a coder run that is still appending."""
from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from benchmarks.consultants import fill_judge, judge_eval as je, repair  # noqa: E402


class Q:
    task, sandbox_path = "t", "solution.py"


def _setup(tmp_path, monkeypatch):
    rows = [{"question_id": f"python-med-0{i}", "model": "m",
             "passes_tests": True, "sandbox_dir": ""} for i in range(3)]
    monkeypatch.setattr(je, "load_raw_trials", lambda p: rows)
    monkeypatch.setattr(je, "_question_map", lambda p: {r["question_id"]: Q() for r in rows})
    monkeypatch.setattr(je, "_resolve_source", lambda r: ("x = 1\n", ""))
    out = tmp_path / "je"
    out.mkdir()
    recs = []
    for i, score in enumerate((4.0, None, 3.0)):
        key = je.verdict_key("base", "", f"python-med-0{i}", "m", "j", 0)
        recs.append({"key": key, "kind": "base", "variant": "", "rep": 0,
                     "question_id": f"python-med-0{i}", "subject": "m",
                     "judge": "j", "judge_model": "j", "options": {},
                     "score": score, "error": None if score else "502"})
    (out / "verdicts.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))
    return out


def test_only_the_hole_is_reasked_and_the_repair_supersedes_it(tmp_path, monkeypatch):
    out = _setup(tmp_path, monkeypatch)
    asked = []

    def judge_one(client, item):
        asked.append(item["question_id"])
        return {**{k: v for k, v in item.items() if k not in ("messages", "wire_options")},
                "score": 5.0, "error": None, "usage": {}}
    monkeypatch.setattr(je, "_judge_one", judge_one)
    monkeypatch.setattr(je, "_make_client", lambda *a: object())
    assert repair.repair_verdicts(out, Path("t"), Path("q"), base="x", timeout_s=1) == 0
    assert asked == ["python-med-01"]
    lines = (out / "verdicts.jsonl").read_text().splitlines()
    assert len(lines) == 4  # appended, nothing rewritten
    assert repair.latest([json.loads(l) for l in lines])[
        je.verdict_key("base", "", "python-med-01", "m", "j", 0)]["score"] == 5.0


def test_rounds_pause_and_report_holes_left(tmp_path, monkeypatch):
    out = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(je, "_make_client", lambda *a: object())
    monkeypatch.setattr(je, "_judge_one", lambda c, item: {
        **{k: v for k, v in item.items() if k not in ("messages", "wire_options")},
        "score": None, "error": "still down", "usage": {}})
    slept = []
    monkeypatch.setattr(repair.time, "sleep", slept.append)
    rc = repair.main(["--questions-dir", "q", "--judge-eval", f"{out}=t",
                      "--rounds", "3", "--pause-s", "7"])
    assert rc == 1 and slept == [7, 7]


def test_an_incomplete_coder_run_is_not_rewritten(tmp_path):
    (tmp_path / "metadata.json").write_text(json.dumps(
        {"questions": ["a", "b"], "models": ["m"], "judge_model": "k"}))
    (tmp_path / "trials.jsonl").write_text(json.dumps(
        {"question_id": "a", "compiles": True, "quality_score": None}) + "\n")
    assert not fill_judge.is_complete(tmp_path)
    assert repair.repair_coder(tmp_path, Path("q"), base="x", timeout_s=1) == -1
