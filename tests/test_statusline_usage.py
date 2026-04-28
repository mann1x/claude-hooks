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


class TestPeakMarker:
    """Anthropic peak-hour indicator. Default windows in UTC:
    shoulder = weekdays+weekends 13:00-22:00 (US business),
    peak-of-peak = weekdays only 17:00-21:00 (mid-afternoon ET)."""

    # 2026-04-27 was a Monday (weekday); 2026-05-02 was a Saturday.
    _MONDAY = _dt.datetime(2026, 4, 27)
    _SATURDAY = _dt.datetime(2026, 5, 2)

    def test_off_peak_weekday_morning(self, mod):
        # 06:00 UTC Monday — well before US shoulder.
        t = self._MONDAY.replace(hour=6, minute=0)
        assert mod.peak_marker(now=t, fmt="emoji") == ""
        assert mod.peak_marker(now=t, fmt="ascii") == ""

    def test_shoulder_weekday(self, mod):
        # 14:00 UTC Monday — inside shoulder, outside peak-of-peak.
        t = self._MONDAY.replace(hour=14, minute=30)
        assert mod.peak_marker(now=t, fmt="emoji") == "⏰"
        assert mod.peak_marker(now=t, fmt="ascii") == "[busy]"
        assert mod.peak_marker(now=t, fmt="plain") == ""

    def test_peak_of_peak_weekday(self, mod):
        # 18:30 UTC Monday — inside peak-of-peak window.
        t = self._MONDAY.replace(hour=18, minute=30)
        assert mod.peak_marker(now=t, fmt="emoji") == "🔥"
        assert mod.peak_marker(now=t, fmt="ascii") == "[peak]"
        assert mod.peak_marker(now=t, fmt="plain") == ""

    def test_weekend_during_peak_hours_drops_to_shoulder(self, mod):
        # 18:30 UTC Saturday — inside peak window but Sat → shoulder only.
        t = self._SATURDAY.replace(hour=18, minute=30)
        assert mod.peak_marker(now=t, fmt="emoji") == "⏰"

    def test_weekend_off_peak(self, mod):
        # 06:00 UTC Saturday — outside both windows.
        t = self._SATURDAY.replace(hour=6, minute=0)
        assert mod.peak_marker(now=t, fmt="emoji") == ""

    def test_boundary_inclusive_start_exclusive_end(self, mod):
        # Default shoulder is 13-22; 13:00 fires, 22:00 doesn't.
        t13 = self._MONDAY.replace(hour=13, minute=0)
        t22 = self._MONDAY.replace(hour=22, minute=0)
        assert mod.peak_marker(now=t13, fmt="emoji") == "⏰"
        assert mod.peak_marker(now=t22, fmt="emoji") == ""

    def test_env_override_shoulder(self, mod, monkeypatch):
        # Force shoulder to 06-08 UTC; 14:00 should now be off-peak.
        monkeypatch.setenv("CLAUDE_HOOKS_STATUSLINE_PEAK_HOURS_UTC", "6-8")
        # Push peak-of-peak out of the way too.
        monkeypatch.setenv("CLAUDE_HOOKS_STATUSLINE_PEAKPEAK_HOURS_UTC", "23-24")
        t = self._MONDAY.replace(hour=14, minute=0)
        assert mod.peak_marker(now=t, fmt="emoji") == ""
        t = self._MONDAY.replace(hour=7, minute=0)
        assert mod.peak_marker(now=t, fmt="emoji") == "⏰"

    def test_env_override_garbage_falls_back_to_default(self, mod, monkeypatch):
        monkeypatch.setenv("CLAUDE_HOOKS_STATUSLINE_PEAK_HOURS_UTC", "not-a-range")
        # Default shoulder still applies → 14:00 weekday is shoulder.
        t = self._MONDAY.replace(hour=14, minute=0)
        assert mod.peak_marker(now=t, fmt="emoji") == "⏰"

    def test_format_segment_appends_peak_before_warning(self, mod):
        # 18:30 UTC Monday + 65% util on binding window → "🔥 ⚠" stack.
        t = self._MONDAY.replace(hour=18, minute=30)
        s = _state(
            five_hour_utilization=0.65,
            seven_day_utilization=0.20,
            representative_claim="five_hour",
            last_updated=t.isoformat() + "Z",
        )
        out = mod.format_segment(s, fmt="emoji", now=t)
        assert out.endswith(" 🔥 ⚠")

    def test_format_segment_peak_only_no_warning(self, mod):
        # 18:30 UTC Monday + 10% util → 🔥 alone, no rate warning.
        t = self._MONDAY.replace(hour=18, minute=30)
        s = _state(
            five_hour_utilization=0.10,
            seven_day_utilization=0.20,
            representative_claim="five_hour",
            last_updated=t.isoformat() + "Z",
        )
        out = mod.format_segment(s, fmt="emoji", now=t)
        assert out.endswith(" 🔥")
        assert "⚠" not in out
        assert "🔴" not in out

    def test_format_segment_off_peak_no_marker(self, mod):
        # 06:00 UTC Monday + 10% util → no marker at all.
        t = self._MONDAY.replace(hour=6, minute=0)
        s = _state(
            five_hour_utilization=0.10,
            seven_day_utilization=0.20,
            representative_claim="five_hour",
            last_updated=t.isoformat() + "Z",
        )
        out = mod.format_segment(s, fmt="emoji", now=t)
        assert "🔥" not in out
        assert "⏰" not in out
        # Plain pct only.
        assert out == "5h 10% · 7d 20%"

    def test_format_segment_plain_format_drops_peak(self, mod):
        # Plain format never shows glyphs of any kind.
        t = self._MONDAY.replace(hour=18, minute=30)
        s = _state(
            five_hour_utilization=0.20,
            representative_claim="five_hour",
            last_updated=t.isoformat() + "Z",
        )
        out = mod.format_segment(s, fmt="plain", now=t)
        assert out == "5h 20%"


class TestDefaultFormat:
    """``default_format`` chooses ``ascii`` on Windows (cmd.exe / legacy
    consoles can't render emoji) and ``emoji`` elsewhere. Env var
    ``CLAUDE_HOOKS_STATUSLINE_FORMAT`` overrides regardless of platform."""

    def test_linux_default_is_emoji(self, mod, monkeypatch):
        monkeypatch.delenv("CLAUDE_HOOKS_STATUSLINE_FORMAT", raising=False)
        monkeypatch.setattr(mod.sys, "platform", "linux")
        assert mod.default_format() == "emoji"

    def test_darwin_default_is_emoji(self, mod, monkeypatch):
        monkeypatch.delenv("CLAUDE_HOOKS_STATUSLINE_FORMAT", raising=False)
        monkeypatch.setattr(mod.sys, "platform", "darwin")
        assert mod.default_format() == "emoji"

    def test_windows_default_is_ascii(self, mod, monkeypatch):
        """The bug from pandorum 2026-04-27 — cmd.exe rendered ⏰ ⚠
        as tofu boxes. The default must be ascii on win32."""
        monkeypatch.delenv("CLAUDE_HOOKS_STATUSLINE_FORMAT", raising=False)
        monkeypatch.setattr(mod.sys, "platform", "win32")
        assert mod.default_format() == "ascii"

    def test_env_override_wins_on_windows(self, mod, monkeypatch):
        """Windows Terminal users can opt back into emoji."""
        monkeypatch.setattr(mod.sys, "platform", "win32")
        monkeypatch.setenv("CLAUDE_HOOKS_STATUSLINE_FORMAT", "emoji")
        assert mod.default_format() == "emoji"

    def test_env_override_wins_on_linux(self, mod, monkeypatch):
        monkeypatch.setattr(mod.sys, "platform", "linux")
        monkeypatch.setenv("CLAUDE_HOOKS_STATUSLINE_FORMAT", "ascii")
        assert mod.default_format() == "ascii"

    def test_env_plain_is_honoured(self, mod, monkeypatch):
        monkeypatch.setattr(mod.sys, "platform", "linux")
        monkeypatch.setenv("CLAUDE_HOOKS_STATUSLINE_FORMAT", "plain")
        assert mod.default_format() == "plain"

    def test_invalid_env_falls_back_to_platform_default(self, mod, monkeypatch):
        """A bogus env value must not break the statusline — fall back
        to the platform default (which is what we'd have used anyway)."""
        monkeypatch.setattr(mod.sys, "platform", "win32")
        monkeypatch.setenv("CLAUDE_HOOKS_STATUSLINE_FORMAT", "rainbow")
        assert mod.default_format() == "ascii"

    def test_env_value_is_lowercased_and_stripped(self, mod, monkeypatch):
        monkeypatch.setattr(mod.sys, "platform", "linux")
        monkeypatch.setenv("CLAUDE_HOOKS_STATUSLINE_FORMAT", "  ASCII  ")
        assert mod.default_format() == "ascii"


class TestIsWindowsConsole:
    """``_is_windows_console`` covers more than ``sys.platform == "win32"``
    so emoji still tofu-downgrades from Cygwin / msys2 / Git Bash where
    Python sees a POSIX-shaped world but the underlying console can't
    render the glyphs."""

    def _clear_env(self, monkeypatch):
        for k in ("MSYSTEM", "OS", "CLAUDE_HOOKS_STATUSLINE_FORCE_EMOJI"):
            monkeypatch.delenv(k, raising=False)

    def test_native_win32_detected(self, mod, monkeypatch):
        self._clear_env(monkeypatch)
        monkeypatch.setattr(mod.sys, "platform", "win32")
        monkeypatch.setattr(mod.os, "name", "nt")
        assert mod._is_windows_console() is True

    def test_cygwin_detected(self, mod, monkeypatch):
        self._clear_env(monkeypatch)
        monkeypatch.setattr(mod.sys, "platform", "cygwin")
        monkeypatch.setattr(mod.os, "name", "posix")
        assert mod._is_windows_console() is True

    def test_msys_detected(self, mod, monkeypatch):
        self._clear_env(monkeypatch)
        monkeypatch.setattr(mod.sys, "platform", "msys")
        monkeypatch.setattr(mod.os, "name", "posix")
        assert mod._is_windows_console() is True

    def test_git_bash_detected_via_msystem(self, mod, monkeypatch):
        """Git Bash on Windows: Python reports ``sys.platform == "linux"``
        in some builds, but ``MSYSTEM`` betrays the real environment."""
        self._clear_env(monkeypatch)
        monkeypatch.setattr(mod.sys, "platform", "linux")
        monkeypatch.setattr(mod.os, "name", "posix")
        monkeypatch.setenv("MSYSTEM", "MINGW64")
        assert mod._is_windows_console() is True

    def test_windows_nt_env_detected(self, mod, monkeypatch):
        self._clear_env(monkeypatch)
        monkeypatch.setattr(mod.sys, "platform", "linux")
        monkeypatch.setattr(mod.os, "name", "posix")
        monkeypatch.setenv("OS", "Windows_NT")
        assert mod._is_windows_console() is True

    def test_linux_not_detected(self, mod, monkeypatch):
        self._clear_env(monkeypatch)
        monkeypatch.setattr(mod.sys, "platform", "linux")
        monkeypatch.setattr(mod.os, "name", "posix")
        assert mod._is_windows_console() is False

    def test_darwin_not_detected(self, mod, monkeypatch):
        self._clear_env(monkeypatch)
        monkeypatch.setattr(mod.sys, "platform", "darwin")
        monkeypatch.setattr(mod.os, "name", "posix")
        assert mod._is_windows_console() is False

    def test_wsl_not_treated_as_windows(self, mod, monkeypatch):
        """WSL runs real Linux Python with real Linux fonts — emoji
        renders fine in WSL terminals so we must not downgrade."""
        self._clear_env(monkeypatch)
        monkeypatch.setattr(mod.sys, "platform", "linux")
        monkeypatch.setattr(mod.os, "name", "posix")
        # WSL leaves no ``MSYSTEM`` and ``OS`` typically isn't Windows_NT
        # inside the WSL shell session.
        assert mod._is_windows_console() is False


class TestEffectiveFormatRuntimeDowngrade:
    """The exact bug the user reported on pandorum: their statusLine
    command was wired with a hardcoded ``--format emoji`` (older docs
    suggested it), which bypassed the safe ``default_format`` Windows
    fallback. ``_effective_format`` is the runtime safety net — even if
    the caller passes ``"emoji"`` explicitly, we downgrade on Windows-
    like consoles unless the user opted back in via the FORCE env var."""

    def _clear_env(self, monkeypatch):
        for k in ("MSYSTEM", "OS", "CLAUDE_HOOKS_STATUSLINE_FORCE_EMOJI"):
            monkeypatch.delenv(k, raising=False)

    def test_emoji_downgrades_on_windows(self, mod, monkeypatch):
        self._clear_env(monkeypatch)
        monkeypatch.setattr(mod.sys, "platform", "win32")
        monkeypatch.setattr(mod.os, "name", "nt")
        assert mod._effective_format("emoji") == "ascii"

    def test_emoji_passes_through_on_linux(self, mod, monkeypatch):
        self._clear_env(monkeypatch)
        monkeypatch.setattr(mod.sys, "platform", "linux")
        monkeypatch.setattr(mod.os, "name", "posix")
        assert mod._effective_format("emoji") == "emoji"

    def test_ascii_unchanged_on_windows(self, mod, monkeypatch):
        self._clear_env(monkeypatch)
        monkeypatch.setattr(mod.sys, "platform", "win32")
        monkeypatch.setattr(mod.os, "name", "nt")
        assert mod._effective_format("ascii") == "ascii"

    def test_plain_unchanged_on_windows(self, mod, monkeypatch):
        self._clear_env(monkeypatch)
        monkeypatch.setattr(mod.sys, "platform", "win32")
        monkeypatch.setattr(mod.os, "name", "nt")
        assert mod._effective_format("plain") == "plain"

    def test_force_emoji_env_keeps_emoji_on_windows(self, mod, monkeypatch):
        """Windows Terminal + Cascadia Code users opt back in."""
        self._clear_env(monkeypatch)
        monkeypatch.setattr(mod.sys, "platform", "win32")
        monkeypatch.setattr(mod.os, "name", "nt")
        monkeypatch.setenv("CLAUDE_HOOKS_STATUSLINE_FORCE_EMOJI", "1")
        assert mod._effective_format("emoji") == "emoji"

    def test_force_emoji_accepts_truthy_aliases(self, mod, monkeypatch):
        self._clear_env(monkeypatch)
        monkeypatch.setattr(mod.sys, "platform", "win32")
        monkeypatch.setattr(mod.os, "name", "nt")
        for v in ("true", "yes", "on", "TRUE", "Yes"):
            monkeypatch.setenv("CLAUDE_HOOKS_STATUSLINE_FORCE_EMOJI", v)
            assert mod._effective_format("emoji") == "emoji", v

    def test_force_emoji_falsy_does_not_keep_emoji(self, mod, monkeypatch):
        self._clear_env(monkeypatch)
        monkeypatch.setattr(mod.sys, "platform", "win32")
        monkeypatch.setattr(mod.os, "name", "nt")
        monkeypatch.setenv("CLAUDE_HOOKS_STATUSLINE_FORCE_EMOJI", "0")
        assert mod._effective_format("emoji") == "ascii"

    def test_format_segment_runtime_downgrade_warn_glyph(self, mod, monkeypatch):
        """End-to-end: ``--format emoji`` from a Windows-wired statusline
        command must produce ASCII glyphs, not emoji."""
        self._clear_env(monkeypatch)
        monkeypatch.setattr(mod.sys, "platform", "win32")
        monkeypatch.setattr(mod.os, "name", "nt")
        s = _state(
            five_hour_utilization=0.55,
            representative_claim="five_hour",
        )
        out = mod.format_segment(s, fmt="emoji")
        assert "⚠" not in out
        assert out.endswith(" !")

    def test_peak_marker_runtime_downgrade_shoulder(self, mod, monkeypatch):
        self._clear_env(monkeypatch)
        monkeypatch.setattr(mod.sys, "platform", "win32")
        monkeypatch.setattr(mod.os, "name", "nt")
        t = _dt.datetime(2026, 4, 27, 14, 30)  # Mon 14:30 UTC — shoulder window
        out = mod.peak_marker(now=t, fmt="emoji")
        assert out == "[busy]"

    def test_format_segment_force_emoji_keeps_glyphs_on_windows(
        self, mod, monkeypatch,
    ):
        self._clear_env(monkeypatch)
        monkeypatch.setattr(mod.sys, "platform", "win32")
        monkeypatch.setattr(mod.os, "name", "nt")
        monkeypatch.setenv("CLAUDE_HOOKS_STATUSLINE_FORCE_EMOJI", "1")
        s = _state(
            five_hour_utilization=0.55,
            representative_claim="five_hour",
        )
        out = mod.format_segment(s, fmt="emoji")
        assert out.endswith(" ⚠")


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


class TestReadStateRemote:
    def test_unwraps_dashboard_envelope(self, mod, monkeypatch):
        body = json.dumps({"state": {"five_hour_utilization": 0.42}, "burn": {}}).encode()
        self._install_fake_urlopen(monkeypatch, mod, body)
        assert mod.read_state_remote("http://x/api/ratelimit.json") == {"five_hour_utilization": 0.42}

    def test_accepts_bare_state_payload(self, mod, monkeypatch):
        body = json.dumps({"five_hour_utilization": 0.10}).encode()
        self._install_fake_urlopen(monkeypatch, mod, body)
        assert mod.read_state_remote("http://x/api/ratelimit.json") == {"five_hour_utilization": 0.10}

    def test_network_error_returns_empty(self, mod, monkeypatch):
        import urllib.error
        def raiser(*a, **kw):
            raise urllib.error.URLError("boom")
        monkeypatch.setattr(mod.urllib.request, "urlopen", raiser)
        assert mod.read_state_remote("http://x/api/ratelimit.json") == {}

    def test_bad_json_returns_empty(self, mod, monkeypatch):
        self._install_fake_urlopen(monkeypatch, mod, b"not json")
        assert mod.read_state_remote("http://x/api/ratelimit.json") == {}

    @staticmethod
    def _install_fake_urlopen(monkeypatch, mod, body):
        class _Resp:
            def __init__(self, body): self._body = body
            def read(self): return self._body
            def __enter__(self): return self
            def __exit__(self, *a): return False
        monkeypatch.setattr(mod.urllib.request, "urlopen",
                            lambda req, timeout=None: _Resp(body))


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

    def test_show_blocked_appends_segment(self, tmp_path, mod):
        import datetime as _dt
        p = tmp_path / "state.json"
        p.write_text(json.dumps({
            "last_updated": _dt.datetime.utcnow().isoformat() + "Z",
            "five_hour_utilization": 0.42,
            "representative_claim": "five_hour",
        }))
        today = _dt.datetime.utcnow().strftime("%Y-%m-%d")
        (tmp_path / f"{today}.jsonl").write_text(
            "\n".join(json.dumps({"ts": _dt.datetime.utcnow().isoformat() + "Z",
                                  "warmup_blocked": True})
                     for _ in range(3)) + "\n"
        )
        out = subprocess.run(
            [sys.executable, str(SCRIPT),
             "--state-file", str(p), "--format", "plain",
             "--show-blocked"],
            capture_output=True, text=True, timeout=5,
        )
        assert out.returncode == 0
        assert "blk=3" in out.stdout

    def test_show_blocked_zero_hides_segment(self, tmp_path):
        import datetime as _dt
        p = tmp_path / "state.json"
        p.write_text(json.dumps({
            "last_updated": _dt.datetime.utcnow().isoformat() + "Z",
            "five_hour_utilization": 0.42,
            "representative_claim": "five_hour",
        }))
        # No JSONL file — blocked=0 → no blk= segment
        out = subprocess.run(
            [sys.executable, str(SCRIPT),
             "--state-file", str(p), "--format", "plain",
             "--show-blocked"],
            capture_output=True, text=True, timeout=5,
        )
        assert out.returncode == 0
        assert "blk=" not in out.stdout

    def test_count_blocked_unit(self, mod, tmp_path):
        import datetime as _dt
        today = _dt.datetime.utcnow().strftime("%Y-%m-%d")
        (tmp_path / f"{today}.jsonl").write_text(
            "\n".join(json.dumps({"ts": _dt.datetime.utcnow().isoformat() + "Z",
                                  "warmup_blocked": v})
                     for v in (True, True, False, True)) + "\n"
            + "garbage line\n"
        )
        assert mod.count_blocked_today(tmp_path) == 3

    def test_count_blocked_missing_file(self, mod, tmp_path):
        assert mod.count_blocked_today(tmp_path) == 0

    def test_exit_zero_on_corrupt_file(self, tmp_path):
        p = tmp_path / "state.json"
        p.write_text("not json")
        out = subprocess.run(
            [sys.executable, str(SCRIPT), "--state-file", str(p)],
            capture_output=True, text=True, timeout=5,
        )
        assert out.returncode == 0
        assert out.stdout == ""
