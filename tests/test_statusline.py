"""Tests for claude_hooks/statusline.py and scripts/statusline_compose.py.

The status line is the only surface Claude Code refreshes while a
session is idle, and it costs no tokens: usage comes from the payload's
own ``rate_limits``, and unread mail is a cached per-session count.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from claude_hooks import statusline as sl

REPO = Path(__file__).resolve().parent.parent
COMPOSE = REPO / "scripts" / "statusline_compose.py"


class TestNativeState:
    def test_both_windows_as_fractions(self):
        now = _dt.datetime(2026, 9, 27, 11, 0, 0)
        st = sl.native_state({"rate_limits": {
            "five_hour": {"used_percentage": 19, "resets_at": 1},
            "seven_day": {"used_percentage": 90, "resets_at": 2},
        }}, now=now)
        assert st["five_hour_utilization"] == pytest.approx(0.19)
        assert st["seven_day_utilization"] == pytest.approx(0.90)
        assert st["last_updated"] == "2026-09-27T11:00:00Z"

    def test_binding_window_is_the_fuller_one(self):
        def claim(five, seven):
            return sl.native_state({"rate_limits": {
                "five_hour": {"used_percentage": five},
                "seven_day": {"used_percentage": seven}}})["representative_claim"]
        assert claim(19, 90) == "seven_day"
        assert claim(85, 40) == "five_hour"
        assert claim(50, 50) == "five_hour"

    def test_single_window(self):
        st = sl.native_state({"rate_limits": {"seven_day": {"used_percentage": 7}}})
        assert "five_hour_utilization" not in st
        assert st["representative_claim"] == "seven_day"

    @pytest.mark.parametrize("payload", [
        {}, {"rate_limits": None}, {"rate_limits": {}},
        {"rate_limits": {"five_hour": {"used_percentage": "x"}}},
        {"rate_limits": {"five_hour": "nope"}},
    ])
    def test_nothing_usable_is_empty(self, payload):
        assert sl.native_state(payload) == {}


class TestUnreadCount:
    PAYLOAD = {"session_id": "sess-1",
               "workspace": {"project_dir": "/work/proj"}}

    def test_lookup_gets_session_and_project_dir(self, tmp_path):
        seen = []
        n = sl.unread_count(self.PAYLOAD, cache_dir=tmp_path,
                            lookup=lambda s, c: seen.append((s, c)) or 3)
        assert n == 3
        assert seen == [("sess-1", "/work/proj")]

    def test_cached_within_ttl(self, tmp_path):
        calls = []

        def lookup(s, c):
            calls.append(1)
            return len(calls)
        assert sl.unread_count(self.PAYLOAD, cache_dir=tmp_path, ttl=20,
                               lookup=lookup, now=1000.0) == 1
        assert sl.unread_count(self.PAYLOAD, cache_dir=tmp_path, ttl=20,
                               lookup=lookup, now=1019.0) == 1
        assert sl.unread_count(self.PAYLOAD, cache_dir=tmp_path, ttl=20,
                               lookup=lookup, now=1021.0) == 2
        assert len(calls) == 2

    def test_failed_lookup_is_cached_too(self, tmp_path):
        """A store that is down costs one attempt per ttl, not one per
        assistant message."""
        calls = []

        def boom(s, c):
            calls.append(1)
            raise RuntimeError("store down")
        for t in (1000.0, 1005.0, 1010.0):
            assert sl.unread_count(self.PAYLOAD, cache_dir=tmp_path, ttl=20,
                                   lookup=boom, now=t) is None
        assert len(calls) == 1

    def test_caches_are_per_session(self, tmp_path):
        other = dict(self.PAYLOAD, session_id="sess-2")
        sl.unread_count(self.PAYLOAD, cache_dir=tmp_path, lookup=lambda s, c: 1,
                        now=1000.0)
        assert sl.unread_count(other, cache_dir=tmp_path, lookup=lambda s, c: 5,
                               now=1001.0) == 5

    def test_no_session_no_lookup(self, tmp_path):
        assert sl.unread_count({}, cache_dir=tmp_path,
                               lookup=lambda s, c: pytest.fail("looked up")) is None

    def test_old_caches_are_pruned(self, tmp_path):
        stale = tmp_path / "statusline-gone.json"
        stale.write_text("{}")
        old = 1000.0 - sl.MAIL_CACHE_MAX_AGE_SECONDS - 10
        os.utime(stale, (old, old))
        sl.unread_count(self.PAYLOAD, cache_dir=tmp_path, lookup=lambda s, c: 0,
                        now=1000.0)
        assert not stale.exists()
        assert (tmp_path / "statusline-sess-1.json").exists()

    def test_session_id_is_sanitised_into_the_filename(self, tmp_path):
        sl.unread_count({"session_id": "../../etc/x"}, cache_dir=tmp_path,
                        lookup=lambda s, c: 0)
        assert [p.name for p in tmp_path.iterdir()] == ["statusline-.._.._etc_x.json"]


class TestLookupUnread:
    def test_mailbox_disabled_is_none(self):
        assert sl.lookup_unread("s", "/x", {"hooks": {"mailbox": {"enabled": False}}}) is None

    def test_marker_without_mailbox_is_none(self, tmp_path):
        (tmp_path / ".claude-hooks-disable").write_text("keep: memory\n")
        cfg = {"hooks": {"mailbox": {"enabled": True}}, "providers": {}}
        assert sl.lookup_unread("s", str(tmp_path), cfg) is None

    def test_no_provider_is_none(self, tmp_path):
        cfg = {"hooks": {"mailbox": {"enabled": True}}, "providers": {}}
        assert sl.lookup_unread("s", str(tmp_path), cfg) is None


class TestMailSegment:
    def test_hidden_when_zero_or_unknown(self):
        assert sl.mail_segment(0, fmt="emoji") == ""
        assert sl.mail_segment(None, fmt="emoji") == ""

    def test_glyphs(self):
        assert sl.mail_segment(2, fmt="emoji") == "📬 2"
        assert sl.mail_segment(2, fmt="ascii") == "mail:2"
        assert sl.mail_segment(2, fmt="plain") == "mail:2"


class TestCompose:
    def test_full_line(self):
        sys.path.insert(0, str(REPO))
        try:
            from scripts.statusline_compose import compose
        finally:
            sys.path.remove(str(REPO))
        line = compose({"workspace": {"current_dir": "/a/proj"},
                        "model": {"display_name": "Opus"},
                        "context_window": {"used_percentage": 23.7}},
                       usage="5h 1%", mail="📬 1")
        assert line == "proj | ctx: 23% used | Opus | 5h 1% | 📬 1"

    def test_cli_end_to_end_without_mail(self):
        payload = {"workspace": {"current_dir": "/a/proj"},
                   "model": {"display_name": "Opus"},
                   "rate_limits": {"five_hour": {"used_percentage": 42}}}
        out = subprocess.run(
            [sys.executable, str(COMPOSE), "--format", "plain", "--no-mail",
             "--proxy-url", "http://x/api/ratelimit.json"],
            input=json.dumps(payload), capture_output=True, text=True,
            encoding="utf-8", timeout=10)
        assert out.returncode == 0
        assert out.stdout == "proj | Opus | 5h 42%"

    def test_cli_never_breaks_on_garbage(self):
        out = subprocess.run([sys.executable, str(COMPOSE), "--no-mail"],
                             input="garbage", capture_output=True, text=True,
                             encoding="utf-8", timeout=10)
        assert out.returncode == 0
        assert out.stdout == "/"


class TestAckNotes:
    """An ack note on a message this session sent is delivered by the
    hooks at the next prompt, so an idle session saw nothing: the badge
    counted only the inbox."""

    PAYLOAD = {"session_id": "s-ack", "workspace": {"project_dir": "/p"}}

    def test_segment_shows_both_halves(self):
        assert sl.mail_segment(2, acks=1, fmt="emoji") == "📬 2 ↩1"
        assert sl.mail_segment(0, acks=3, fmt="emoji") == "📬 ↩3"
        assert sl.mail_segment(2, acks=1, fmt="ascii") == "mail:2 ack:1"
        assert sl.mail_segment(0, acks=1, fmt="plain") == "ack:1"
        assert sl.mail_segment(None, acks=0, fmt="emoji") == ""

    def test_counts_are_cached_as_a_pair(self, tmp_path):
        calls = []

        def lookup(s, c):
            calls.append(s)
            return (1, 2)
        for t in (1000.0, 1010.0):
            assert sl.mail_counts(self.PAYLOAD, cache_dir=tmp_path, ttl=20,
                                  lookup=lookup, now=t) == (1, 2)
        assert len(calls) == 1
        assert sl.unread_count(self.PAYLOAD, cache_dir=tmp_path, ttl=20,
                               lookup=lookup, now=1011.0) == 1

    def test_a_bare_count_lookup_has_no_acks(self, tmp_path):
        assert sl.mail_counts(self.PAYLOAD, cache_dir=tmp_path,
                              lookup=lambda s, c: 4) == (4, 0)

    def test_a_cache_file_from_before_acks_still_reads(self, tmp_path):
        import json
        path = sl._cache_path(tmp_path, "s-ack")
        path.write_text(json.dumps({"at": 1000.0, "count": 3}),
                        encoding="utf-8")
        assert sl.mail_counts(
            self.PAYLOAD, cache_dir=tmp_path, ttl=20, now=1005.0,
            lookup=lambda s, c: pytest.fail("looked up")) == (3, 0)

    def test_a_failed_lookup_is_none_not_zero(self, tmp_path):
        def boom(s, c):
            raise RuntimeError("store down")
        assert sl.mail_counts(self.PAYLOAD, cache_dir=tmp_path,
                              lookup=boom) is None
