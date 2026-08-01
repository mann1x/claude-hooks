"""Phase 7 tests: ``claude-consultants show --raw <sid>`` dumps
the structured events table from transcript.db, with optional
``--filter`` clauses and ``--limit``.

These tests target ``cmd_show`` directly with a fake-args object,
avoiding the FastAPI/uvicorn integration path used by
test_consultants_cli.py — that file is skipped in the main test
env (uvicorn is only installed in the consultants conda env)."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import pytest

from consultants import cli
from consultants.engine import storage
from consultants.engine.recorder import MessageRecorder, RecorderMeta


def _seed_db(cwd: Path, sid: str) -> Path:
    sdir = cwd / ".claude-hooks" / "consultants" / sid
    sdir.mkdir(parents=True, exist_ok=True)
    db = sdir / storage.TRANSCRIPT_DB_FILENAME
    meta = RecorderMeta(
        sid=sid, cwd=str(cwd),
        question="why?", effort="medium", topology="council",
        models={"planner": "kimi-k2.6:cloud",
                "researcher": "qwen3.5:cloud",
                "synthesizer": "kimi-k2.6:cloud"},
    )
    rec = MessageRecorder(db, meta=meta)
    try:
        # Planner
        rec.record_node(role="planner", kind="node_enter")
        rec.record_llm(
            role="planner", round=1, model="kimi-k2.6:cloud",
            request={"messages": []},
            response={"choices": [{"message": {"content": "1. find foo"}}]},
            prompt_tokens=42, completion_tokens=11, duration_ms=200,
        )
        rec.record_node(role="planner", kind="node_exit",
                        duration_ms=205)
        # Researcher fan-out (2 lanes)
        for lane in (0, 1):
            rec.record_node(role="researcher", kind="node_enter",
                            round=1, lane_idx=lane)
            rec.record_tool(
                role="researcher", round=1, lane_idx=lane,
                tool="read_file",
                args=f'{{"path": "lane{lane}.py"}}',
                output=f"# lane {lane} content\n",
                duration_ms=10,
            )
            rec.record_llm(
                role="researcher", round=1, lane_idx=lane,
                model="qwen3.5:cloud",
                request={"x": 1}, response={"y": 2},
                prompt_tokens=300, completion_tokens=80,
            )
            rec.record_node(role="researcher", kind="node_exit",
                            round=1, lane_idx=lane, duration_ms=900)
        # Synthesizer
        rec.record_llm(
            role="synthesizer", round=1, model="kimi-k2.6:cloud",
            request={"a": 1}, response={"b": 2},
            prompt_tokens=500, completion_tokens=200,
        )
        rec.finalize(status="completed")
    finally:
        rec.close()
    return db


def _run_show_raw(cwd: Path, sid: str,
                  filters: list[str] | None = None,
                  limit: int = 0) -> list[dict]:
    args = SimpleNamespace(
        sid=sid, cwd=str(cwd),
        raw=True, filter=filters, limit=limit,
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.cmd_show(args, base="http://unused")
    assert rc == 0
    rows = []
    for line in buf.getvalue().splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


class TestShowRaw:
    def test_dumps_all_events_one_per_line(self, tmp_path):
        sid = "csl-raw-001"
        _seed_db(tmp_path, sid)
        rows = _run_show_raw(tmp_path, sid)
        # 1 planner llm + 2 researcher llm + 1 synth llm = 4 llm rows
        # 2 researcher tool calls = 2 tool rows
        # planner enter+exit + 2 researcher enter+exit = 6 node rows
        # total: 12
        assert len(rows) == 12
        kinds = {r["kind"] for r in rows}
        assert kinds == {"node_enter", "node_exit", "llm_call", "tool_call"}
        # Sorted by ts.
        prev = 0.0
        for r in rows:
            assert r["ts"] >= prev
            prev = r["ts"]

    def test_filter_by_role(self, tmp_path):
        sid = "csl-raw-002"
        _seed_db(tmp_path, sid)
        rows = _run_show_raw(tmp_path, sid,
                             filters=["role=researcher"])
        assert len(rows) >= 4  # 2 enter + 2 exit + 2 llm + 2 tool
        for r in rows:
            assert r["role"] == "researcher"

    def test_filter_by_kind(self, tmp_path):
        sid = "csl-raw-003"
        _seed_db(tmp_path, sid)
        rows = _run_show_raw(tmp_path, sid,
                             filters=["kind=tool_call"])
        assert len(rows) == 2
        for r in rows:
            assert r["kind"] == "tool_call"
            assert r["tool"] == "read_file"

    def test_filter_combines_with_AND(self, tmp_path):
        sid = "csl-raw-004"
        _seed_db(tmp_path, sid)
        rows = _run_show_raw(
            tmp_path, sid,
            filters=["role=researcher", "kind=llm_call"],
        )
        assert len(rows) == 2
        assert all(r["role"] == "researcher" and r["kind"] == "llm_call"
                   for r in rows)

    def test_filter_lane_idx_coerces_to_int(self, tmp_path):
        sid = "csl-raw-005"
        _seed_db(tmp_path, sid)
        rows = _run_show_raw(tmp_path, sid,
                             filters=["lane_idx=1"])
        assert len(rows) > 0
        for r in rows:
            assert r["lane_idx"] == 1

    def test_limit_caps_rows(self, tmp_path):
        sid = "csl-raw-006"
        _seed_db(tmp_path, sid)
        rows = _run_show_raw(tmp_path, sid, limit=3)
        assert len(rows) == 3

    def test_unknown_filter_column_rejected(self, tmp_path):
        sid = "csl-raw-007"
        _seed_db(tmp_path, sid)
        with pytest.raises(cli.CLIError) as ei:
            _run_show_raw(tmp_path, sid,
                          filters=["DROP TABLE events;=1"])
        assert "unknown" in str(ei.value).lower()

    def test_filter_without_equals_rejected(self, tmp_path):
        sid = "csl-raw-008"
        _seed_db(tmp_path, sid)
        with pytest.raises(cli.CLIError) as ei:
            _run_show_raw(tmp_path, sid, filters=["roleresearcher"])
        assert "key=value" in str(ei.value)

    def test_filter_int_with_non_integer_value_rejected(self, tmp_path):
        sid = "csl-raw-009"
        _seed_db(tmp_path, sid)
        with pytest.raises(cli.CLIError) as ei:
            _run_show_raw(tmp_path, sid, filters=["round=banana"])
        assert "integer" in str(ei.value).lower()

    def test_missing_db_errors_clearly(self, tmp_path):
        # No transcript.db seeded — error message points the user at
        # the v1.0 vs v1.1 distinction.
        with pytest.raises(cli.CLIError) as ei:
            _run_show_raw(tmp_path, "csl-no-db")
        msg = str(ei.value)
        assert "no transcript.db" in msg
        assert "v1.0" in msg

    def test_default_show_still_renders_summary(self, tmp_path):
        # Without --raw, cmd_show renders summary.md as before. Seed
        # both summary.md and metadata.json so the legacy path works.
        sid = "csl-classic"
        sdir = tmp_path / ".claude-hooks" / "consultants" / sid
        sdir.mkdir(parents=True)
        (sdir / storage.SUMMARY_FILENAME).write_text("# my summary\nbody")
        (sdir / storage.METADATA_FILENAME).write_text(
            json.dumps({"models": {"x": "y"}})
        )
        args = SimpleNamespace(
            sid=sid, cwd=str(tmp_path),
            raw=False, filter=None, limit=0,
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.cmd_show(args, base="http://unused")
        assert rc == 0
        out = json.loads(buf.getvalue())
        assert "summary_markdown" in out
        assert out["summary_markdown"].startswith("# my summary")
