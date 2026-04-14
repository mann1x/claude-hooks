"""Tests for scripts/statusline_usage.py — P4 statusline segment."""

from __future__ import annotations

import datetime as _dt
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "statusline_usage.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("statusline_usage", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load_module()


def _state(**kw):
    """Build a state dict with sensible defaults."""
    ts = kw.pop("last_updated", _dt.datetime.utcnow().isoformat() + "Z")
    return {"last_updated": ts, **kw}


class TestFormatSegment:
    def test_both_windows(self, mod):
        s = _state(
            five_hour_utilization=0.42,
            seven_day_utilization=0.18,
            representative_claim="five_hour",
        )
        out = mod.format_segment(s, fmt="plain")
        assert out == "5h 42% · 7d 18%"

    def test_only_five_hour(self, mod):
        s = _state(
            five_hour_utilization=0.65,
            representative_claim="five_hour",
        )
        assert mod.format_segment(s, fmt="plain") == "5h 65%"

    def test_warn_glyph_at_50(self, mod):
        s = _state(
            five_hour_utilization=0.55,
            representative_claim="five_hour",
        )
        assert mod.format_segment(s, fmt="emoji").endswith(" ⚠")
        assert mod.format_segment(s, fmt="ascii").endswith(" !")
        assert "⚠" not in mod.format_segment(s, fmt="plain")

    def test_danger_glyph_at_80(self, mod):
        s = _state(
            five_hour_utilization=0.85,
            representative_claim="five_hour",
        )
        assert mod.format_segment(s, fmt="emoji").endswith(" 🔴")
        assert mod.format_segment(s, fmt="ascii").endswith(" !!")

    def test_no_glyph_below_50(self, mod):
        s = _state(
            five_hour_utilization=0.10,
            representative_claim="five_hour",
        )
        out = mod.format_segment(s, fmt="emoji")
        assert "⚠" not in out
        assert "🔴" not in out

    def test_stale_state_returns_empty(self, mod):
        old = (_dt.datetime.utcnow() - _dt.timedelta(hours=1)).isoformat() + "Z"
        s = _state(
            five_hour_utilization=0.42,
            last_updated=old,
        )
        assert mod.format_segment(s, stale_seconds=600) == ""

    def test_empty_state_returns_empty(self, mod):
        assert mod.format_segment({}) == ""

    def test_missing_timestamp(self, mod):
        s = {"five_hour_utilization": 0.42}
        assert mod.format_segment(s) == ""

    def test_glyph_uses_binding_claim(self, mod):
        # Binding = 7d window, which is > 80%. 5h is low → glyph still fires.
        s = _state(
            five_hour_utilization=0.10,
            seven_day_utilization=0.90,
            representative_claim="seven_day",
        )
        out = mod.format_segment(s, fmt="ascii")
        assert out.endswith(" !!")

    def test_non_numeric_util_skipped(self, mod):
        s = _state(
            five_hour_utilization="high",
            representative_claim="five_hour",
        )
        assert mod.format_segment(s, fmt="plain") == ""


class TestReadState:
    def test_missing_file_returns_empty_dict(self, mod, tmp_path):
        assert mod.read_state(tmp_path / "nope.json") == {}

    def test_corrupt_file_returns_empty_dict(self, mod, tmp_path):
        p = tmp_path / "state.json"
        p.write_text("not json")
        assert mod.read_state(p) == {}

    def test_valid_file_parsed(self, mod, tmp_path):
        p = tmp_path / "state.json"
        p.write_text(json.dumps({"five_hour_utilization": 0.5}))
        assert mod.read_state(p) == {"five_hour_utilization": 0.5}


class TestCliEntryPoint:
    def test_exit_zero_on_missing_file(self, tmp_path):
        out = subprocess.run(
            [sys.executable, str(SCRIPT),
             "--state-file", str(tmp_path / "nope.json")],
            capture_output=True, text=True, timeout=5,
        )
        assert out.returncode == 0
        assert out.stdout == ""

    def test_prints_segment_when_fresh(self, tmp_path):
        p = tmp_path / "state.json"
        p.write_text(json.dumps({
            "last_updated": _dt.datetime.utcnow().isoformat() + "Z",
            "five_hour_utilization": 0.42,
            "representative_claim": "five_hour",
        }))
        out = subprocess.run(
            [sys.executable, str(SCRIPT),
             "--state-file", str(p), "--format", "plain"],
            capture_output=True, text=True, timeout=5,
        )
        assert out.returncode == 0
        assert out.stdout == "5h 42%"

    def test_exit_zero_on_corrupt_file(self, tmp_path):
        p = tmp_path / "state.json"
        p.write_text("not json")
        out = subprocess.run(
            [sys.executable, str(SCRIPT), "--state-file", str(p)],
            capture_output=True, text=True, timeout=5,
        )
        assert out.returncode == 0
        assert out.stdout == ""
