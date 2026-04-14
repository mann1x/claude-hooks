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


def _openai_resp(vec):
    return _FakeResp(json.dumps({"data": [{"embedding": vec}]}).encode())


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
            with pytest.raises(EmbedderError, match="unexpected"):
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
