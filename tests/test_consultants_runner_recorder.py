"""Phase 3 tests: the runner instantiates a MessageRecorder pointed at
``<cwd>/.claude-hooks/consultants/<sid>/transcript.db`` and finalizes
it after the graph drains.

These tests target the helpers in ``consultants.server.runner``
(``_build_recorder`` + ``_finalize_recorder``) directly because the
heavy graph-runner integration would require the consultants conda
env (LangChain). The helpers carry the all of the SQLite-side risk;
the runner just calls them at the right points (covered by an
end-to-end run on solidpc post-deploy).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from consultants import config as cc
from consultants.engine import storage
from consultants.server import runner as runner_mod


def _cfg(effort: str = "medium", topology: str = "council") -> cc.ConsultantsConfig:
    return cc.ConsultantsConfig(topology=topology, effort=effort)


class TestBuildRecorder:
    def test_creates_db_at_expected_path(self, tmp_path):
        sid = "csl-2026-05-07-aaaa"
        rec = runner_mod._build_recorder(
            sid=sid, cwd=str(tmp_path), question="why?",
            cfg=_cfg(),
            models={"planner": "kimi-k2.6:cloud",
                    "synthesizer": "kimi-k2.6:cloud"},
            parent_sid=None,
        )
        assert rec is not None
        try:
            expected = (tmp_path / ".claude-hooks" / "consultants" / sid
                        / storage.TRANSCRIPT_DB_FILENAME)
            assert expected.exists()
            assert rec.db_path == expected
        finally:
            rec.close()

    def test_meta_row_carries_parent_sid_and_models(self, tmp_path):
        sid = "csl-2026-05-07-bbbb"
        rec = runner_mod._build_recorder(
            sid=sid, cwd=str(tmp_path), question="why?",
            cfg=_cfg(effort="high", topology="council"),
            models={"researcher": "qwen3.5:cloud",
                    "synthesizer": "kimi-k2.6:cloud"},
            parent_sid="csl-parent-9999",
        )
        try:
            conn = sqlite3.connect(str(rec.db_path))
            row = conn.execute(
                "SELECT effort, topology, parent_sid, models_json, status "
                "FROM meta"
            ).fetchone()
            conn.close()
            assert row[0] == "high"
            assert row[1] == "council"
            assert row[2] == "csl-parent-9999"
            assert "qwen3.5:cloud" in row[3]
            assert row[4] == "running"
        finally:
            rec.close()

    def test_returns_none_on_construction_failure(self, tmp_path, monkeypatch):
        # Force the recorder constructor to blow up; the helper should
        # log + swallow + return None so the consultation still runs.
        from consultants.engine import recorder as rec_mod

        def _explode(*a, **kw):
            raise OSError("read-only filesystem")

        monkeypatch.setattr(rec_mod, "MessageRecorder", _explode)
        out = runner_mod._build_recorder(
            sid="csl-x", cwd=str(tmp_path), question="q", cfg=_cfg(),
            models={}, parent_sid=None,
        )
        assert out is None


class TestFinalizeRecorder:
    def test_none_recorder_is_noop(self):
        # Must accept None silently — the construction-failed path
        # leaves the runner with recorder=None and we still finalize
        # at every exit point.
        runner_mod._finalize_recorder(None, status="completed")

    def test_finalize_flips_meta_status(self, tmp_path):
        rec = runner_mod._build_recorder(
            sid="csl-final", cwd=str(tmp_path), question="q",
            cfg=_cfg(),
            models={"planner": "m"}, parent_sid=None,
        )
        # Record one event so the file isn't bare.
        rec.record_llm(role="planner", model="m",
                       request={"x": 1}, response={"y": 2},
                       prompt_tokens=10, completion_tokens=4)
        runner_mod._finalize_recorder(
            rec, status="completed",
        )
        # The recorder is now closed — re-open read-only for the
        # assertion.
        conn = sqlite3.connect(str(rec.db_path))
        status, finished_at, err = conn.execute(
            "SELECT status, finished_at, error FROM meta"
        ).fetchone()
        n_events = conn.execute(
            "SELECT COUNT(*) FROM events"
        ).fetchone()[0]
        conn.close()
        assert status == "completed"
        assert finished_at is not None
        assert err is None
        assert n_events == 1

    def test_finalize_records_failure_with_error(self, tmp_path):
        rec = runner_mod._build_recorder(
            sid="csl-failed", cwd=str(tmp_path), question="q",
            cfg=_cfg(),
            models={"planner": "m"}, parent_sid=None,
        )
        runner_mod._finalize_recorder(
            rec, status="failed", error="researcher tombstoned",
        )
        conn = sqlite3.connect(str(rec.db_path))
        status, err = conn.execute(
            "SELECT status, error FROM meta"
        ).fetchone()
        conn.close()
        assert status == "failed"
        assert err == "researcher tombstoned"

    def test_finalize_swallows_misbehaving_recorder(self):
        # If finalize/close raise, we log+continue. Use a stub.
        class Bad:
            def finalize(self, **kw):
                raise RuntimeError("disk gone")
            def close(self):
                raise RuntimeError("connection refused")
        runner_mod._finalize_recorder(Bad(), status="completed")


class TestEndToEndPath:
    """End-to-end through the helpers: simulate a tiny consultation
    flow (build -> a few events -> finalize) and assert the resulting
    file is queryable in the shape Phase 4 will rely on."""

    def test_full_flow_produces_queryable_db(self, tmp_path):
        sid = "csl-e2e-001"
        rec = runner_mod._build_recorder(
            sid=sid, cwd=str(tmp_path), question="why?",
            cfg=_cfg(effort="medium"),
            models={"planner": "kimi-k2.6:cloud",
                    "researcher": "qwen3.5:cloud",
                    "synthesizer": "kimi-k2.6:cloud"},
            parent_sid=None,
        )
        # Planner round
        rec.record_node(role="planner", kind="node_enter")
        rec.record_llm(
            role="planner", round=1, model="kimi-k2.6:cloud",
            request={"messages": [{"role": "user", "content": "Q"}]},
            response={"choices": [{"message": {"content": "1. find foo"}}]},
            prompt_tokens=42, completion_tokens=11, duration_ms=200,
        )
        rec.record_node(role="planner", kind="node_exit", duration_ms=205)
        # Researcher with one tool call + lane fan-out
        rec.record_node(role="researcher", kind="node_enter",
                        round=1, lane_idx=0)
        rec.record_tool(
            role="researcher", round=1, lane_idx=0,
            tool="read_file", args='{"path": "foo.py"}',
            output="def foo():\n    pass\n",
            duration_ms=12,
        )
        rec.record_llm(
            role="researcher", round=1, lane_idx=0,
            model="qwen3.5:cloud",
            request={"x": 1}, response={"y": 2},
            prompt_tokens=300, completion_tokens=80,
        )
        rec.record_node(role="researcher", kind="node_exit",
                        round=1, lane_idx=0, duration_ms=900)
        # Synthesizer round
        rec.record_llm(
            role="synthesizer", round=1, model="kimi-k2.6:cloud",
            request={"a": 1}, response={"b": 2},
            prompt_tokens=500, completion_tokens=200,
        )
        runner_mod._finalize_recorder(
            rec, status="completed",
        )

        conn = sqlite3.connect(str(rec.db_path))
        # Sums per role (Phase 4 will query in the same shape).
        rows = dict(conn.execute(
            "SELECT role, COUNT(*) FROM events WHERE kind='llm_call' "
            "GROUP BY role"
        ).fetchall())
        tool_count = conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind='tool_call'"
        ).fetchone()[0]
        # Schema reconstruction query — picks the most recent
        # llm_call per role (Phase 4 uses ts ordering for full thread).
        researcher_last = conn.execute(
            "SELECT model, prompt_tokens, completion_tokens "
            "FROM events WHERE role='researcher' AND kind='llm_call' "
            "ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        conn.close()

        assert rows == {"planner": 1, "researcher": 1, "synthesizer": 1}
        assert tool_count == 1
        assert researcher_last[0] == "qwen3.5:cloud"
        assert researcher_last[1] == 300
        assert researcher_last[2] == 80
