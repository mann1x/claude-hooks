"""Context-overflow shrink-and-retry in ``LlamafileEmbedder.embed``.

``max_chars`` is a *character* cap standing in for a token budget, and
that substitution carries a hidden assumption. With ``max_chars=30000``
against a 16384-token window it needs at least ~1.83 chars/token.
Measured on solidpc 2026-07-25 with the model's own tokenizer:

    real memory text   30 000 chars ->  9 580 tokens  (3.13)  fits
    base64 blob        30 000 chars -> 22 188 tokens  (1.35)  overflows
    minified JSON      30 000 chars -> 25 471 tokens  (1.18)  overflows

Real memories are dense (code, paths, hashes) but nowhere near base64,
so the cap normally holds with ~6 800 tokens spare. When it doesn't, the
server answers HTTP 400 in ~0.1 s and the store raises -- which on the
detached path is a ``log.warning`` and a **silently lost memory**.

Rather than lower ``max_chars`` for everyone to accommodate content
almost nobody stores, the overflow is handled where it happens: the 400
body carries ``n_prompt_tokens`` and ``n_ctx``, so the text is cut by
that ratio and retried.

The tests below drive a fake HTTP layer rather than a live llamafile, so
they assert the *decision* logic -- which 400s are retryable, how far the
text is cut, and that non-overflow errors are never retried.
"""

from __future__ import annotations

import json
import sys
import unittest
import urllib.error
from io import BytesIO
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from claude_hooks import embedders  # noqa: E402
from claude_hooks.embedders import (  # noqa: E402
    ContextOverflowError,
    EmbedderError,
    LlamafileEmbedder,
    _parse_context_overflow,
)

OVERFLOW_BODY = json.dumps({
    "error": {
        "code": 400,
        "message": ("request (22275 tokens) exceeds the available context "
                    "size (16384 tokens), try increasing it"),
        "type": "exceed_context_size_error",
        "n_prompt_tokens": 22275,
        "n_ctx": 16384,
    }
})


def _http_error(code: int, body: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://x/embedding", code, "Bad Request", {}, BytesIO(body.encode()))


class _FakeServer:
    """Stands in for llama.cpp: rejects anything over ``token_budget``.

    ``chars_per_token`` models how densely the payload tokenises, which
    is the whole variable under test.
    """

    def __init__(self, *, chars_per_token: float, n_ctx: int = 16384):
        self.chars_per_token = chars_per_token
        self.n_ctx = n_ctx
        self.seen: list[int] = []

    def __call__(self, req, timeout=None):
        text = json.loads(req.data.decode())["content"]
        self.seen.append(len(text))
        tokens = int(len(text) / self.chars_per_token)
        if tokens > self.n_ctx:
            raise _http_error(400, json.dumps({"error": {
                "code": 400, "type": "exceed_context_size_error",
                "message": (f"request ({tokens} tokens) exceeds the available "
                            f"context size ({self.n_ctx} tokens)"),
                "n_prompt_tokens": tokens, "n_ctx": self.n_ctx}}))

        class _Resp:
            def read(self_inner):
                return json.dumps({"embedding": [0.1, 0.2, 0.3]}).encode()

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

        return _Resp()


def _embedder(**kw) -> LlamafileEmbedder:
    return LlamafileEmbedder(url="http://x/embedding", max_chars=30000,
                             daemon_ensure=False, **kw)


class TestParseContextOverflow(unittest.TestCase):
    def test_recognises_the_real_body(self):
        self.assertEqual(_parse_context_overflow(400, OVERFLOW_BODY),
                         (22275, 16384))

    def test_recognises_by_message_when_type_is_absent(self):
        body = json.dumps({"error": {
            "message": "request (99 tokens) exceeds the available context "
                       "size (10 tokens)", "n_prompt_tokens": 99, "n_ctx": 10}})
        self.assertEqual(_parse_context_overflow(400, body), (99, 10))

    def test_other_400_is_not_an_overflow(self):
        body = json.dumps({"error": {"code": 400, "message": "bad request"}})
        self.assertIsNone(_parse_context_overflow(400, body))

    def test_non_400_is_never_an_overflow(self):
        self.assertIsNone(_parse_context_overflow(500, OVERFLOW_BODY))

    def test_unparseable_body_is_not_an_overflow(self):
        self.assertIsNone(_parse_context_overflow(400, "<html>nope</html>"))

    def test_missing_numbers_degrade_to_zeros(self):
        body = json.dumps({"error": {"type": "exceed_context_size_error"}})
        self.assertEqual(_parse_context_overflow(400, body), (0, 0))


class TestShrinkAndRetry(unittest.TestCase):
    def _run(self, chars_per_token, text_len=30000):
        srv = _FakeServer(chars_per_token=chars_per_token)
        e = _embedder()
        orig = embedders.urllib.request.urlopen
        embedders.urllib.request.urlopen = srv
        try:
            vec = e.embed("x" * text_len)
        finally:
            embedders.urllib.request.urlopen = orig
        return vec, srv

    def test_content_that_fits_is_sent_once(self):
        vec, srv = self._run(chars_per_token=3.13)
        self.assertEqual(len(vec), 3)
        self.assertEqual(len(srv.seen), 1, "no retry for content that fits")
        self.assertEqual(srv.seen[0], 30000)

    def test_base64_density_recovers(self):
        """1.35 chars/token — the measured base64 ratio."""
        vec, srv = self._run(chars_per_token=1.35)
        self.assertEqual(len(vec), 3)
        self.assertGreater(len(srv.seen), 1, "should have retried")
        self.assertLess(srv.seen[-1], srv.seen[0], "retry must be smaller")

    def test_minified_json_density_recovers(self):
        """1.18 chars/token — the measured minified-JSON ratio."""
        vec, srv = self._run(chars_per_token=1.18)
        self.assertEqual(len(vec), 3)
        self.assertGreater(len(srv.seen), 1)

    def test_converges_within_two_round_trips(self):
        """The cut is computed from the server's own ratio, so it should
        land in one correction — not a blind halving sequence."""
        for cpt in (1.35, 1.18, 1.0):
            with self.subTest(chars_per_token=cpt):
                _, srv = self._run(chars_per_token=cpt)
                self.assertLessEqual(len(srv.seen), 2)

    def test_shrink_is_monotonic(self):
        _, srv = self._run(chars_per_token=1.0)
        self.assertEqual(srv.seen, sorted(srv.seen, reverse=True))

    def test_gives_up_with_a_clear_error(self):
        """A server that reports overflow no matter how small the input
        must not loop forever. Uses fixed numbers so the shrink can
        never satisfy it — a misbehaving or misconfigured server."""
        seen: list[int] = []

        def always_overflow(req, timeout=None):
            seen.append(len(json.loads(req.data.decode())["content"]))
            raise _http_error(400, OVERFLOW_BODY)

        e = _embedder()
        orig = embedders.urllib.request.urlopen
        embedders.urllib.request.urlopen = always_overflow
        try:
            with self.assertRaises(EmbedderError) as ctx:
                e.embed("x" * 30000)
        finally:
            embedders.urllib.request.urlopen = orig
        self.assertIn("exceeds context", str(ctx.exception))
        self.assertLessEqual(len(seen), e.CTX_RETRY_ATTEMPTS)
        self.assertGreater(len(seen), 1, "should have tried at least once more")

    def test_extreme_density_still_converges(self):
        """0.01 chars/token is absurd, but the ratio-based cut handles
        it in one correction rather than giving up."""
        vec, srv = self._run(chars_per_token=0.01)
        self.assertEqual(len(vec), 3)
        self.assertLessEqual(len(srv.seen), 2)


class TestNonOverflowErrorsAreNotRetried(unittest.TestCase):
    def test_plain_400_raises_immediately(self):
        calls = []

        def boom(req, timeout=None):
            calls.append(1)
            raise _http_error(400, json.dumps({"error": {"message": "nope"}}))

        e = _embedder()
        orig = embedders.urllib.request.urlopen
        embedders.urllib.request.urlopen = boom
        try:
            with self.assertRaises(EmbedderError):
                e.embed("hello")
        finally:
            embedders.urllib.request.urlopen = orig
        self.assertEqual(len(calls), 1, "a non-overflow 400 must not retry")

    def test_500_raises_immediately(self):
        calls = []

        def boom(req, timeout=None):
            calls.append(1)
            raise _http_error(500, "server exploded")

        e = _embedder()
        orig = embedders.urllib.request.urlopen
        embedders.urllib.request.urlopen = boom
        try:
            with self.assertRaises(EmbedderError):
                e.embed("hello")
        finally:
            embedders.urllib.request.urlopen = orig
        self.assertEqual(len(calls), 1)


class TestShrinkHelper(unittest.TestCase):
    def test_uses_the_server_ratio_and_the_token_budget(self):
        """The cut must satisfy both constraints. Fitting n_ctx alone is
        not enough: verified live on solidpc, a 30 000-char base64
        payload cut to ~15 000 tokens sits comfortably inside the 16 384
        window and *still* ran past a 300 s timeout. So the binding
        limit is normally the clock, not the window."""
        e = _embedder()
        err = ContextOverflowError("x", n_prompt_tokens=22275, n_ctx=16384)
        out = e._shrink_for_ctx("y" * 30000, err)
        chars_per_token = 30000 / 22275
        budget = min(16384 * e.CTX_SHRINK_SAFETY,
                     float(e.CTX_RETRY_TOKEN_BUDGET))
        self.assertEqual(len(out), int(budget * chars_per_token))

    def test_token_budget_binds_before_the_context_window(self):
        e = _embedder()
        err = ContextOverflowError("x", n_prompt_tokens=22275, n_ctx=16384)
        out = e._shrink_for_ctx("y" * 30000, err)
        ctx_only = int(30000 * (16384 / 22275) * e.CTX_SHRINK_SAFETY)
        self.assertLess(len(out), ctx_only,
                        "clock budget must cut harder than the window alone")

    def test_retry_stays_inside_the_embedder_timeout(self):
        """Guards the cross-host sizing. Windows runs llamafile ~2×
        slower at identical weights, so a budget tuned to solidpc alone
        would overrun the 180 s timeout on pandorum."""
        e = _embedder()
        self.assertLessEqual(e.CTX_RETRY_TOKEN_BUDGET, 2048)

    def test_blind_cut_without_numbers(self):
        e = _embedder()
        err = ContextOverflowError("x", n_prompt_tokens=0, n_ctx=0)
        out = e._shrink_for_ctx("y" * 1000, err)
        self.assertEqual(len(out), int(1000 * e.CTX_BLIND_SHRINK))

    def test_returns_none_when_no_progress_possible(self):
        e = _embedder()
        err = ContextOverflowError("x", n_prompt_tokens=10, n_ctx=1000)
        self.assertIsNone(e._shrink_for_ctx("y" * 100, err))


if __name__ == "__main__":
    unittest.main()
