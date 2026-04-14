"""
Ollama HTTP mocking helpers.

Patches ``urllib.request.urlopen`` inside a target module so the
module's calls to the Ollama generate / embeddings endpoints return
fixtures instead of hitting the network.

Each helper is a context manager that can be layered (different
responses for the same module on successive calls) via ``side_effect``.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from io import BytesIO
from unittest.mock import patch


class _FakeHttpResponse:
    """Duck-types the ``http.client.HTTPResponse`` interface we use."""

    def __init__(self, payload: bytes, status: int = 200):
        self._buf = BytesIO(payload)
        self.status = status

    def read(self, *args, **kwargs) -> bytes:
        return self._buf.read(*args, **kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self._buf.close()


def _fake_response(obj) -> _FakeHttpResponse:
    return _FakeHttpResponse(json.dumps(obj).encode("utf-8"))


@contextmanager
def mock_ollama_generate(
    response_text: str = "",
    *,
    target: str = "claude_hooks.hyde",
    fail: bool = False,
):
    """Patch ``urllib.request.urlopen`` inside ``target`` module.

    When patched, any Ollama ``/api/generate`` call returns a JSON body
    of shape ``{"response": <response_text>}``.

    :param target: dotted module path that imports ``urllib.request``.
    :param fail: if True, ``urlopen`` raises ConnectionRefusedError.
    """
    import urllib.error

    if fail:
        side_effect = ConnectionRefusedError("simulated")
        with patch(f"{target}.urllib.request.urlopen", side_effect=side_effect) as m:
            yield m
        return

    def _handler(*args, **kwargs):
        return _fake_response({"response": response_text})

    with patch(f"{target}.urllib.request.urlopen", side_effect=_handler) as m:
        yield m


@contextmanager
def mock_ollama_embeddings(
    vector: list[float],
    *,
    target: str = "claude_hooks.embedders",
    fail: bool = False,
):
    """Same shape as :func:`mock_ollama_generate` but for ``/api/embeddings``."""
    if fail:
        with patch(
            f"{target}.urllib.request.urlopen",
            side_effect=ConnectionRefusedError("simulated"),
        ) as m:
            yield m
        return

    def _handler(*args, **kwargs):
        # Support both Ollama response shapes:
        #   /api/embeddings → {"embedding": [...]}
        #   /api/embed      → {"embeddings": [[...]]}
        return _fake_response({"embedding": vector, "embeddings": [vector]})

    with patch(f"{target}.urllib.request.urlopen", side_effect=_handler) as m:
        yield m
