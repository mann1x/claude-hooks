"""Tests for the shared content_hash helper.

This function is the idempotency key for both PgvectorProvider and
SqliteVecProvider (v1.7+), plus scripts/migrate_to_pgvector.py.
Cross-store migration tools assume identical output on identical
input — regression test guards against accidental drift.
"""

from __future__ import annotations

import hashlib
import unittest

from claude_hooks.providers._content_hash import content_hash


class TestContentHash(unittest.TestCase):

    def test_returns_32_byte_sha256(self):
        h = content_hash("hello world")
        self.assertEqual(len(h), 32)
        self.assertIsInstance(h, bytes)
        # Sanity: matches stdlib sha256 of the normalised text.
        expected = hashlib.sha256(b"hello world").digest()
        self.assertEqual(h, expected)

    def test_known_value(self):
        # Pin the algorithm — if this changes, existing on-disk hashes
        # stop colliding with new ones and idempotency breaks.
        self.assertEqual(
            content_hash("hello world").hex(),
            "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9",
        )

    def test_whitespace_runs_collapse(self):
        self.assertEqual(
            content_hash("hello world"),
            content_hash("hello   world"),
        )
        self.assertEqual(
            content_hash("hello world"),
            content_hash("hello\tworld"),
        )
        self.assertEqual(
            content_hash("hello world"),
            content_hash("hello\nworld"),
        )

    def test_leading_and_trailing_whitespace_stripped(self):
        self.assertEqual(
            content_hash("hello world"),
            content_hash("  hello world  "),
        )
        self.assertEqual(
            content_hash("hello world"),
            content_hash("\n  hello world  \n"),
        )

    def test_case_is_preserved(self):
        # Normalisation is whitespace-only — case still matters.
        self.assertNotEqual(content_hash("Hello"), content_hash("hello"))

    def test_punctuation_is_preserved(self):
        self.assertNotEqual(
            content_hash("hello world"),
            content_hash("hello, world"),
        )

    def test_unicode_is_preserved(self):
        # UTF-8 round-trips faithfully.
        h_ascii = content_hash("cafe")
        h_utf = content_hash("café")
        self.assertNotEqual(h_ascii, h_utf)

    def test_empty_string_hashes_to_empty(self):
        # Documented behaviour: callers reject empty content upstream.
        self.assertEqual(
            content_hash(""),
            hashlib.sha256(b"").digest(),
        )
        self.assertEqual(
            content_hash("   "),
            hashlib.sha256(b"").digest(),
        )

    def test_pgvector_alias_still_works(self):
        # Regression guard: pgvector.py re-exports content_hash as
        # the private name _content_hash for back-compat with its
        # existing call sites. Both must produce identical output.
        from claude_hooks.providers import pgvector
        self.assertEqual(
            pgvector._content_hash("hello world"),
            content_hash("hello world"),
        )


if __name__ == "__main__":
    unittest.main()
