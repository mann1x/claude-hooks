"""
Embedder abstraction for providers that need to compute vectors locally
(pgvector, sqlite_vec). Qdrant and Memory KG do their own embedding
inside the MCP server, so they don't need this layer.

Five implementations:

- :class:`OllamaEmbedder` — talks to a local Ollama daemon over HTTP
  (``/api/embeddings``). Uses stdlib only.
- :class:`OpenAiCompatibleEmbedder` — for any OpenAI-compatible
  ``/v1/embeddings`` endpoint (LM Studio, vLLM, etc.). Stdlib only.
- :class:`LlamafileEmbedder` — talks to a llamafile/llama.cpp
  ``/embedding`` endpoint, optionally asking ``claude-hooks-daemon``
  to spawn the llamafile on demand. Stdlib only.
- :class:`CompositeEmbedder` — wraps two embedders as
  primary/fallback. On primary :class:`EmbedderError` it retries on
  the fallback (and raises a config-error if the two embedders return
  vectors of different dimension once both have produced one).
- :class:`NullEmbedder` — raises :class:`EmbedderError` on every call.
  The default for un-configured providers; produces a clear error if a
  scaffold provider is enabled without a working embedder.
"""

from __future__ import annotations

import json
import logging
import socket
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from typing import Optional

log = logging.getLogger("claude_hooks.embedders")


class EmbedderError(RuntimeError):
    """Raised when embedding a text fails."""


class ContextOverflowError(EmbedderError):
    """The input tokenised to more than the server's context window.

    Carries the server's own numbers so the caller can shrink by the
    exact ratio instead of guessing. ``n_ctx`` may be 0 when the server
    reported the overflow without a usable figure.
    """

    def __init__(self, message: str, *, n_prompt_tokens: int = 0, n_ctx: int = 0):
        super().__init__(message)
        self.n_prompt_tokens = n_prompt_tokens
        self.n_ctx = n_ctx


def _parse_context_overflow(code: int, body: str) -> Optional[tuple[int, int]]:
    """Recognise llama.cpp's over-context 400 and pull its numbers out.

    The server answers a too-long input with, verbatim:

        {"error": {"code": 400, "type": "exceed_context_size_error",
                   "message": "request (22275 tokens) exceeds the
                   available context size (16384 tokens), ...",
                   "n_prompt_tokens": 22275, "n_ctx": 16384}}

    Returns ``(n_prompt_tokens, n_ctx)``, or None when this is some
    other 400 that must not be retried.
    """
    if code != 400:
        return None
    try:
        err = (json.loads(body) or {}).get("error") or {}
    except (ValueError, AttributeError):
        return None
    if not isinstance(err, dict):
        return None
    is_overflow = (
        err.get("type") == "exceed_context_size_error"
        or "exceeds the available context size" in str(err.get("message", ""))
    )
    if not is_overflow:
        return None
    try:
        return int(err.get("n_prompt_tokens") or 0), int(err.get("n_ctx") or 0)
    except (TypeError, ValueError):
        return 0, 0


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
        num_gpu: Optional[int] = None,
    ):
        self.url = url
        self.model = model
        self.timeout = timeout
        self.max_chars = max_chars if max_chars is not None else self.DEFAULT_MAX_CHARS
        self.num_ctx = num_ctx if num_ctx is not None else self.DEFAULT_NUM_CTX
        self.keep_alive = keep_alive
        # ``num_gpu=N`` overrides the auto-detected GPU layer count.
        # Default is None = let Ollama decide. Rarely needed; primarily
        # useful when sharing a daemon with a much larger chat model
        # whose VRAM headroom you want to control explicitly.
        self.num_gpu = num_gpu

    def _options_payload(self) -> dict:
        out: dict = {}
        options: dict = {}
        if self.num_ctx:
            options["num_ctx"] = self.num_ctx
        if self.num_gpu is not None:
            options["num_gpu"] = self.num_gpu
        if options:
            out["options"] = options
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


class LlamafileEmbedder(Embedder):
    """
    Local llamafile / llama.cpp ``/embedding`` endpoint.

    The llamafile is supervised by ``claude-hooks-daemon`` (see
    :mod:`claude_hooks.embedding_manager`): the daemon lazily spawns
    it on the first ``ensure_running()`` ping, idle-reaps it after
    ``embedding.idle_timeout_seconds`` (default 3600; was 300 for
    Ollama ``OLLAMA_KEEP_ALIVE=5m`` parity, raised because each reap
    opens a respawn race that sessions report as a down embedder),
    and re-spawns on the next ping. The embedder optionally fires that ping itself
    via ``daemon_ensure=True`` (the default) so the supervision is
    transparent to the caller — the embedder behaves like a normal
    HTTP client and the warm/cold lifecycle is handled out of band.

    The HTTP shape is the llama.cpp server's ``POST /embedding`` API:
    request body ``{"content": "<text>"}``, response
    ``{"embedding": [float, ...]}``. Some llama.cpp builds wrap the
    response in a single-element list, others nest the vector inside
    a list-of-vectors (n-batch>1 shape). All three are handled here
    so the embedder works against whichever flavor the bundled
    llamafile build emits.
    """

    name = "llamafile"

    # llama.cpp server uses the model's native ctx by default and the
    # composite shipped with claude-hooks is built with ``--ctx-size
    # 16384`` baked in, so the per-request ctx is effectively
    # configured at spawn time by EmbeddingManager. The embedder still
    # truncates super-long inputs as a belt-and-suspenders measure;
    # see OllamaEmbedder for the rationale on the constants.
    DEFAULT_MAX_CHARS: int = 16000

    #: Attempts allowed for the shrink-and-retry loop in ``embed`` when
    #: the input overflows the server's context window. Each attempt
    #: costs a full round trip, but the ratio-based cut converges in one
    #: for every ratio observed so far (base64 1.35 chars/token and
    #: minified JSON 1.18 both fit on the second try from 30 000 chars).
    #: The budget is for pathological content where a single scaled cut
    #: undershoots, not for repeated blind halving.
    CTX_RETRY_ATTEMPTS: int = 4

    #: Margin applied on top of the server's own token ratio.
    #: Tokenisation is not linear in characters, so cutting to exactly
    #: ``n_ctx / n_prompt_tokens`` can land marginally over again if the
    #: removed tail was sparser than average.
    CTX_SHRINK_SAFETY: float = 0.92

    #: Fallback cut when the server reports an overflow but no usable
    #: token numbers. Deliberately aggressive — it only runs when the
    #: precise path is unavailable.
    CTX_BLIND_SHRINK: float = 0.6

    #: Token ceiling for a retry, *independent* of the context window.
    #:
    #: Fitting ``n_ctx`` is necessary but not sufficient. Verified live
    #: on solidpc 2026-07-25: a 30 000-char base64 payload was correctly
    #: cut to 20 299 chars (~15 000 tokens, comfortably inside the
    #: 16 384 window) — and then the retry itself ran past a 300 s
    #: timeout, because CPU embedding measures ~32 tok/s at 2 k tokens
    #: and degrades from there. Fitting the window but not the clock
    #: just trades a fast 400 for a slow timeout: same lost memory, many
    #: minutes later.
    #:
    #: Sized for the *slowest* host, not the reference one. Measured on
    #: solidpc (Ryzen 5 5600G, AVX2) at loadavg 16, a 3072-token retry
    #: took 126-141 s — inside the 180 s default timeout, but with
    #: almost no margin. Windows hosts run llamafile roughly 2× slower
    #: at byte-identical weights (an OS/build gap, not hardware), which
    #: would put 3072 tokens over the timeout on pandorum. 1536 lands
    #: near 60-70 s here and ~125-140 s there, so both stay inside the
    #: budget.
    #:
    #: Raise it only alongside ``embedder_options.timeout``. This
    #: truncation is a last resort for pathologically dense content —
    #: normal memories tokenise at 3.0-3.4 chars/token and never reach
    #: this path at all.
    CTX_RETRY_TOKEN_BUDGET: int = 1536

    def __init__(
        self,
        url: str = "http://127.0.0.1:38092/embedding",
        timeout: float = 180.0,
        max_chars: Optional[int] = None,
        daemon_ensure: bool = True,
    ):
        self.url = url
        self.timeout = timeout
        self.max_chars = (
            max_chars if max_chars is not None else self.DEFAULT_MAX_CHARS
        )
        self.daemon_ensure = daemon_ensure

    # ----------------------------------------------------------------
    # daemon-supervision hook
    # ----------------------------------------------------------------

    def _ensure_running(self) -> None:
        """Ask claude-hooks-daemon to spawn the llamafile if not up.

        Best-effort: if the daemon is down, the daemon_client function
        is missing (older install), or the ensure call raises, we fall
        through and try the HTTP request anyway. The HTTP request
        itself will surface a clean :class:`EmbedderError` if the
        llamafile really isn't reachable.

        Lazy import so this module stays stdlib-only at import time
        and tests can mock the daemon_client without dragging in the
        daemon's TCP socket setup.
        """
        if not self.daemon_ensure:
            return
        import importlib

        try:
            daemon_client = importlib.import_module("claude_hooks.daemon_client")
        except Exception:
            return
        fn = getattr(daemon_client, "embedding_ensure", None)
        if fn is None:
            # Daemon-client API hasn't been extended yet (v1.4
            # rolls this in via daemon.py / daemon_client.py
            # changes). Pre-rollout the embedder still works when
            # the llamafile has been started by hand.
            return
        try:
            fn()
        except Exception:
            return

    # ----------------------------------------------------------------
    # Embedder protocol
    # ----------------------------------------------------------------

    def embed(self, text: str) -> list[float]:
        """Embed ``text``, shrinking and retrying if it overflows ctx.

        ``max_chars`` is a *character* cap standing in for a token
        budget, and that substitution has a hidden assumption: with
        ``max_chars=30000`` against a 16384-token window it needs at
        least ~1.83 chars/token. Real memories measure 3.0-3.4 (code,
        paths and hashes tokenise densely), so the cap normally holds
        with room to spare — but base64 (1.35) and minified JSON (1.18)
        blow straight through it, and a store that fails here is a
        memory silently lost on the detached path.

        Rather than lower ``max_chars`` for everyone to suit content
        almost nobody stores, overflow is handled where it happens: the
        server reports both ``n_prompt_tokens`` and ``n_ctx``, so the
        text can be cut by that exact ratio and retried. Costs an extra
        round trip, but only for input that would otherwise have failed
        outright.
        """
        if not text:
            raise EmbedderError("cannot embed empty string")
        if self.max_chars:
            text = text[: self.max_chars]
        self._ensure_running()

        for attempt in range(self.CTX_RETRY_ATTEMPTS):
            try:
                return self._embed_once(text)
            except ContextOverflowError as e:
                shorter = self._shrink_for_ctx(text, e)
                if shorter is None or attempt == self.CTX_RETRY_ATTEMPTS - 1:
                    raise EmbedderError(
                        f"llamafile input exceeds context after "
                        f"{attempt + 1} attempt(s): {e}"
                    ) from e
                log.warning(
                    "llamafile: %d tokens exceeds ctx %d — retrying at "
                    "%d chars (was %d)",
                    e.n_prompt_tokens, e.n_ctx, len(shorter), len(text),
                )
                text = shorter
        raise EmbedderError("llamafile: context-overflow retry loop exhausted")

    def _shrink_for_ctx(
        self, text: str, err: ContextOverflowError,
    ) -> Optional[str]:
        """Cut ``text`` to fit ``err.n_ctx``, or None if it can't help.

        The target must satisfy *two* constraints, not one:

        1. Fit the context window (``n_ctx``, with a safety margin —
           tokenisation is not linear in characters, so the removed tail
           may have been denser or sparser than the average).
        2. Fit the clock (``CTX_RETRY_TOKEN_BUDGET``). A payload can sit
           inside the window and still take many minutes to embed on
           CPU, in which case the retry only moves the failure from a
           fast 400 to a slow timeout.

        The server's numbers give the observed chars/token for *this*
        text, which converts both budgets into characters directly.
        Returning None (no progress possible) is what stops a pointless
        second round trip on a degenerate response.
        """
        if err.n_prompt_tokens <= 0 or err.n_ctx <= 0:
            # No usable numbers: fall back to a flat cut so a server
            # that reports the overflow without figures still converges.
            target = int(len(text) * self.CTX_BLIND_SHRINK)
        else:
            chars_per_token = len(text) / float(err.n_prompt_tokens)
            budget_tokens = min(
                err.n_ctx * self.CTX_SHRINK_SAFETY,
                float(self.CTX_RETRY_TOKEN_BUDGET),
            )
            target = int(budget_tokens * chars_per_token)
        if target <= 0 or target >= len(text):
            return None
        return text[:target]

    def _embed_once(self, text: str) -> list[float]:
        payload = {"content": text}
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
            raw = e.read()[:400].decode("utf-8", "replace")
            overflow = _parse_context_overflow(e.code, raw)
            if overflow is not None:
                n_tok, n_ctx = overflow
                raise ContextOverflowError(
                    f"llamafile HTTP {e.code}: {raw[:200]}",
                    n_prompt_tokens=n_tok, n_ctx=n_ctx,
                )
            raise EmbedderError(f"llamafile HTTP {e.code}: {raw[:200]}")
        except (urllib.error.URLError, socket.timeout) as e:
            raise EmbedderError(f"llamafile unreachable at {self.url}: {e}")
        emb = self._extract_vector(data)
        if not self.dim:
            self.dim = len(emb)
        return emb

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """llama.cpp server accepts ``{"content": [str, ...]}`` for
        batched requests; falls back to per-text on any HTTP error so
        older / stripped llama.cpp builds still work."""
        if not texts:
            return []
        if self.max_chars:
            texts = [t[: self.max_chars] for t in texts]
        self._ensure_running()
        payload = {"content": texts}
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
        except urllib.error.HTTPError:
            return [self.embed(t) for t in texts]
        except (urllib.error.URLError, socket.timeout) as e:
            raise EmbedderError(f"llamafile unreachable at {self.url}: {e}")
        embs = self._extract_batch(data, expected=len(texts))
        if not self.dim and embs:
            self.dim = len(embs[0])
        return embs

    # ----------------------------------------------------------------
    # response-shape handling
    # ----------------------------------------------------------------

    @staticmethod
    def _extract_vector(data: object) -> list[float]:
        """Pull a single embedding out of any of the response shapes
        the llama.cpp server is known to emit:

        - Flat:           ``{"embedding": [f, f, ...]}``
        - List-wrapped:   ``[{"embedding": [f, f, ...]}]``
        - Double-nested:  ``{"embedding": [[f, f, ...]]}``  (n-batch=1)
        """
        if isinstance(data, list):
            if not data:
                raise EmbedderError("llamafile returned an empty list")
            data = data[0]
        if not isinstance(data, dict):
            raise EmbedderError(
                f"llamafile returned unexpected response type: {type(data).__name__}"
            )
        emb = data.get("embedding")
        if not isinstance(emb, list) or not emb:
            raise EmbedderError("llamafile returned no embedding")
        if isinstance(emb[0], list):
            emb = emb[0]
        if not isinstance(emb[0], (int, float)):
            raise EmbedderError(
                f"llamafile returned non-numeric vector element: {type(emb[0]).__name__}"
            )
        return list(emb)

    @staticmethod
    def _extract_batch(data: object, *, expected: int) -> list[list[float]]:
        """Pull a list of embeddings out of a batch response. The
        server returns ``[{"embedding": [...]}, ...]`` with one entry
        per input. If the embedder hit the older shape that doesn't
        support batching, the caller will already have raised at HTTP
        level and fallen back to per-text; this only handles the
        success path."""
        if isinstance(data, dict):
            # Some builds wrap the list under "data" or "embeddings".
            if "embeddings" in data and isinstance(data["embeddings"], list):
                data = data["embeddings"]
            elif "data" in data and isinstance(data["data"], list):
                data = data["data"]
            elif "embedding" in data:
                # Single embedding returned for a batched request —
                # treat as one row, let the length check below catch
                # the mismatch.
                data = [data]
        if not isinstance(data, list):
            raise EmbedderError(
                f"llamafile batch returned unexpected type: {type(data).__name__}"
            )
        out: list[list[float]] = []
        for item in data:
            if isinstance(item, list):
                # Bare list of floats, no wrapping dict.
                vec = item
                if vec and isinstance(vec[0], list):
                    vec = vec[0]
                out.append(list(vec))
                continue
            if not isinstance(item, dict):
                raise EmbedderError(
                    f"llamafile batch entry is not a dict: {type(item).__name__}"
                )
            emb = item.get("embedding")
            if not isinstance(emb, list) or not emb:
                raise EmbedderError("llamafile batch entry missing embedding")
            if isinstance(emb[0], list):
                emb = emb[0]
            out.append(list(emb))
        if len(out) != expected:
            raise EmbedderError(
                f"llamafile batch returned {len(out)} embeddings for {expected} inputs"
            )
        return out


class CompositeEmbedder(Embedder):
    """
    Primary + fallback wrapper. ``embed()`` tries the primary first;
    on :class:`EmbedderError` it retries on the fallback. Used to wire
    ``Ollama → llamafile`` (the v1.4 default) or
    ``OpenAI-compatible → llamafile``.

    Dim consistency is checked once both embedders have produced a
    vector. If they disagree (e.g. someone wired Ollama
    ``qwen3-embedding:0.6b`` (1024-dim) against a llamafile built for
    ``nomic-embed-text`` (768-dim)), the next fallback call raises an
    :class:`EmbedderError` — the vector space would be silently
    corrupted otherwise, and pgvector / sqlite_vec store rows mixing
    the two would never compare correctly. This matches the
    primary/fallback idiom used by :mod:`claude_hooks.hyde`.
    """

    name = "composite"

    def __init__(
        self,
        primary: Embedder,
        fallback: Embedder,
        log_fallback: bool = True,
    ):
        self.primary = primary
        self.fallback = fallback
        self.log_fallback = log_fallback

    @property
    def dim(self) -> int:  # type: ignore[override]
        # Prefer primary's dim; fall back to whichever's known.
        return self.primary.dim or self.fallback.dim

    @dim.setter
    def dim(self, value: int) -> None:  # type: ignore[override]
        # Embedder base class declares ``dim`` as a class attribute, so
        # the ABC ``__init__`` doesn't set an instance value. We treat
        # the property as read-through to ``primary``/``fallback`` to
        # keep the attribute behavior stable; assigning is a no-op.
        return

    def _check_dim_consistency(self) -> None:
        if self.primary.dim and self.fallback.dim and self.primary.dim != self.fallback.dim:
            raise EmbedderError(
                "composite embedder dim mismatch: primary "
                f"{self.primary.name}={self.primary.dim} vs fallback "
                f"{self.fallback.name}={self.fallback.dim}. The two "
                "embedders must produce the same vector dimension or "
                "the recall vector space will be corrupted on failover."
            )

    def embed(self, text: str) -> list[float]:
        try:
            vec = self.primary.embed(text)
        except EmbedderError as e_primary:
            if self.log_fallback:
                # Stdlib logging is set up by daemon / hook entrypoints.
                import logging

                logging.getLogger("claude_hooks.embedders").warning(
                    "primary embedder %s failed (%s); falling back to %s",
                    self.primary.name, e_primary, self.fallback.name,
                )
            vec = self.fallback.embed(text)
            self._check_dim_consistency()
            return vec
        # Primary succeeded — still verify dim consistency once we
        # have both. The fallback's dim is only set after it's been
        # called once, so this is a no-op until the first failover.
        self._check_dim_consistency()
        return vec

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            vecs = self.primary.embed_batch(texts)
        except EmbedderError as e_primary:
            if self.log_fallback:
                import logging

                logging.getLogger("claude_hooks.embedders").warning(
                    "primary embedder %s batch failed (%s); falling "
                    "back to %s", self.primary.name, e_primary,
                    self.fallback.name,
                )
            vecs = self.fallback.embed_batch(texts)
            self._check_dim_consistency()
            return vecs
        self._check_dim_consistency()
        return vecs


def make_embedder(name: str, options: Optional[dict] = None) -> Embedder:
    """
    Factory: build an embedder from a config name + options dict.
    Falls back to :class:`NullEmbedder` for unknown names.

    For ``"composite"`` the options dict carries nested embedder specs:
    ``{"primary": "ollama", "primary_options": {...},
       "fallback": "llamafile", "fallback_options": {...}}``.
    Either side may itself be ``"composite"`` for arbitrary chaining,
    though the installer only generates one level.
    """
    options = options or {}
    if name == "ollama":
        allowed = ("url", "model", "timeout", "max_chars", "num_ctx",
                   "keep_alive", "num_gpu")
        return OllamaEmbedder(**{k: v for k, v in options.items() if k in allowed})
    if name in ("openai", "openai_compatible"):
        return OpenAiCompatibleEmbedder(
            **{k: v for k, v in options.items() if k in ("url", "model", "api_key", "timeout")}
        )
    if name == "llamafile":
        allowed = ("url", "timeout", "max_chars", "daemon_ensure")
        return LlamafileEmbedder(**{k: v for k, v in options.items() if k in allowed})
    if name == "composite":
        primary_name = options.get("primary") or "null"
        fallback_name = options.get("fallback") or "null"
        primary = make_embedder(primary_name, options.get("primary_options") or {})
        fallback = make_embedder(fallback_name, options.get("fallback_options") or {})
        return CompositeEmbedder(
            primary=primary,
            fallback=fallback,
            log_fallback=bool(options.get("log_fallback", True)),
        )
    return NullEmbedder()
