"""Tests for LlamafileEmbedder + CompositeEmbedder (v1.4).

Mocking strategy mirrors ``tests/test_embedders.py``: we patch
``urllib.request.urlopen`` so HTTP failures, response-shape variants,
and timeout paths can be exercised without a real llamafile process.

The daemon-supervision hook (``LlamafileEmbedder._ensure_running``) is
left as a best-effort no-op while ``daemon_client.embedding_ensure``
hasn't been wired yet (that lands with task #35). Tests that need to
prove the hook is **not** called for unrelated paths patch it
explicitly.
"""

from __future__ import annotations

import json
import socket
import urllib.error
from io import BytesIO
from unittest.mock import patch

import pytest

from claude_hooks.embedders import (
    CompositeEmbedder,
    EmbedderError,
    LlamafileEmbedder,
    NullEmbedder,
    OllamaEmbedder,
    OpenAiCompatibleEmbedder,
    make_embedder,
)


# --------------------------------------------------------------------- #
# Shared test fakes — same shape as test_embedders.py uses
# --------------------------------------------------------------------- #

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


def _llama_flat(vec):
    """``{"embedding": [...]}`` — the documented llama.cpp shape."""
    return _FakeResp(json.dumps({"embedding": vec}).encode())


def _llama_wrapped(vec):
    """``[{"embedding": [...]}]`` — observed on some llamafile builds."""
    return _FakeResp(json.dumps([{"embedding": vec}]).encode())


def _llama_nested(vec):
    """``{"embedding": [[...]]}`` — n-batch=1 nested shape."""
    return _FakeResp(json.dumps({"embedding": [vec]}).encode())


def _llama_batch(vecs):
    """Batch response: ``[{"embedding": ...}, ...]``."""
    return _FakeResp(json.dumps([{"embedding": v} for v in vecs]).encode())


# --------------------------------------------------------------------- #
# LlamafileEmbedder
# --------------------------------------------------------------------- #

class TestLlamafileEmbedder:
    def _vec(self):
        return [0.1] * 1024

    # ---- happy paths --------------------------------------------------

    def test_embed_flat_shape(self):
        e = LlamafileEmbedder(daemon_ensure=False)
        with patch("urllib.request.urlopen", return_value=_llama_flat(self._vec())):
            out = e.embed("hello")
        assert out == self._vec()
        assert e.dim == 1024

    def test_embed_list_wrapped_shape(self):
        e = LlamafileEmbedder(daemon_ensure=False)
        with patch("urllib.request.urlopen", return_value=_llama_wrapped(self._vec())):
            out = e.embed("hello")
        assert out == self._vec()
        assert e.dim == 1024

    def test_embed_double_nested_shape(self):
        e = LlamafileEmbedder(daemon_ensure=False)
        with patch("urllib.request.urlopen", return_value=_llama_nested(self._vec())):
            out = e.embed("hello")
        assert out == self._vec()
        assert e.dim == 1024

    def test_embed_truncates_long_input(self):
        e = LlamafileEmbedder(daemon_ensure=False, max_chars=10)
        captured = {}

        def _spy(req, timeout):
            captured["body"] = req.data.decode("utf-8")
            return _llama_flat([0.5])

        with patch("urllib.request.urlopen", side_effect=_spy):
            e.embed("x" * 100)
        assert json.loads(captured["body"])["content"] == "x" * 10

    # ---- error paths --------------------------------------------------

    def test_embed_empty_raises(self):
        with pytest.raises(EmbedderError, match="empty"):
            LlamafileEmbedder(daemon_ensure=False).embed("")

    def test_embed_http_error(self):
        e = LlamafileEmbedder(daemon_ensure=False)
        err = urllib.error.HTTPError(
            url="http://x", code=500, msg="boom", hdrs=None,
            fp=BytesIO(b"server-msg"),
        )
        with patch("urllib.request.urlopen", side_effect=err):
            with pytest.raises(EmbedderError, match="HTTP 500"):
                e.embed("hello")

    def test_embed_network_unreachable(self):
        e = LlamafileEmbedder(daemon_ensure=False)
        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.URLError("connection refused")):
            with pytest.raises(EmbedderError, match="unreachable"):
                e.embed("hello")

    def test_embed_socket_timeout(self):
        e = LlamafileEmbedder(daemon_ensure=False)
        with patch("urllib.request.urlopen", side_effect=socket.timeout("slow")):
            with pytest.raises(EmbedderError, match="unreachable"):
                e.embed("hello")

    def test_embed_empty_embedding_raises(self):
        e = LlamafileEmbedder(daemon_ensure=False)
        with patch("urllib.request.urlopen", return_value=_FakeResp(
                json.dumps({"embedding": []}).encode())):
            with pytest.raises(EmbedderError, match="no embedding"):
                e.embed("hello")

    def test_embed_non_dict_non_list_response(self):
        e = LlamafileEmbedder(daemon_ensure=False)
        with patch("urllib.request.urlopen", return_value=_FakeResp(
                json.dumps("just a string").encode())):
            with pytest.raises(EmbedderError, match="unexpected response type"):
                e.embed("hello")

    def test_embed_empty_list_response(self):
        e = LlamafileEmbedder(daemon_ensure=False)
        with patch("urllib.request.urlopen", return_value=_FakeResp(b"[]")):
            with pytest.raises(EmbedderError, match="empty list"):
                e.embed("hello")

    # ---- batch --------------------------------------------------------

    def test_embed_batch_happy(self):
        e = LlamafileEmbedder(daemon_ensure=False)
        vecs = [[0.1] * 8, [0.2] * 8, [0.3] * 8]
        with patch("urllib.request.urlopen", return_value=_llama_batch(vecs)):
            out = e.embed_batch(["a", "b", "c"])
        assert out == vecs
        assert e.dim == 8

    def test_embed_batch_empty_short_circuits(self):
        e = LlamafileEmbedder(daemon_ensure=False)
        # No HTTP call should happen; we'd see a TypeError if it did.
        assert e.embed_batch([]) == []

    def test_embed_batch_falls_back_on_http_error(self):
        """Older llama.cpp builds reject ``content: [list]`` — the
        embedder should re-issue per-text requests."""
        e = LlamafileEmbedder(daemon_ensure=False)

        calls = {"n": 0}
        per_text_vec = [0.7] * 4

        def _urlopen(req, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.HTTPError(
                    url="http://x", code=400, msg="batch not supported",
                    hdrs=None, fp=BytesIO(b""),
                )
            return _llama_flat(per_text_vec)

        with patch("urllib.request.urlopen", side_effect=_urlopen):
            out = e.embed_batch(["a", "b"])
        assert out == [per_text_vec, per_text_vec]
        assert calls["n"] == 3  # one failed batch + two per-text

    def test_embed_batch_dim_set_from_first(self):
        e = LlamafileEmbedder(daemon_ensure=False)
        with patch("urllib.request.urlopen",
                   return_value=_llama_batch([[0.0] * 16])):
            e.embed_batch(["only-one"])
        assert e.dim == 16

    def test_embed_batch_length_mismatch_raises(self):
        e = LlamafileEmbedder(daemon_ensure=False)
        with patch("urllib.request.urlopen",
                   return_value=_llama_batch([[0.0] * 4])):
            with pytest.raises(EmbedderError, match="for 2 inputs"):
                e.embed_batch(["a", "b"])

    # ---- daemon ensure hook ------------------------------------------

    def test_daemon_ensure_false_skips_hook(self):
        """daemon_ensure=False must not even *try* to import
        daemon_client (matters in unit-test envs where the daemon
        module may not be importable)."""
        e = LlamafileEmbedder(daemon_ensure=False)
        with patch("urllib.request.urlopen", return_value=_llama_flat([0.5])):
            with patch.object(LlamafileEmbedder, "_ensure_running",
                              wraps=e._ensure_running) as spy:
                e.embed("x")
        # _ensure_running is still called (it's always called on the
        # embed path), but its short-circuit on daemon_ensure=False
        # means no daemon_client import attempt.
        assert spy.called

    def test_daemon_ensure_calls_client_when_available(self):
        """If daemon_client.embedding_ensure exists, it must be invoked
        before the HTTP request."""
        e = LlamafileEmbedder(daemon_ensure=True)
        call_order: list[str] = []

        # Build a fake daemon_client module surface.
        class _FakeDaemonClient:
            @staticmethod
            def embedding_ensure():
                call_order.append("ensure")

        # urlopen is the second event in the desired order.
        def _urlopen(req, timeout):
            call_order.append("http")
            return _llama_flat([0.5])

        with patch.dict("sys.modules", {"claude_hooks.daemon_client": _FakeDaemonClient}):
            with patch("urllib.request.urlopen", side_effect=_urlopen):
                e.embed("x")
        assert call_order == ["ensure", "http"]

    def test_daemon_ensure_swallows_client_errors(self):
        """If daemon_client.embedding_ensure raises, the embedder must
        still attempt the HTTP request (the daemon may be down but the
        llamafile started by hand)."""
        e = LlamafileEmbedder(daemon_ensure=True)

        class _FakeDaemonClient:
            @staticmethod
            def embedding_ensure():
                raise RuntimeError("daemon socket closed")

        with patch.dict("sys.modules", {"claude_hooks.daemon_client": _FakeDaemonClient}):
            with patch("urllib.request.urlopen", return_value=_llama_flat([0.5])):
                out = e.embed("x")
        assert out == [0.5]


# --------------------------------------------------------------------- #
# CompositeEmbedder
# --------------------------------------------------------------------- #

class _StubEmbedder:
    """Minimal Embedder-shaped stub. Lets us script success/failure
    outcomes without importing real backends."""

    def __init__(self, name: str, *, vec=None, raises=None, batch_vecs=None):
        self.name = name
        self.dim = 0
        self._vec = vec
        self._raises = raises
        self._batch_vecs = batch_vecs

    def embed(self, text: str) -> list[float]:
        if self._raises:
            raise self._raises
        assert self._vec is not None
        self.dim = len(self._vec)
        return list(self._vec)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if self._raises:
            raise self._raises
        assert self._batch_vecs is not None
        if self._batch_vecs:
            self.dim = len(self._batch_vecs[0])
        return [list(v) for v in self._batch_vecs]


class TestCompositeEmbedder:
    # ---- happy paths --------------------------------------------------

    def test_primary_success_no_fallback(self):
        primary = _StubEmbedder("p", vec=[0.1] * 4)
        fallback = _StubEmbedder("f", vec=[0.9] * 4)
        ce = CompositeEmbedder(primary, fallback)
        assert ce.embed("x") == [0.1] * 4
        # Fallback never called → its dim stays unset.
        assert fallback.dim == 0
        # ce.dim should mirror primary.
        assert ce.dim == 4

    def test_primary_fails_fallback_succeeds(self):
        primary = _StubEmbedder("p", raises=EmbedderError("primary down"))
        fallback = _StubEmbedder("f", vec=[0.7] * 4)
        ce = CompositeEmbedder(primary, fallback)
        assert ce.embed("x") == [0.7] * 4
        assert fallback.dim == 4
        # dim mirrors fallback because primary never produced one.
        assert ce.dim == 4

    def test_both_fail_raises_fallback_error(self):
        primary = _StubEmbedder("p", raises=EmbedderError("primary down"))
        fallback = _StubEmbedder("f", raises=EmbedderError("fallback down too"))
        ce = CompositeEmbedder(primary, fallback)
        with pytest.raises(EmbedderError, match="fallback down too"):
            ce.embed("x")

    # ---- dim consistency ---------------------------------------------

    def test_dim_mismatch_after_failover_raises(self):
        primary = _StubEmbedder("p", vec=[0.0] * 1024)
        fallback = _StubEmbedder("f", vec=[0.0] * 768)
        ce = CompositeEmbedder(primary, fallback)
        # Prime primary's dim with one successful call.
        ce.embed("x")
        # Now force fallback to fire — its dim will differ.
        primary._raises = EmbedderError("primary now down")
        primary._vec = None
        with pytest.raises(EmbedderError, match="dim mismatch"):
            ce.embed("y")

    def test_matching_dims_no_raise(self):
        primary = _StubEmbedder("p", vec=[0.0] * 1024)
        fallback = _StubEmbedder("f", vec=[0.0] * 1024)
        ce = CompositeEmbedder(primary, fallback)
        ce.embed("x")
        primary._raises = EmbedderError("primary now down")
        primary._vec = None
        # Should not raise — dims match.
        out = ce.embed("y")
        assert len(out) == 1024

    # ---- batch --------------------------------------------------------

    def test_batch_primary_success(self):
        primary = _StubEmbedder("p", batch_vecs=[[1, 2], [3, 4]])
        fallback = _StubEmbedder("f", batch_vecs=[[9, 9], [9, 9]])
        ce = CompositeEmbedder(primary, fallback)
        assert ce.embed_batch(["a", "b"]) == [[1, 2], [3, 4]]
        assert fallback.dim == 0

    def test_batch_falls_back(self):
        primary = _StubEmbedder("p", raises=EmbedderError("down"))
        fallback = _StubEmbedder("f", batch_vecs=[[1, 1], [2, 2]])
        ce = CompositeEmbedder(primary, fallback)
        assert ce.embed_batch(["a", "b"]) == [[1, 1], [2, 2]]

    def test_batch_empty_short_circuits(self):
        # Neither side should be touched.
        primary = _StubEmbedder("p", raises=AssertionError("should not call"))
        fallback = _StubEmbedder("f", raises=AssertionError("should not call"))
        ce = CompositeEmbedder(primary, fallback)
        assert ce.embed_batch([]) == []

    # ---- factory ------------------------------------------------------

    def test_factory_composite(self):
        ce = make_embedder("composite", {
            "primary": "ollama",
            "primary_options": {"url": "http://x/api/embeddings",
                                "model": "qwen3-embedding:0.6b"},
            "fallback": "llamafile",
            "fallback_options": {"url": "http://127.0.0.1:38092/embedding"},
        })
        assert isinstance(ce, CompositeEmbedder)
        assert isinstance(ce.primary, OllamaEmbedder)
        assert isinstance(ce.fallback, LlamafileEmbedder)
        assert ce.fallback.url == "http://127.0.0.1:38092/embedding"

    def test_factory_composite_unknown_side_yields_null(self):
        ce = make_embedder("composite", {
            "primary": "unknown",
            "fallback": "ollama",
            "fallback_options": {},
        })
        assert isinstance(ce, CompositeEmbedder)
        assert isinstance(ce.primary, NullEmbedder)
        assert isinstance(ce.fallback, OllamaEmbedder)

    def test_factory_composite_log_fallback_flag(self):
        ce = make_embedder("composite", {
            "primary": "ollama", "fallback": "llamafile",
            "log_fallback": False,
        })
        assert isinstance(ce, CompositeEmbedder)
        assert ce.log_fallback is False

    def test_factory_llamafile_whitelist(self):
        # Unknown options are silently dropped (matches existing factory style).
        e = make_embedder("llamafile", {
            "url": "http://h:1/embedding",
            "timeout": 7.5,
            "max_chars": 50,
            "daemon_ensure": False,
            "ignored": "x",
        })
        assert isinstance(e, LlamafileEmbedder)
        assert e.url == "http://h:1/embedding"
        assert e.timeout == 7.5
        assert e.max_chars == 50
        assert e.daemon_ensure is False

    def test_factory_openai_compatible_unaffected(self):
        # Regression guard: the v1.4 factory changes must not break
        # the existing openai_compatible branch.
        e = make_embedder("openai_compatible", {"model": "text-embedding-3-small"})
        assert isinstance(e, OpenAiCompatibleEmbedder)
        assert e.model == "text-embedding-3-small"
