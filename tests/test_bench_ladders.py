"""bench_ladders: only current, priced models get a rung, and an older
run is calibrated onto the reference through the models both measured."""
from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
for p in (_REPO, _REPO / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import bench_ladders as bl  # noqa: E402
from benchmarks.consultants import pricing  # noqa: E402


def _run(root: Path, name: str, models: dict) -> Path:
    """models: tag -> (score, passes, wall_s, completion tokens)."""
    d = root / name
    for tag, (score, passes, wall, comp) in models.items():
        md = d / tag.replace(":", "_")
        md.mkdir(parents=True)
        rows = [{"model": tag, "question_id": f"python-med-{i:02d}",
                 "quality_score": score, "passes_tests": passes,
                 "wall_s": wall, "tokens_prompt": 1000,
                 "tokens_completion": comp} for i in range(4)]
        (md / "trials.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows))
    return d


def test_retiring_and_unpriced_models_get_no_rung(tmp_path):
    ref = _run(tmp_path, "ref", {
        "glm-5.3-flash:cloud": (4, True, 2.0, 500),
        "deepseek-v4-flash:cloud": (5, True, 1.0, 500),  # retiring
        "qwen3.5:cloud": (5, True, 1.0, 500),            # unpriced
    })
    rows, _ = bl.combine([ref])
    assert [r["model"] for r in rows] == ["glm-5.3-flash:cloud"]
    assert "deepseek-v4-flash" in pricing.RETIRING


def test_an_older_run_is_scaled_by_the_shared_models(tmp_path):
    ref = _run(tmp_path, "ref", {"glm-5.3:cloud": (4, True, 2.0, 500)})
    old = _run(tmp_path, "old", {
        "glm-5.3:cloud": (2, True, 20.0, 500),   # anchor: Q ×2, time ÷10
        "kimi-k2.6:cloud": (2, True, 30.0, 500),
    })
    rows, cals = bl.combine([ref, old])
    by = {r["model"]: r for r in rows}
    assert cals[0]["anchors"] == ["glm-5.3:cloud"]
    assert round(cals[0]["q"], 6) == 2.0 and round(cals[0]["t"], 6) == 0.1
    assert by["glm-5.3:cloud"]["calibrated"] is False  # reference wins
    kimi = by["kimi-k2.6:cloud"]
    assert kimi["calibrated"] and round(kimi["Q"], 6) == 0.8
    assert round(kimi["median_s"], 6) == 3.0


def test_a_run_with_no_shared_model_is_not_ranked(tmp_path):
    ref = _run(tmp_path, "ref", {"glm-5.3:cloud": (4, True, 2.0, 500)})
    old = _run(tmp_path, "old", {"kimi-k2.6:cloud": (4, True, 30.0, 500)})
    rows, cals = bl.combine([ref, old])
    assert [r["model"] for r in rows] == ["glm-5.3:cloud"]
    assert cals[0]["q"] is None


def test_failing_trials_count_zero_quality(tmp_path):
    ref = _run(tmp_path, "ref", {"glm-5.3:cloud": (5, False, 2.0, 500)})
    rows, _ = bl.combine([ref])
    assert rows[0]["Q"] == 0.0
    assert rows[0]["usd_per_q"] == float("inf")
    assert "⚠" in bl.render(rows, [])
