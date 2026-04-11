"""
HyDE (Hypothetical Document Embeddings) query expansion.

Before searching Qdrant, generates a short hypothetical answer to the user's
prompt using a local Ollama model. The hypothetical answer is then used as
the search query, which produces dramatically better vector matches because
it's in the same "answer space" as the stored memories.

Falls back gracefully to the original prompt if Ollama is unavailable.
"""

from __future__ import annotations

import json
import logging
import socket
import urllib.error
import urllib.request

log = logging.getLogger("claude_hooks.hyde")

_SYSTEM_PROMPT = (
    "You are a memory recall assistant. Given a user's question, write a short, "
    "factual answer as if it were a stored memory entry. Be specific and use "
    "technical terms. Do not explain, just state facts. Maximum 2-3 sentences."
)


def expand_query(
    prompt: str,
    *,
    model: str = "qwen3.5:2b",
    fallback_model: str = "gemma4:e2b",
    url: str = "http://localhost:11434/api/generate",
    timeout: float = 30.0,
    max_tokens: int = 150,
    keep_alive: str = "15m",
) -> str:
    """
    Generate a hypothetical answer to use as a vector search query.
    Returns the original prompt on any failure.
    """
    if not prompt.strip():
        return prompt

    # Try primary model first, then fallback.
    for m in [model, fallback_model]:
        result = _call_ollama(prompt, model=m, url=url, timeout=timeout, max_tokens=max_tokens, keep_alive=keep_alive)
        if result:
            log.debug("hyde expanded with %s: %s", m, result[:80])
            return result

    log.debug("hyde: all models failed, using raw prompt")
    return prompt


def _call_ollama(
    prompt: str,
    *,
    model: str,
    url: str,
    timeout: float,
    max_tokens: int,
    keep_alive: str = "-1",
) -> str:
    """Call Ollama generate API. Returns the response text or empty string on failure.

    keep_alive: how long Ollama should keep the model resident after this call.
    "15m" = stays loaded for 15 minutes after last use. "-1" = never unload.
    """
    body = json.dumps({
        "model": model,
        "system": _SYSTEM_PROMPT,
        "prompt": prompt,
        "stream": False,
        "think": False,
        "keep_alive": keep_alive,
        "options": {"num_predict": max_tokens},
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout, OSError):
        return ""
    except Exception:
        return ""

    response = (data.get("response") or "").strip()
    if not response or len(response) < 10:
        return ""
    return response
