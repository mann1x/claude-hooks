"""bench_costs: sampling arms of one model stay apart, a running arm is
not priced, and every judge_eval record is a paid call."""
from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
for p in (_REPO, _REPO / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import bench_costs as bc  # noqa: E402

U = {"coder": {"model": "glm-5.3-flash:cloud", "prompt": 1000, "completion": 500},
     "judge": {"model": "kimi-k2.6:cloud", "prompt": 1000, "completion": 500}}


def _arm(root: Path, name: str, n: int, expected: int) -> None:
    d = root / name
    d.mkdir(parents=True)
    (d / "sampling.json").write_text("{}")
    (d / "metadata.json").write_text(json.dumps(
        {"questions": [f"q{i}" for i in range(expected)], "models": ["m"]}))
    (d / "trials.jsonl").write_text("".join(json.dumps(
        {"model": "glm-5.3-flash:cloud", "passes_tests": True, "usage": U,
         "timestamp": "2026-09-23T14:00:00Z"}) + "\n" for _ in range(n)))


def test_arms_of_one_model_get_their_own_rows_and_running_ones_are_not_priced(tmp_path):
    _arm(tmp_path, "glm-t07", 3, 3)
    _arm(tmp_path, "glm-t07-pen", 3, 3)
    _arm(tmp_path, "glm-new", 1, 3)
    rows = bc.suite_costs(tmp_path)
    assert len(rows) == 3 and all(r["n"] in (1, 3) for r in rows.values())
    text = bc.render_suite(tmp_path)
    assert "arm `glm-t07`" in text and "arm `glm-t07-pen`" in text
    assert "running: 1/3" in text


def test_every_judge_eval_record_is_priced_failed_and_repaired_included(tmp_path):
    f = tmp_path / "verdicts.jsonl"
    usage = {"judge": {"model": "glm-5.3-flash:cloud", "prompt": 500, "completion": 700}}
    recs = [{"judge": "j", "score": None, "usage": usage, "at": "2026-09-23T14:00:00+00:00"},
            {"judge": "j", "score": 4.0, "repaired": True, "usage": usage,
             "at": "2026-09-23T14:05:00+00:00"}]
    f.write_text("".join(json.dumps(r) + "\n" for r in recs))
    rows = bc.judge_eval_costs([f])
    assert rows["j"]["records"] == 2 and rows["j"]["failed"] == 1
    assert rows["j"]["repaired"] == 1 and rows["j"]["usd"] > 0
