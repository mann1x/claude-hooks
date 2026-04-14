"""Tests for scripts/proxy_stats.py."""

from __future__ import annotations

import datetime as _dt
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "proxy_stats.py"


def _load():
    spec = importlib.util.spec_from_file_location("proxy_stats", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Register in sys.modules BEFORE exec so @dataclass can look up the
    # class's module when building __init__ (dataclasses.py:712).
    sys.modules["proxy_stats"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load()


def _now_iso() -> str:
    return _dt.datetime.utcnow().isoformat() + "Z"


def _write_log(tmp_path: Path, records: list[dict]) -> Path:
    d = tmp_path / "proxy-log"
    d.mkdir()
    day = _dt.datetime.utcnow().strftime("%Y-%m-%d")
    (d / f"{day}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n"
    )
    return d


# ================================================================ #
# Aggregation
# ================================================================ #
class TestAggregate:
    def test_counts_requests_blocks_passed_synthetic(self, mod):
        recs = [
            {"ts": _now_iso(), "status": 200, "warmup_blocked": True,
             "is_warmup": True},
            {"ts": _now_iso(), "status": 200, "warmup_blocked": True,
             "is_warmup": True},
            {"ts": _now_iso(), "status": 200, "is_warmup": True},
            {"ts": _now_iso(), "status": 200, "synthetic": True},
            {"ts": _now_iso(), "status": 200},
            {"ts": _now_iso(), "status": 429},
            {"ts": _now_iso(), "status": 502},
        ]
        days = mod.aggregate(recs)
        (k, s), = days.items()
        assert s.requests == 7
        assert s.warmups_blocked == 2
        assert s.warmups_passed == 1
        assert s.synthetic == 1
        assert s.status_2xx == 5
        assert s.status_4xx == 1
        assert s.status_5xx == 1

    def test_per_model_count(self, mod):
        recs = [
            {"ts": _now_iso(), "status": 200, "model_delivered": "opus-4-6"},
            {"ts": _now_iso(), "status": 200, "model_delivered": "opus-4-6"},
            {"ts": _now_iso(), "status": 200, "model_requested": "haiku-4-5"},
        ]
        days = mod.aggregate(recs)
        s = next(iter(days.values()))
        assert s.by_model["opus-4-6"] == 2
        assert s.by_model["haiku-4-5"] == 1

    def test_sums_usage_tokens(self, mod):
        recs = [
            {"ts": _now_iso(), "status": 200,
             "usage": {"input_tokens": 5, "output_tokens": 10,
                       "cache_read_input_tokens": 100,
                       "cache_creation_input_tokens": 20}},
            {"ts": _now_iso(), "status": 200,
             "usage": {"input_tokens": 3, "output_tokens": 7,
                       "cache_read_input_tokens": 50}},
        ]
        days = mod.aggregate(recs)
        s = next(iter(days.values()))
        assert s.input_tokens == 8
        assert s.output_tokens == 17
        assert s.cache_read_tokens == 150
        assert s.cache_creation_tokens == 20

    def test_missing_timestamps_skipped(self, mod):
        recs = [
            {"status": 200},
            {"ts": "garbage", "status": 200},
            {"ts": _now_iso(), "status": 200},
        ]
        days = mod.aggregate(recs)
        total = sum(s.requests for s in days.values())
        assert total == 1


# ================================================================ #
# CLI
# ================================================================ #
class TestCli:
    def test_empty_dir_prints_zero_line(self, tmp_path):
        out = subprocess.run(
            [sys.executable, str(SCRIPT), "--log-dir", str(tmp_path / "nope")],
            capture_output=True, text=True, timeout=5,
        )
        assert out.returncode == 0
        assert "(no records" in out.stdout

    def test_json_shape(self, tmp_path):
        log_dir = _write_log(tmp_path, [
            {"ts": _now_iso(), "status": 200, "warmup_blocked": True,
             "is_warmup": True},
        ])
        out = subprocess.run(
            [sys.executable, str(SCRIPT), "--log-dir", str(log_dir), "--json"],
            capture_output=True, text=True, timeout=5,
        )
        assert out.returncode == 0
        data = json.loads(out.stdout)
        assert "days" in data
        assert data["days"][0]["warmups_blocked"] == 1

    def test_by_model_section(self, tmp_path):
        log_dir = _write_log(tmp_path, [
            {"ts": _now_iso(), "status": 200, "model_delivered": "opus-4-6"},
            {"ts": _now_iso(), "status": 200, "model_delivered": "opus-4-6"},
            {"ts": _now_iso(), "status": 200, "model_delivered": "haiku"},
        ])
        out = subprocess.run(
            [sys.executable, str(SCRIPT), "--log-dir", str(log_dir),
             "--by-model"],
            capture_output=True, text=True, timeout=5,
        )
        assert out.returncode == 0
        assert "opus-4-6" in out.stdout
        assert "haiku" in out.stdout

    def test_rate_limit_state_rendered(self, tmp_path):
        log_dir = tmp_path / "plog"
        log_dir.mkdir()
        (log_dir / f"{_dt.datetime.utcnow().strftime('%Y-%m-%d')}.jsonl").write_text(
            json.dumps({"ts": _now_iso(), "status": 200}) + "\n"
        )
        (log_dir / "ratelimit-state.json").write_text(json.dumps({
            "last_updated": _now_iso(),
            "representative_claim": "five_hour",
            "five_hour_utilization": 0.42,
        }))
        out = subprocess.run(
            [sys.executable, str(SCRIPT), "--log-dir", str(log_dir)],
            capture_output=True, text=True, timeout=5,
        )
        assert out.returncode == 0
        assert "Current rate-limit state" in out.stdout
        assert "42.00%" in out.stdout

    def test_since_until_window(self, tmp_path):
        old = (_dt.datetime.utcnow() - _dt.timedelta(days=30)).isoformat() + "Z"
        new = _now_iso()
        log_dir = _write_log(tmp_path, [
            {"ts": old, "status": 200},
            {"ts": new, "status": 200},
        ])
        today = _dt.datetime.utcnow().strftime("%Y-%m-%d")
        out = subprocess.run(
            [sys.executable, str(SCRIPT),
             "--log-dir", str(log_dir), "--since", today, "--json"],
            capture_output=True, text=True, timeout=5,
        )
        data = json.loads(out.stdout)
        # Only the fresh record should be in-window.
        total = sum(d["requests"] for d in data["days"])
        assert total == 1
