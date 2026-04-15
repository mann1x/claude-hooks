"""
Tests for ``claude_hooks.proxy.stop_phrase_guard``.

Covers:
- YAML subset loader (top-level keys → list of strings)
- StopPhraseScanner per-category counting
- Chunked feed — matches that span chunk boundaries counted once
- Malformed patterns skipped, no crash
- Missing YAML file → empty scanner, not a crash
- Integration: SseTail uses the module-level factory when set
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from claude_hooks.proxy.stop_phrase_guard import (
    StopPhraseScanner,
    _load_phrases_yaml,
)
from claude_hooks.proxy import sse as sse_mod


# ============================================================ #
class TestYamlLoader:
    def test_basic_shape(self, tmp_path):
        p = tmp_path / "sp.yaml"
        p.write_text(
            "cat1:\n"
            "  - \"foo\"\n"
            "  - 'bar baz'\n"
            "cat2:\n"
            "  - unquoted plain\n"
        )
        out = _load_phrases_yaml(p)
        assert out == {
            "cat1": ["foo", "bar baz"],
            "cat2": ["unquoted plain"],
        }

    def test_comments_and_blank_lines_ignored(self, tmp_path):
        p = tmp_path / "sp.yaml"
        p.write_text(
            "# top comment\n"
            "\n"
            "only_cat:\n"
            "  # inner comment\n"
            "  - \"hello\"    # trailing\n"
            "\n"
        )
        assert _load_phrases_yaml(p) == {"only_cat": ["hello"]}

    def test_missing_file_returns_empty(self, tmp_path):
        assert _load_phrases_yaml(tmp_path / "nope.yaml") == {}

    def test_real_repo_phrase_file_parses(self):
        from claude_hooks.proxy.stop_phrase_guard import DEFAULT_PHRASES_PATH
        out = _load_phrases_yaml(DEFAULT_PHRASES_PATH)
        # All 8 stellaraccident categories present.
        assert set(out.keys()) == {
            "ownership_dodging", "permission_seeking", "premature_stopping",
            "known_limitation_labeling", "session_length_excuses",
            "simplest_fix", "reasoning_reversal", "self_admitted_error",
        }
        # Each has at least one pattern and nothing empty.
        for cat, pats in out.items():
            assert pats, f"{cat} has no patterns"
            for p in pats:
                assert p and isinstance(p, str)


# ============================================================ #
class TestScanner:
    def _sc(self, **kw):
        return StopPhraseScanner(
            kw or {
                "perm": [r"should i continue\??"],
                "lazy": [r"that was lazy"],
                "revert": [r"\boh wait\b", r"\bactually,"],
            }
        )

    def test_single_chunk_matches(self):
        s = self._sc()
        s.feed("You are right — that was lazy. Actually, should I continue?")
        assert s.category_counts == {"perm": 1, "lazy": 1, "revert": 1}
        assert s.total_hits() == 3

    def test_multiple_hits_same_category(self):
        s = self._sc(lazy=[r"that was lazy"])
        s.feed("that was lazy and that was lazy again")
        assert s.category_counts == {"lazy": 2}

    def test_chunk_boundary_not_double_counted(self):
        s = self._sc()
        # Split a phrase across feeds; must count exactly once.
        s.feed("prelude… should I ")
        s.feed("continue? tail")
        assert s.category_counts["perm"] == 1

    def test_chunk_boundary_no_false_positive_on_rescan(self):
        """Matches that fell entirely inside the carry buffer of the
        previous feed must NOT be re-counted on the next feed.
        """
        s = self._sc()
        s.feed("that was lazy.")
        s.feed("more text with nothing relevant")
        assert s.category_counts["lazy"] == 1

    def test_unicode_dash_safe(self):
        s = self._sc()
        s.feed("nothing weird — that was lazy — end")
        assert s.category_counts["lazy"] == 1

    def test_bytes_input_decoded(self):
        s = self._sc()
        s.feed("that was lazy".encode("utf-8"))
        assert s.category_counts["lazy"] == 1

    def test_empty_feed_noop(self):
        s = self._sc()
        s.feed("")
        s.feed(None)  # defensive
        assert s.total_hits() == 0

    def test_malformed_pattern_skipped(self, caplog):
        # unbalanced paren → re.error during compile; scanner skips
        # the bad pattern but keeps going.
        s = StopPhraseScanner({
            "bad": ["(unclosed"],
            "good": ["hit"],
        })
        assert "bad" not in s.categories
        assert "good" in s.categories
        s.feed("we have a hit here")
        assert s.category_counts["good"] == 1


# ============================================================ #
class TestSseIntegration:
    def test_factory_produces_per_tail_scanner(self, monkeypatch):
        """``SseTail`` constructs its own scanner via the module
        factory so concurrent responses don't share state.
        """
        def factory():
            return StopPhraseScanner({"dodge": [r"not my changes"]})

        monkeypatch.setattr(sse_mod, "_STOP_SCANNER_FACTORY", factory)

        tail_a = sse_mod.SseTail()
        tail_b = sse_mod.SseTail()
        assert tail_a.stop_scanner is not None
        assert tail_b.stop_scanner is not None
        assert tail_a.stop_scanner is not tail_b.stop_scanner

        tail_a.stop_scanner.feed("That's not my changes at all")
        assert tail_a.stop_scanner.category_counts["dodge"] == 1
        assert tail_b.stop_scanner.category_counts["dodge"] == 0

    def test_disabled_factory_leaves_scanner_none(self, monkeypatch):
        monkeypatch.setattr(sse_mod, "_STOP_SCANNER_FACTORY", None)
        tail = sse_mod.SseTail()
        assert tail.stop_scanner is None

    def test_text_delta_feeds_scanner(self, monkeypatch):
        """text_delta events must route their text into the scanner."""
        captured = []

        class FakeScanner:
            def __init__(self):
                self.category_counts = {}
            def feed(self, text):
                captured.append(text)

        monkeypatch.setattr(sse_mod, "_STOP_SCANNER_FACTORY",
                            lambda: FakeScanner())

        tail = sse_mod.SseTail()
        # Emulate an SSE text_delta event.
        payload = json.dumps({
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "hello world"},
        }).encode()
        frame = b"event: content_block_delta\ndata: " + payload + b"\n\n"
        list(tail.wrap([frame]))
        assert captured == ["hello world"]
