"""Shared chat-completion helper for HyDE / reflect / consolidate.

Pre-v1.5, each of those three modules rolled its own
``urllib.request.urlopen`` against Ollama's ``/api/generate``.
v1.5 introduces a ``llamafile://<label>`` identifier that routes
to a daemon-supervised local llamafile speaking OpenAI's
``/v1/chat/completions``. Rather than triple-duplicate the
prefix-parsing + wire-format-switch logic, this module centralises
it.

What this module is NOT
-----------------------

This is **not** the unified ChatClient for /get-advice and
/consultants. Those two have a richer ``chat(payload) -> dict``
interface (tool calls, message history, streaming) and live in
their own modules (`get_advice/chat_client.py`,
`consultants/server/runner.py`). The v1.5 plan calls for a factory
shim at *those* call sites that returns a backend-appropriate
client; this module's :func:`make_chat_client` is reused by that
factory but the simpler :func:`call` API here is shaped for the
``{prompt, system}`` flows of HyDE / reflect / consolidate.

Wire formats
------------

Both clients speak the same in-memory ``(prompt, system)`` API but
serialise differently:

- :class:`OllamaChatClient` -> POST ``/api/generate``
  with ``{model, system, prompt, stream:false, think:false,
  keep_alive, options:{num_predict, num_ctx}}``. Response:
  ``{response: "..."}``. **Unchanged from the pre-v1.5 helpers** —
  this client is the existing logic, just extracted.

- :class:`LlamafileChatClient` -> POST
  ``/v1/chat/completions`` (OpenAI shape) on the daemon-resolved
  port. Request: ``{model: label, messages:[{role:system,...},
  {role:user,...}], stream:false, max_tokens, temperature}``.
  Response: ``{choices:[{message:{content:"..."}}]}``.

Error contract
--------------

:func:`call` returns ``""`` on any failure (matches the
pre-v1.5 contract — HyDE / reflect / consolidate all fall back
gracefully to the raw prompt or skip the LLM step).

The underlying client classes raise :class:`ChatBackendError` on
the same failures so callers that want to surface errors (e.g.
a future tool-using path) can opt in.
"""

from __future__ import annotations

import json
import logging
import socket
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from typing import Optional

log = logging.getLogger("claude_hooks.chat_backend")


_PROVIDER_PREFIX = "llamafile://"
_DEFAULT_OLLAMA_GENERATE = "http://localhost:11434/api/generate"


class ChatBackendError(Exception):
    """All chat-backend failures (connect refused, HTTP non-2xx,
    timeout, malformed response). Subclassed for routing-time errors
    that the CLI may want to surface verbatim."""


class UnknownBackendError(ChatBackendError):
    """Model identifier had an unrecognised scheme (e.g. ``foo://``)
    or a llamafile ref pointing at an unregistered label."""


# --------------------------------------------------------------------- #
# Reference parsing
# --------------------------------------------------------------------- #

def parse_model_ref(ref: str) -> tuple[str, str]:
    """Returns ``(backend, target)``.

    Examples:
        ``"llamafile://gemma"``  -> ``("llamafile", "gemma")``
        ``"qwen:cloud"``         -> ``("ollama", "qwen:cloud")``
        ``"gemma4-e4b"``         -> ``("ollama", "gemma4-e4b")``

    The Ollama path is intentionally permissive: anything not
    starting with a known prefix is passed verbatim to Ollama,
    which interprets its own suffix conventions (``:cloud``,
    ``:latest``, etc.).
    """
    if ref.startswith(_PROVIDER_PREFIX):
        return ("llamafile", ref[len(_PROVIDER_PREFIX):])
    return ("ollama", ref)


# --------------------------------------------------------------------- #
# Client ABC
# --------------------------------------------------------------------- #

class ChatClient(ABC):
    """Pinhole API used by HyDE / reflect / consolidate."""

    name: str = "base"

    @abstractmethod
    def generate(
        self,
        user_prompt: str,
        system_prompt: str,
        model: str,
        *,
        timeout: float,
        max_tokens: int,
        num_ctx: int,
        keep_alive: str = "15m",
    ) -> str:
        """Return the LLM's response text, or raise
        :class:`ChatBackendError` on any failure (network, HTTP,
        malformed body, empty response). Empty-string returns
        indicate the model produced nothing — that's not a backend
        error, it's a content outcome."""


# --------------------------------------------------------------------- #
# Ollama client (extracted from pre-v1.5 hyde/reflect/consolidate)
# --------------------------------------------------------------------- #

class OllamaChatClient(ChatClient):
    """POSTs to Ollama's native ``/api/generate``. Same wire format
    HyDE / reflect / consolidate have used since v0.2.
    """

    name = "ollama"

    def __init__(self, url: str = _DEFAULT_OLLAMA_GENERATE):
        self.url = url

    def generate(
        self,
        user_prompt: str,
        system_prompt: str,
        model: str,
        *,
        timeout: float = 30.0,
        max_tokens: int = 150,
        num_ctx: int = 16384,
        keep_alive: str = "15m",
    ) -> str:
        options: dict = {"num_predict": max_tokens}
        if num_ctx and num_ctx > 0:
            options["num_ctx"] = int(num_ctx)
        body = json.dumps({
            "model": model,
            "system": system_prompt,
            "prompt": user_prompt,
            "stream": False,
            "think": False,
            "keep_alive": keep_alive,
            "options": options,
        }).encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError,
                socket.timeout, OSError) as e:
            raise ChatBackendError(f"ollama call failed: {e}") from e
        except (ValueError, json.JSONDecodeError) as e:
            raise ChatBackendError(f"ollama malformed response: {e}") from e
        return (data.get("response") or "").strip()


# --------------------------------------------------------------------- #
# Llamafile client (talks /v1/chat/completions on a daemon-resolved port)
# --------------------------------------------------------------------- #

class LlamafileChatClient(ChatClient):
    """Daemon-ensured chat llamafile, OpenAI ``/v1/chat/completions``.

    On every call: ask the daemon to ensure the model for ``label``
    is loaded (returns the port), cache the port for
    ``port_cache_ttl`` seconds, POST to that port. On a connection
    refusal mid-call (the daemon may have idle-reaped between our
    last ensure and this call), invalidate the cache, re-ensure
    once, retry.
    """

    name = "llamafile"

    def __init__(
        self,
        label: str,
        *,
        host: str = "127.0.0.1",
        port_cache_ttl: float = 60.0,
        daemon_ensure: bool = True,
    ):
        if not label:
            raise UnknownBackendError("llamafile label is required")
        self.label = label
        self.host = host
        self.port_cache_ttl = port_cache_ttl
        self.daemon_ensure = daemon_ensure
        self._cached_port: Optional[int] = None
        self._cached_at: float = 0.0

    # --- daemon-ensure / port resolution ---

    def _resolve_port(self, *, force_refresh: bool = False) -> int:
        """Return the live port for ``self.label`` via daemon RPC.

        Caches the result for ``port_cache_ttl`` so steady traffic
        doesn't pay the RPC round-trip on every embed. Cold cache
        or forced refresh hits ``daemon_client.chat_model_ensure``,
        which spawns the model if needed (cold-start latency lives
        in this call)."""
        import time
        now = time.monotonic()
        if (not force_refresh
                and self._cached_port is not None
                and (now - self._cached_at) < self.port_cache_ttl):
            return self._cached_port

        if not self.daemon_ensure:
            raise UnknownBackendError(
                "LlamafileChatClient.daemon_ensure=False but no "
                "static port supplied — set daemon_ensure=True or "
                "pass an explicit port"
            )

        # Lazy import — keeps the module importable in test contexts
        # that don't have the daemon_client wired up.
        import importlib
        dc = importlib.import_module("claude_hooks.daemon_client")
        try:
            resp = dc.chat_model_ensure(self.label, timeout=120.0)
        except Exception as e:
            raise ChatBackendError(
                f"daemon RPC chat_model_ensure({self.label!r}) failed: {e}"
            ) from e
        if resp is None:
            raise ChatBackendError(
                f"daemon unreachable; cannot resolve "
                f"llamafile://{self.label}"
            )
        if not resp.get("ready") or "port" not in resp:
            reason = resp.get("reason", "unknown")
            raise ChatBackendError(
                f"daemon refused to bring up "
                f"llamafile://{self.label}: {reason}"
            )
        self._cached_port = int(resp["port"])
        self._cached_at = now
        return self._cached_port

    # --- generate ---

    def generate(
        self,
        user_prompt: str,
        system_prompt: str,
        model: str,
        *,
        timeout: float = 30.0,
        max_tokens: int = 150,
        num_ctx: int = 16384,
        keep_alive: str = "15m",
    ) -> str:
        # ``model`` is the registry label. llama.cpp's server uses
        # it as the response's ``model`` field and otherwise ignores
        # it (the actual GGUF is what was loaded at spawn). We pass
        # it verbatim so logs / proxies can correlate.
        port = self._resolve_port()
        try:
            return self._post(port, user_prompt, system_prompt, model,
                              timeout, max_tokens)
        except ChatBackendError as first_err:
            # The daemon may have idle-reaped between our last
            # ensure and this POST. Invalidate the port cache,
            # re-ensure once, retry. Surface the original error if
            # the retry also fails (helps debugging).
            log.info(
                "llamafile %s first POST failed (%s); re-ensuring + retrying",
                self.label, first_err,
            )
            port = self._resolve_port(force_refresh=True)
            try:
                return self._post(port, user_prompt, system_prompt, model,
                                  timeout, max_tokens)
            except ChatBackendError as retry_err:
                raise ChatBackendError(
                    f"llamafile {self.label!r} call failed after retry: "
                    f"{retry_err} (first attempt: {first_err})"
                ) from retry_err

    def _post(
        self, port: int, user_prompt: str, system_prompt: str,
        model: str, timeout: float, max_tokens: int,
    ) -> str:
        url = f"http://{self.host}:{port}/v1/chat/completions"
        body = json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "max_tokens": int(max_tokens),
        }).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json",
                     "Connection": "close"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError,
                socket.timeout, OSError) as e:
            raise ChatBackendError(f"llamafile call failed: {e}") from e
        except (ValueError, json.JSONDecodeError) as e:
            raise ChatBackendError(
                f"llamafile malformed response: {e}"
            ) from e
        # OpenAI shape: choices[0].message.content
        choices = data.get("choices") or []
        if not choices or not isinstance(choices, list):
            return ""
        msg = (choices[0].get("message") or {})
        return (msg.get("content") or "").strip()


# --------------------------------------------------------------------- #
# Factory + one-shot helper
# --------------------------------------------------------------------- #

def make_chat_client(
    model_ref: str,
    *,
    ollama_url: str = _DEFAULT_OLLAMA_GENERATE,
    host: str = "127.0.0.1",
    port_cache_ttl: float = 60.0,
    daemon_ensure: bool = True,
) -> tuple[ChatClient, str]:
    """Pick the right backend for ``model_ref`` and return
    ``(client, resolved_target)``.

    ``resolved_target`` is what the client expects as the ``model``
    parameter to :meth:`ChatClient.generate`:
      - Ollama refs: the original string (the ``:cloud`` suffix
        and other Ollama conventions are preserved).
      - llamafile refs: just the label (the ``llamafile://``
        prefix is stripped).
    """
    backend, target = parse_model_ref(model_ref)
    if backend == "ollama":
        return OllamaChatClient(url=ollama_url), target
    if backend == "llamafile":
        return (
            LlamafileChatClient(
                label=target, host=host,
                port_cache_ttl=port_cache_ttl,
                daemon_ensure=daemon_ensure,
            ),
            target,
        )
    raise UnknownBackendError(
        f"unrecognised model identifier: {model_ref!r} "
        "(expected bare name, ':cloud' suffix, or 'llamafile://<label>')"
    )


def call(
    user_prompt: str,
    system_prompt: str,
    model_ref: str,
    *,
    ollama_url: str = _DEFAULT_OLLAMA_GENERATE,
    timeout: float = 30.0,
    max_tokens: int = 150,
    num_ctx: int = 16384,
    keep_alive: str = "15m",
    raise_on_error: bool = False,
) -> str:
    """One-shot chat-completion helper used by HyDE / reflect /
    consolidate.

    Default behaviour is graceful: any backend error returns ``""``
    (matches the pre-v1.5 contract of the three callers, all of
    whom degrade to "skip the LLM step" on failure). Pass
    ``raise_on_error=True`` to opt into structured errors — useful
    in tests or when a caller wants to fall over to a different
    backend itself.
    """
    try:
        client, resolved = make_chat_client(model_ref, ollama_url=ollama_url)
    except UnknownBackendError as e:
        if raise_on_error:
            raise
        log.warning("chat_backend: %s", e)
        return ""
    try:
        out = client.generate(
            user_prompt=user_prompt,
            system_prompt=system_prompt,
            model=resolved,
            timeout=timeout,
            max_tokens=max_tokens,
            num_ctx=num_ctx,
            keep_alive=keep_alive,
        )
    except ChatBackendError as e:
        if raise_on_error:
            raise
        log.debug("chat_backend call failed: %s", e)
        return ""
    return out
