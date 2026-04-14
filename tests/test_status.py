"""Tests for scripts/status.py — the unified dashboard view."""

from __future__ import annotations

import datetime as _dt
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "status.py"


def _load():
    spec = importlib.util.spec_from_file_location("status_script", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["status_script"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load()


def _today() -> str:
    return _dt.datetime.utcnow().strftime("%Y-%m-%d")


def _now_iso() -> str:
    return _dt.datetime.utcnow().isoformat() + "Z"


class TestTodaysStats:
    def test_empty_log_dir(self, mod, tmp_path):
        s = mod._todays_stats(tmp_path)
        assert s == {"requests": 0, "warmups_blocked": 0,
                     "warmups_passed": 0, "synthetic": 0}

    def test_counts_today_only(self, mod, tmp_path):
        # Write a mix of today and yesterday.
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / f"{_today()}.jsonl").write_text(
            "\n".join(json.dumps(r) for r in [
                {"ts": _now_iso(), "warmup_blocked": True, "is_warmup": True},
                {"ts": _now_iso(), "is_warmup": True},
                {"ts": _now_iso(), "synthetic": True},
                {"ts": _now_iso()},
            ]) + "\n"
        )
        # Yesterday file must be ignored.
        yesterday = (_dt.datetime.utcnow() - _dt.timedelta(days=1)).strftime("%Y-%m-%d")
        (tmp_path / f"{yesterday}.jsonl").write_text(
            json.dumps({"ts": yesterday + "T12:00:00Z"}) + "\n"
        )
        s = mod._todays_stats(tmp_path)
        assert s == {"requests": 4, "warmups_blocked": 1,
                     "warmups_passed": 1, "synthetic": 1}

    def test_bad_json_lines_ignored(self, mod, tmp_path):
        (tmp_path / f"{_today()}.jsonl").write_text(
            "{broken json\n" + json.dumps({"ts": _now_iso()}) + "\n"
        )
        s = mod._todays_stats(tmp_path)
        assert s["requests"] == 1


class TestReadState:
    def test_missing_returns_none(self, mod, tmp_path):
        assert mod._read_state(tmp_path) is None

    def test_corrupt_returns_none(self, mod, tmp_path):
        (tmp_path / "ratelimit-state.json").write_text("not json")
        assert mod._read_state(tmp_path) is None

    def test_valid_parsed(self, mod, tmp_path):
        (tmp_path / "ratelimit-state.json").write_text(
            json.dumps({"five_hour_utilization": 0.4})
        )
        assert mod._read_state(tmp_path)["five_hour_utilization"] == 0.4


class TestBuildStatus:
    def test_full_shape(self, mod, tmp_path):
        (tmp_path / "ratelimit-state.json").write_text(json.dumps({
            "last_updated": _now_iso(),
            "representative_claim": "five_hour",
            "five_hour_utilization": 0.5,
        }))
        (tmp_path / f"{_today()}.jsonl").write_text(
            json.dumps({"ts": _now_iso(), "warmup_blocked": True,
                        "is_warmup": True}) + "\n"
        )
        st = mod.build_status(tmp_path)
        assert "timestamp" in st
        assert st["proxy"]["listen"] is None or isinstance(
            st["proxy"]["listen"], str
        )
        assert st["rate_limit"]["five_hour_utilization"] == 0.5
        assert st["today"]["warmups_blocked"] == 1


class TestRenderText:
    def test_includes_all_sections(self, mod, tmp_path):
        st = mod.build_status(tmp_path)
        out = mod.render_text(st)
        assert "Proxy config:" in out
        assert "Rate-limit state" in out
        assert "Today (UTC)" in out


class TestCli:
    def test_runs_without_crashing(self, tmp_path):
        out = subprocess.run(
            [sys.executable, str(SCRIPT), "--log-dir", str(tmp_path)],
            capture_output=True, text=True, timeout=5,
        )
        assert out.returncode == 0
        assert "claude-hooks status" in out.stdout

    def test_json_output(self, tmp_path):
        out = subprocess.run(
            [sys.executable, str(SCRIPT),
             "--log-dir", str(tmp_path), "--json"],
            capture_output=True, text=True, timeout=5,
        )
        assert out.returncode == 0
        data = json.loads(out.stdout)
        assert "today" in data
        assert "rate_limit" in data
