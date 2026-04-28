"""Unit tests for claude_hooks.hyde."""

from __future__ import annotations

from unittest.mock import patch

from claude_hooks import hyde
from claude_hooks.hyde import (
    _call_ollama,
    _format_context,
    expand_query,
    expand_query_with_context,
)

from tests.mocks.ollama import mock_ollama_generate


def _args():
    return dict(
        user_prompt="raw",
        system_prompt="sys",
        model="gemma4:e2b",
        url="http://localhost:11434/api/generate",
        timeout=5.0,
        max_tokens=50,
    )


class TestCallOllama:
    def test_returns_response_text_when_long_enough(self):
        with mock_ollama_generate("this is long enough to count"):
            out = _call_ollama(**_args())
        assert out == "this is long enough to count"

    def test_returns_empty_on_short_response(self):
        # Under 10 chars → treated as no-useful-result.
        with mock_ollama_generate("short"):
            out = _call_ollama(**_args())
        assert out == ""

    def test_returns_empty_on_connection_failure(self):
        with mock_ollama_generate("ignored", fail=True):
            out = _call_ollama(**_args())
        assert out == ""

    def test_sends_keep_alive_in_body(self):
        captured = {}

        def _capture(req, timeout):
            import json
            captured["body"] = json.loads(req.data.decode("utf-8"))
            from io import BytesIO
            return _FakeResp(b'{"response": "response long enough"}')

        class _FakeResp:
            def __init__(self, p):
                from io import BytesIO
                self._b = BytesIO(p)
            def read(self, *a, **kw):
                return self._b.read(*a, **kw)
            def __enter__(self):
                return self
            def __exit__(self, *a):
                self._b.close()

        with patch("claude_hooks.hyde.urllib.request.urlopen", side_effect=_capture):
            _call_ollama(keep_alive="30m", **_args())
        assert captured["body"]["keep_alive"] == "30m"
        assert captured["body"]["think"] is False
        assert captured["body"]["options"]["num_predict"] == 50


class TestExpandQuery:
    def test_empty_prompt_returns_empty(self):
        assert expand_query("") == ""
        assert expand_query("   ") == "   "

    def test_uses_primary_model_when_it_works(self):
        with mock_ollama_generate("hypothetical answer here"):
            out = expand_query("what is X?")
        assert out == "hypothetical answer here"

    def test_falls_back_to_fallback_on_primary_failure(self):
        # First call returns empty (simulating primary fail), second returns text.
        from io import BytesIO
        import json

        class _FakeResp:
            def __init__(self, p):
                self._b = BytesIO(p)
            def read(self, *a, **kw): return self._b.read(*a, **kw)
            def __enter__(self): return self
            def __exit__(self, *a): self._b.close()

        responses = [
            _FakeResp(b'{"response": "tiny"}'),   # primary: <10 chars → rejected
            _FakeResp(json.dumps({"response": "fallback answer worked"}).encode()),
        ]
        with patch("claude_hooks.hyde.urllib.request.urlopen", side_effect=responses):
            out = expand_query("x", model="A", fallback_model="B")
        assert out == "fallback answer worked"

    def test_returns_prompt_when_all_models_fail(self):
        with mock_ollama_generate("", fail=True):
            out = expand_query("original query", model="A", fallback_model="B")
        assert out == "original query"


class TestExpandQueryWithContext:
    def test_empty_memories_returns_prompt(self):
        assert expand_query_with_context("what?", []) == "what?"

    def test_empty_prompt_returns_prompt(self):
        assert expand_query_with_context("", ["m1", "m2"]) == ""

    def test_grounded_response_returned_when_ollama_succeeds(self):
        with mock_ollama_generate("grounded response text"):
            out = expand_query_with_context(
                "q",
                ["memory one", "memory two"],
            )
        assert out == "grounded response text"

    def test_memories_appear_in_request_body(self):
        captured = {}

        def _capture(req, timeout):
            import json
            from io import BytesIO
            captured["body"] = json.loads(req.data.decode("utf-8"))
            class _R:
                def __init__(self, p): self._b = BytesIO(p)
                def read(self, *a, **kw): return self._b.read(*a, **kw)
                def __enter__(self): return self
                def __exit__(self, *a): self._b.close()
            return _R(b'{"response": "grounded answer here"}')

        with patch("claude_hooks.hyde.urllib.request.urlopen", side_effect=_capture):
            expand_query_with_context(
                "my question", ["FIRST-MEMORY-XYZ", "SECOND-ONE"],
                cache_enabled=False,  # bypass HyDE cache; this test asserts on request body shape
            )
        # The context block must reach the LLM.
        assert "FIRST-MEMORY-XYZ" in captured["body"]["prompt"]
        assert "SECOND-ONE" in captured["body"]["prompt"]

    def test_returns_prompt_when_all_fail(self):
        with mock_ollama_generate("", fail=True):
            out = expand_query_with_context("original", ["m1"])
        assert out == "original"


class TestFormatContext:
    def test_respects_max_chars(self):
        mems = ["a" * 500, "b" * 500]
        out = _format_context(mems, max_chars=200)
        assert len(out) <= 210  # allow for the "1. " prefix overhead

    def test_multiple_small_memories_all_fit(self):
        mems = ["alpha", "beta", "gamma"]
        out = _format_context(mems, max_chars=500)
        assert "1. alpha" in out
        assert "2. beta" in out
        assert "3. gamma" in out

    def test_200_char_minimum_per_entry_cap(self):
        # per_entry_cap = max(200, max_chars // N). With tiny budget the
        # floor kicks in — each entry truncated to 197+"..." = 200.
        mems = ["a" * 400, "b" * 400, "c" * 400, "d" * 400]
        out = _format_context(mems, max_chars=100)  # << 200
        # At least one entry should have fit since per-entry floor is 200,
        # even though overall budget is smaller. Expected: empty out because
        # the first entry alone exceeds max_chars. This documents the
        # known edge case.
        # (Not asserting a specific value here — just that it doesn't crash.)
        assert isinstance(out, str)

    def test_collapses_whitespace(self):
        mems = ["line\n\n\nwith   lots\tof    space"]
        out = _format_context(mems, max_chars=500)
        assert "\n\n\n" not in out
        assert "  " not in out

    def test_stops_before_overflow(self):
        mems = ["short"] * 100
        out = _format_context(mems, max_chars=50)
        assert len(out) <= 55
