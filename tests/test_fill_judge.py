"""fill_judge: re-judge only compiled trials whose judge failed, in place."""
from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from benchmarks.consultants import fill_judge as fj  # noqa: E402


def test_fills_only_the_holes_and_records_the_call(tmp_path, monkeypatch):
    class Q:
        id, task, sandbox_path = "c-med-01", "t", "solution.c"
    sb = tmp_path / "sb"
    out = sb / ".claude-hooks" / "consultants" / "bench" / "coder-out"
    out.mkdir(parents=True)
    (out / "solution.c").write_text("int main(){return 0;}\n")
    rows = [
        {"question_id": "c-med-01", "compiles": True, "quality_score": None,
         "sandbox_dir": str(sb), "usage": {"judge": {"model": "k", "calls": 1,
                                                    "prompt": 0, "completion": 0,
                                                    "failed": 1}}},
        {"question_id": "c-med-01", "compiles": True, "quality_score": 4.0,
         "sandbox_dir": str(sb)},
        {"question_id": "c-med-01", "compiles": False, "quality_score": None,
         "sandbox_dir": str(sb)},
    ]
    (tmp_path / "trials.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (tmp_path / "metadata.json").write_text(json.dumps({"judge_model": "k"}))

    class Stub:
        def chat(self, payload, **kw):
            return {"choices": [{"message": {"content": "SCORE: 3\nok"}}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 2}}
    import claude_hooks.get_advice.chat_client as cc
    monkeypatch.setattr(cc, "make_agent_chat_client", lambda *a, **k: Stub())
    monkeypatch.setattr(fj, "load_questions", lambda d: [Q()])
    assert fj.main(["--run-dir", str(tmp_path), "--questions-dir", "x"]) == 0
    got = [json.loads(l) for l in (tmp_path / "trials.jsonl").read_text().splitlines()]
    assert [g["quality_score"] for g in got] == [3.0, 4.0, None]
    assert got[0]["quality_filled"] and got[0]["usage"]["judge"]["calls"] == 2
    assert "quality_filled" not in got[1]
