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

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts. Default loops single-shot ``embed``;
        subclasses with a real batch endpoint should override (the win is
        avoiding model reload + amortising HTTP round-trips).
        """
        return [self.embed(t) for t in texts]


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

    # Defaults assume a modern nomic/arctic/jina-class embedder with native
    # 8k-token training. Without ``num_ctx`` set, Ollama caps each request
    # at 2048 tokens and a Stop-hook turn summary easily overflows that,
    # returning HTTP 500. Setting ``num_ctx`` higher than the model's
    # *native trained* max gives no quality benefit (positions past the
    # trained range get untrained embeddings) and may degrade results,
    # so 8192 is the practical ceiling for the popular Ollama embedders.
    #
    # ``max_chars`` is a belt-and-suspenders truncation so pathologically
    # long inputs land within the token window even when tokenisation is
    # dense (code, paths). 16000 chars at the worst tested ~2 chars/token
    # ratio = 8000 tokens, fits 8k context. Realistic prose at 4 chars/token
    # is ~4000 tokens, leaving plenty of headroom.
    #
    # Override per-model via ``embedder_options`` in claude-hooks.json:
    #   minilm-l6-v2  (256-token native): num_ctx=256,  max_chars=400
    #   mxbai-embed-large-v1 (512 native): num_ctx=512, max_chars=1500
    DEFAULT_NUM_CTX: int = 8192
    DEFAULT_MAX_CHARS: int = 16000

    def __init__(
        self,
        url: str = "http://localhost:11434/api/embeddings",
        model: str = "nomic-embed-text",
        timeout: float = 10.0,
        max_chars: Optional[int] = None,
        num_ctx: Optional[int] = None,
        keep_alive: Optional[str] = None,
    ):
        self.url = url
        self.model = model
        self.timeout = timeout
        self.max_chars = max_chars if max_chars is not None else self.DEFAULT_MAX_CHARS
        self.num_ctx = num_ctx if num_ctx is not None else self.DEFAULT_NUM_CTX
        self.keep_alive = keep_alive

    def _options_payload(self) -> dict:
        out: dict = {}
        if self.num_ctx:
            out["options"] = {"num_ctx": self.num_ctx}
        if self.keep_alive:
            out["keep_alive"] = self.keep_alive
        return out

    def embed(self, text: str) -> list[float]:
        if not text:
            raise EmbedderError("cannot embed empty string")
        if self.max_chars:
            text = text[: self.max_chars]
        payload = {"model": self.model, "prompt": text, **self._options_payload()}
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=body,
            headers={"Content-Type": "application/json", "Connection": "close"},
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

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Use Ollama's /api/embed (input array) endpoint. Falls back to
        per-text /api/embeddings on HTTP error so older daemons still work."""
        if not texts:
            return []
        if self.max_chars:
            texts = [t[: self.max_chars] for t in texts]
        # Derive the batch endpoint from the configured per-text URL.
        batch_url = self.url
        if batch_url.endswith("/api/embeddings"):
            batch_url = batch_url[: -len("/api/embeddings")] + "/api/embed"
        payload = {"model": self.model, "input": texts, **self._options_payload()}
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            batch_url,
            data=body,
            headers={"Content-Type": "application/json", "Connection": "close"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError:
            return [self.embed(t) for t in texts]
        except (urllib.error.URLError, socket.timeout) as e:
            raise EmbedderError(f"ollama unreachable at {batch_url}: {e}")
        embs = data.get("embeddings")
        if not isinstance(embs, list) or len(embs) != len(texts):
            raise EmbedderError(
                f"ollama /api/embed returned {len(embs) if embs else 0} embeddings for {len(texts)} inputs"
            )
        if not self.dim and embs and isinstance(embs[0], list):
            self.dim = len(embs[0])
        return embs


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
        return self._call([text])[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """OpenAI-format /v1/embeddings already accepts ``input: string|array``
        — one call covers the whole batch."""
        if not texts:
            return []
        return self._call(texts)

    def _call(self, texts: list[str]) -> list[list[float]]:
        body = json.dumps({"model": self.model, "input": texts}).encode("utf-8")
        headers = {"Content-Type": "application/json", "Connection": "close"}
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
            embs = [item["embedding"] for item in data["data"]]
        except (KeyError, TypeError):
            raise EmbedderError(f"unexpected embeddings response shape: {str(data)[:200]}")
        if len(embs) != len(texts):
            raise EmbedderError(
                f"embeddings: server returned {len(embs)} for {len(texts)} inputs"
            )
        if not self.dim and embs:
            self.dim = len(embs[0])
        return embs


def make_embedder(name: str, options: Optional[dict] = None) -> Embedder:
    """
    Factory: build an embedder from a config name + options dict.
    Falls back to :class:`NullEmbedder` for unknown names.
    """
    options = options or {}
    if name == "ollama":
        allowed = ("url", "model", "timeout", "max_chars", "num_ctx", "keep_alive")
        return OllamaEmbedder(**{k: v for k, v in options.items() if k in allowed})
    if name in ("openai", "openai_compatible"):
        return OpenAiCompatibleEmbedder(
            **{k: v for k, v in options.items() if k in ("url", "model", "api_key", "timeout")}
        )
    return NullEmbedder()
