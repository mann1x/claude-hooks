"""Unit tests for claude_hooks.embedders."""

from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import patch

import pytest

from claude_hooks.embedders import (
    EmbedderError,
    NullEmbedder,
    OllamaEmbedder,
    OpenAiCompatibleEmbedder,
    make_embedder,
)


class _FakeResp:
    def __init__(self, payload: bytes, status: int = 200):
        self._buf = BytesIO(payload)
        self.status = status

    def read(self, *a, **kw):
        return self._buf.read(*a, **kw)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self._buf.close()


def _ollama_resp(vec):
    return _FakeResp(json.dumps({"embedding": vec}).encode())


def _ollama_batch_resp(vecs):
    return _FakeResp(json.dumps({"embeddings": vecs}).encode())


def _openai_resp(vec):
    return _FakeResp(json.dumps({"data": [{"embedding": vec}]}).encode())


def _openai_batch_resp(vecs):
    return _FakeResp(json.dumps({"data": [{"embedding": v} for v in vecs]}).encode())


class TestNullEmbedder:
    def test_raises(self):
        with pytest.raises(EmbedderError):
            NullEmbedder().embed("anything")


class TestMakeEmbedder:
    def test_ollama(self):
        e = make_embedder("ollama", {"model": "nomic-embed-text", "url": "http://h/e"})
        assert isinstance(e, OllamaEmbedder)
        assert e.model == "nomic-embed-text"
        assert e.url == "http://h/e"

    def test_openai(self):
        e = make_embedder("openai", {"model": "text-embedding-3-small"})
        assert isinstance(e, OpenAiCompatibleEmbedder)
        assert e.model == "text-embedding-3-small"

    def test_openai_compatible_alias(self):
        assert isinstance(make_embedder("openai_compatible", {}), OpenAiCompatibleEmbedder)

    def test_unknown_returns_null(self):
        assert isinstance(make_embedder("invalid", {}), NullEmbedder)


class TestOllamaEmbedder:
    def _vec(self):
        return [0.1] * 768

    def test_embed_returns_vector(self):
        vec = self._vec()
        with patch(
            "claude_hooks.embedders.urllib.request.urlopen",
            return_value=_ollama_resp(vec),
        ):
            out = OllamaEmbedder().embed("hello world")
        assert out == vec

    def test_empty_input_raises(self):
        with pytest.raises(EmbedderError, match="empty"):
            OllamaEmbedder().embed("")

    def test_connection_refused_raises(self):
        import urllib.error
        with patch(
            "claude_hooks.embedders.urllib.request.urlopen",
            side_effect=urllib.error.URLError("refused"),
        ):
            with pytest.raises(EmbedderError, match="unreachable"):
                OllamaEmbedder().embed("hi")

    def test_bad_response_shape_raises(self):
        bad = _FakeResp(json.dumps({"not_an_embedding": "oops"}).encode())
        with patch(
            "claude_hooks.embedders.urllib.request.urlopen",
            return_value=bad,
        ):
            with pytest.raises(EmbedderError, match="no embedding"):
                OllamaEmbedder().embed("hi")

    def test_sets_dim_on_first_call(self):
        vec = self._vec()
        e = OllamaEmbedder()
        assert e.dim == 0
        with patch(
            "claude_hooks.embedders.urllib.request.urlopen",
            return_value=_ollama_resp(vec),
        ):
            e.embed("hi")
        assert e.dim == len(vec)


class TestOpenAiCompatibleEmbedder:
    def test_embed_returns_vector(self):
        vec = [0.2] * 128
        with patch(
            "claude_hooks.embedders.urllib.request.urlopen",
            return_value=_openai_resp(vec),
        ):
            out = OpenAiCompatibleEmbedder().embed("hello")
        assert out == vec

    def test_empty_input_raises(self):
        with pytest.raises(EmbedderError, match="empty"):
            OpenAiCompatibleEmbedder().embed("")

    def test_unexpected_shape_raises(self):
        bad = _FakeResp(json.dumps({"data": []}).encode())
        with patch(
            "claude_hooks.embedders.urllib.request.urlopen",
            return_value=bad,
        ):
            # An empty data array surfaces as a count-mismatch error after
            # the batch refactor — both shapes are still EmbedderError.
            with pytest.raises(EmbedderError, match="returned 0 for 1"):
                OpenAiCompatibleEmbedder().embed("hi")

    def test_api_key_added_to_headers(self):
        vec = [0.0] * 10
        captured = {}

        def _capture(req, timeout):
            captured["headers"] = dict(req.header_items())
            return _openai_resp(vec)

        with patch("claude_hooks.embedders.urllib.request.urlopen", side_effect=_capture):
            OpenAiCompatibleEmbedder(api_key="sk-xyz").embed("hi")

        auth_header = {k.lower(): v for k, v in captured["headers"].items()}.get("authorization")
        assert auth_header == "Bearer sk-xyz"


class TestEmbedBatch:
    """Verify embed_batch hits the right endpoint shape per backend."""

    def test_ollama_batch_uses_api_embed_endpoint(self):
        vecs = [[0.1] * 4, [0.2] * 4, [0.3] * 4]
        captured = {}

        def _capture(req, timeout):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode())
            return _ollama_batch_resp(vecs)

        with patch("claude_hooks.embedders.urllib.request.urlopen", side_effect=_capture):
            out = OllamaEmbedder().embed_batch(["a", "b", "c"])

        assert out == vecs
        assert captured["url"].endswith("/api/embed")
        assert captured["body"]["input"] == ["a", "b", "c"]

    def test_ollama_batch_falls_back_on_http_error(self):
        """Older Ollama daemons without /api/embed should fall back to per-text."""
        import urllib.error
        vecs = [[0.4] * 3, [0.5] * 3]
        call_count = {"n": 0}

        def _side(req, timeout):
            call_count["n"] += 1
            if "/api/embed" in req.full_url and "/api/embeddings" not in req.full_url:
                # batch endpoint -> 404
                raise urllib.error.HTTPError(req.full_url, 404, "not found", {}, None)
            # singleton fallback -> per-text response
            idx = call_count["n"] - 2  # first batch call, then per-text starts at idx=0
            return _ollama_resp(vecs[idx])

        with patch("claude_hooks.embedders.urllib.request.urlopen", side_effect=_side):
            out = OllamaEmbedder().embed_batch(["x", "y"])

        assert out == vecs

    def test_ollama_batch_empty_returns_empty(self):
        # Should not even hit the network.
        with patch("claude_hooks.embedders.urllib.request.urlopen") as m:
            assert OllamaEmbedder().embed_batch([]) == []
            assert not m.called

    def test_openai_batch_single_call(self):
        vecs = [[0.1] * 5, [0.2] * 5]
        captured = {}

        def _capture(req, timeout):
            captured["body"] = json.loads(req.data.decode())
            return _openai_batch_resp(vecs)

        with patch("claude_hooks.embedders.urllib.request.urlopen", side_effect=_capture):
            out = OpenAiCompatibleEmbedder().embed_batch(["a", "b"])

        assert out == vecs
        # OpenAI format puts the whole batch in one request.
        assert captured["body"]["input"] == ["a", "b"]

    def test_openai_single_embed_uses_batch_path_internally(self):
        """embed(text) should still work after the refactor."""
        vec = [0.7] * 3
        with patch(
            "claude_hooks.embedders.urllib.request.urlopen",
            return_value=_openai_batch_resp([vec]),
        ):
            assert OpenAiCompatibleEmbedder().embed("solo") == vec

    def test_openai_batch_count_mismatch_raises(self):
        """Server returning fewer embeddings than inputs is a hard error."""
        with patch(
            "claude_hooks.embedders.urllib.request.urlopen",
            return_value=_openai_batch_resp([[0.0] * 3]),  # 1 emb for 3 inputs
        ):
            with pytest.raises(EmbedderError, match="returned 1 for 3"):
                OpenAiCompatibleEmbedder().embed_batch(["a", "b", "c"])
