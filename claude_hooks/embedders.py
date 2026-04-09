"""
Embedder abstraction for providers that need to compute vectors locally
(pgvector, sqlite_vec). Qdrant and Memory KG do their own embedding
inside the MCP server, so they don't need this layer.

Three implementations:

- :class:`OllamaEmbedder` — talks to a local Ollama daemon over HTTP
  (``/api/embeddings``). Uses stdlib only.
- :class:`OpenAiCompatibleEmbedder` — for any OpenAI-compatible
  ``/v1/embeddings`` endpoint (LM Studio, vLLM, etc.). Stdlib only.
- :class:`NullEmbedder` — raises :class:`EmbedderError` on every call.
  The default for un-configured providers; produces a clear error if a
  scaffold provider is enabled without a working embedder.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from typing import Optional


class EmbedderError(RuntimeError):
    """Raised when embedding a text fails."""


class Embedder(ABC):
    name: str = ""
    dim: int = 0

    @abstractmethod
    def embed(self, text: str) -> list[float]:
        """Return a vector embedding of ``text``."""


class NullEmbedder(Embedder):
    """Always raises. Used as a placeholder when no embedder is configured."""

    name = "null"

    def embed(self, text: str) -> list[float]:
        raise EmbedderError(
            "no embedder configured — set providers.<name>.embedder in claude-hooks.json"
        )


class OllamaEmbedder(Embedder):
    """
    Local Ollama embedder. Uses ``POST /api/embeddings``.

    Default model: ``nomic-embed-text`` (768 dim, fast, good for memory).
    Run ``ollama pull nomic-embed-text`` first.
    """

    name = "ollama"

    def __init__(
        self,
        url: str = "http://localhost:11434/api/embeddings",
        model: str = "nomic-embed-text",
        timeout: float = 10.0,
    ):
        self.url = url
        self.model = model
        self.timeout = timeout

    def embed(self, text: str) -> list[float]:
        if not text:
            raise EmbedderError("cannot embed empty string")
        body = json.dumps({"model": self.model, "prompt": text}).encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise EmbedderError(f"ollama HTTP {e.code}: {e.read()[:200].decode('utf-8', 'replace')}")
        except (urllib.error.URLError, socket.timeout) as e:
            raise EmbedderError(f"ollama unreachable at {self.url}: {e}")
        emb = data.get("embedding")
        if not isinstance(emb, list) or not emb:
            raise EmbedderError(f"ollama returned no embedding for model {self.model}")
        if not self.dim:
            self.dim = len(emb)
        return emb


class OpenAiCompatibleEmbedder(Embedder):
    """
    Any OpenAI-compatible ``/v1/embeddings`` endpoint. Works with LM Studio,
    vLLM, llama-server, OpenRouter, etc.
    """

    name = "openai_compatible"

    def __init__(
        self,
        url: str = "http://localhost:1234/v1/embeddings",
        model: str = "text-embedding-3-small",
        api_key: Optional[str] = None,
        timeout: float = 10.0,
    ):
        self.url = url
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    def embed(self, text: str) -> list[float]:
        if not text:
            raise EmbedderError("cannot embed empty string")
        body = json.dumps({"model": self.model, "input": text}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise EmbedderError(f"embeddings HTTP {e.code}: {e.read()[:200].decode('utf-8', 'replace')}")
        except (urllib.error.URLError, socket.timeout) as e:
            raise EmbedderError(f"embeddings endpoint unreachable: {e}")
        try:
            emb = data["data"][0]["embedding"]
        except (KeyError, IndexError, TypeError):
            raise EmbedderError(f"unexpected embeddings response shape: {str(data)[:200]}")
        if not self.dim:
            self.dim = len(emb)
        return emb


def make_embedder(name: str, options: Optional[dict] = None) -> Embedder:
    """
    Factory: build an embedder from a config name + options dict.
    Falls back to :class:`NullEmbedder` for unknown names.
    """
    options = options or {}
    if name == "ollama":
        return OllamaEmbedder(**{k: v for k, v in options.items() if k in ("url", "model", "timeout")})
    if name in ("openai", "openai_compatible"):
        return OpenAiCompatibleEmbedder(
            **{k: v for k, v in options.items() if k in ("url", "model", "api_key", "timeout")}
        )
    return NullEmbedder()
