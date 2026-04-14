"""Unit tests for claude_hooks.dedup."""

from __future__ import annotations

from claude_hooks.dedup import should_store, text_similarity
from claude_hooks.providers.base import Memory


class TestTextSimilarity:
    def test_identical_is_one(self):
        assert text_similarity("hello world", "hello world") == 1.0

    def test_disjoint_is_low(self):
        assert text_similarity("abcdefg", "zyxwvut") < 0.2

    def test_substring_in_between(self):
        sim = text_similarity("hello world", "hello")
        assert 0.5 < sim < 1.0

    def test_empty_inputs_do_not_crash(self):
        assert text_similarity("", "") == 1.0  # SequenceMatcher ratio on two empties
        assert text_similarity("", "abc") < 1.0
        assert text_similarity("abc", "") < 1.0

    def test_unicode_safe(self):
        a = "café naïve résumé"
        b = "café naïve résumé"
        assert text_similarity(a, b) == 1.0


class TestShouldStore:
    def test_empty_content_returns_false(self, fake_provider):
        p = fake_provider()
        assert should_store("   ", p) is False

    def test_empty_existing_returns_true(self, fake_provider):
        p = fake_provider(recall_returns=[])
        assert should_store("new content here", p) is True

    def test_below_threshold_returns_true(self, fake_provider):
        p = fake_provider(recall_returns=[Memory(text="completely unrelated text")])
        assert should_store("fresh new content", p, threshold=0.85) is True

    def test_above_threshold_returns_false(self, fake_provider):
        content = "The quick brown fox jumps over the lazy dog."
        p = fake_provider(recall_returns=[Memory(text=content)])
        assert should_store(content, p, threshold=0.85) is False

    def test_first_match_in_list_decides(self, fake_provider):
        content = "One specific unique statement."
        p = fake_provider(recall_returns=[
            Memory(text="irrelevant"),
            Memory(text=content),        # match
            Memory(text="also irrelevant"),
        ])
        assert should_store(content, p, threshold=0.80) is False

    def test_provider_error_fails_open(self, fake_provider):
        p = fake_provider(recall_errors=True)
        # On recall failure, return True — better to store a dup than lose data.
        assert should_store("anything", p) is True
