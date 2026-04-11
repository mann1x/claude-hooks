"""
Ollama embedding provider with optional FastEmbed failover.

Calls Ollama's /api/embed endpoint over HTTP. The vector name and size are
inherited from a paired ``FastEmbedProvider`` so that the resulting vectors
are written under the same Qdrant vector name as the existing collection
(e.g. ``fast-all-minilm-l6-v2``). This is what makes a drop-in switch from
fastembed → Ollama possible without re-embedding the collection — provided
the Ollama model produces vectors in the *same* embedding space as fastembed.

For ``sentence-transformers/all-MiniLM-L6-v2``, the model
``locusai/all-minilm-l6-v2`` (fp32 GGUF) on Ollama matches fastembed's ONNX
output to ~6 decimal places of cosine similarity (0.999998+). Other tag
variants like ``all-minilm:33m`` are *not* compatible — they share the
architecture but produce a different vector space (~0.5 similarity).

If ``fallback_to_fastembed`` is True, transient Ollama failures will silently
fall through to the paired FastEmbedProvider so that recall keeps working
when Ollama is down or restarting.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request

from mcp_server_qdrant.embeddings.base import EmbeddingProvider
from mcp_server_qdrant.embeddings.fastembed import FastEmbedProvider

log = logging.getLogger("mcp_server_qdrant.embeddings.ollama")


class OllamaEmbeddingProvider(EmbeddingProvider):
    """Ollama-backed embedding provider with optional FastEmbed failover.

    :param ollama_url: Base URL of the Ollama server (e.g. http://host:11434).
    :param ollama_model: Tag of the Ollama embedding model.
    :param fastembed_model: FastEmbed model name. Used to derive vector name
        and size, AND as the failover backend if ``fallback_to_fastembed``
        is True. Must be in the same embedding space as ``ollama_model``.
    :param keep_alive: How long Ollama should keep the model resident.
    :param timeout: Per-request HTTP timeout.
    :param fallback_to_fastembed: If True, transient Ollama errors fall
        through to the paired FastEmbedProvider.
    """

    def __init__(
        self,
        ollama_url: str,
        ollama_model: str,
        fastembed_model: str,
        keep_alive: str = "15m",
        timeout: float = 10.0,
        fallback_to_fastembed: bool = True,
    ):
        self.ollama_url = ollama_url.rstrip("/")
        self.ollama_model = ollama_model
        self.keep_alive = keep_alive
        self.timeout = timeout
        self.fallback_to_fastembed = fallback_to_fastembed
        # Always create the fastembed provider — we use it for vector
        # name/size derivation, and (optionally) as the failover backend.
        # If you don't want fastembed installed at all, that would require
        # a separate "ollama-only" mode that hardcodes vector name/size,
        # which we don't currently support.
        self._fastembed = FastEmbedProvider(fastembed_model)

    # ------------------------------------------------------------------ #
    # Vector metadata — delegated to fastembed for collection compat
    # ------------------------------------------------------------------ #
    def get_vector_name(self) -> str:
        return self._fastembed.get_vector_name()

    def get_vector_size(self) -> int:
        return self._fastembed.get_vector_size()

    # ------------------------------------------------------------------ #
    # Embedding calls
    # ------------------------------------------------------------------ #
    async def embed_query(self, query: str) -> list[float]:
        try:
            return await self._ollama_embed_one(query)
        except Exception as e:
            if self.fallback_to_fastembed:
                log.warning("ollama embed_query failed, falling back to fastembed: %s", e)
                return await self._fastembed.embed_query(query)
            raise

    async def embed_documents(self, documents: list[str]) -> list[list[float]]:
        try:
            return await self._ollama_embed_many(documents)
        except Exception as e:
            if self.fallback_to_fastembed:
                log.warning("ollama embed_documents failed, falling back to fastembed: %s", e)
                return await self._fastembed.embed_documents(documents)
            raise

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    async def _ollama_embed_one(self, text: str) -> list[float]:
        loop = asyncio.get_event_loop()
        vecs = await loop.run_in_executor(None, self._sync_embed, [text])
        # Unwrap: embed_query returns a single vector, not a list of vectors.
        return vecs[0]

    async def _ollama_embed_many(self, texts: list[str]) -> list[list[float]]:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._sync_embed, texts)

    def _sync_embed(self, texts: list[str]) -> list[list[float]]:
        """Always returns a list with one vector per input text."""
        body = json.dumps({
            "model": self.ollama_model,
            "input": texts,  # Ollama accepts a list even for single inputs
            "keep_alive": self.keep_alive,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{self.ollama_url}/api/embed",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        embeddings = data.get("embeddings")
        if not embeddings or not isinstance(embeddings, list):
            raise RuntimeError(f"ollama returned no embeddings: {data}")
        if not isinstance(embeddings[0], list):
            raise RuntimeError(f"unexpected embeddings shape: {type(embeddings[0])}")
        if len(embeddings) != len(texts):
            raise RuntimeError(
                f"ollama returned {len(embeddings)} embeddings for {len(texts)} inputs"
            )
        return embeddings
