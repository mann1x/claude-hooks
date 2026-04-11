"""
HyDE (Hypothetical Document Embeddings) query expansion.

Before searching Qdrant, generates a short hypothetical answer to the user's
prompt using a local Ollama model. The hypothetical answer is then used as
the search query, which produces dramatically better vector matches because
it's in the same "answer space" as the stored memories.

Two modes:

- ``expand_query`` — plain HyDE. Works well for general queries, but can
  hallucinate badly on project-specific jargon the LLM has never seen
  (e.g. it might expand "109e Gemma" to "Tesla Model Y 109e" because
  it has no idea what a pruned MoE variant is).

- ``expand_query_with_context`` — "grounded" HyDE. Takes a list of
  memories that were already retrieved via a raw-query recall and feeds
  them to the LLM as factual context before asking it to generate the
  hypothetical. This prevents hallucinations because the model can
  anchor its answer in real stored memories, and dramatically improves
  recall quality on niche topics.

Both modes fall back gracefully to the original prompt if Ollama is
unavailable.
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

_GROUNDED_SYSTEM_PROMPT = (
    "You are a memory recall assistant. You are given a user's question and a "
    "few related memory entries retrieved from a local knowledge base. Using "
    "ONLY facts that are consistent with those memories, write a short, "
    "technical answer to the question as if it were itself a stored memory "
    "entry. Prefer terminology and specifics from the provided memories. "
    "Do not invent facts beyond what is grounded in the memories. "
    "Maximum 2-3 sentences. Do not explain, just state facts."
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

    for m in [model, fallback_model]:
        result = _call_ollama(
            user_prompt=prompt,
            system_prompt=_SYSTEM_PROMPT,
            model=m,
            url=url,
            timeout=timeout,
            max_tokens=max_tokens,
            keep_alive=keep_alive,
        )
        if result:
            log.debug("hyde expanded with %s: %s", m, result[:80])
            return result

    log.debug("hyde: all models failed, using raw prompt")
    return prompt


def expand_query_with_context(
    prompt: str,
    memories: list[str],
    *,
    model: str = "qwen3.5:2b",
    fallback_model: str = "gemma4:e2b",
    url: str = "http://localhost:11434/api/generate",
    timeout: float = 30.0,
    max_tokens: int = 150,
    keep_alive: str = "15m",
    max_context_chars: int = 1500,
) -> str:
    """
    Generate a hypothetical answer grounded in the supplied memory snippets.

    ``memories`` should be a list of strings representing memory entries
    retrieved via a raw-query Qdrant search. They are trimmed and stitched
    into the prompt as factual context the LLM is told to respect.

    Returns the original prompt on any failure or if ``memories`` is empty
    (in which case there is nothing to ground against and plain ``expand_query``
    should be used instead — callers should gate on this).
    """
    if not prompt.strip():
        return prompt
    if not memories:
        return prompt

    context = _format_context(memories, max_context_chars)
    user_prompt = (
        f"Relevant memories:\n{context}\n\n"
        f"Question: {prompt}\n\n"
        f"Write the factual memory entry now:"
    )

    for m in [model, fallback_model]:
        result = _call_ollama(
            user_prompt=user_prompt,
            system_prompt=_GROUNDED_SYSTEM_PROMPT,
            model=m,
            url=url,
            timeout=timeout,
            max_tokens=max_tokens,
            keep_alive=keep_alive,
        )
        if result:
            log.debug("hyde (grounded) expanded with %s: %s", m, result[:80])
            return result

    log.debug("hyde (grounded): all models failed, using raw prompt")
    return prompt


def _format_context(memories: list[str], max_chars: int) -> str:
    """Stitch memory snippets into a bullet list, capped at ``max_chars``."""
    lines: list[str] = []
    total = 0
    for i, mem in enumerate(memories, 1):
        snippet = " ".join(mem.strip().split())  # collapse whitespace
        # Hard-cap individual entries so one huge memory can't starve others.
        per_entry_cap = max(200, max_chars // max(1, len(memories)))
        if len(snippet) > per_entry_cap:
            snippet = snippet[: per_entry_cap - 3].rstrip() + "..."
        line = f"{i}. {snippet}"
        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line) + 1
    return "\n".join(lines)


def _call_ollama(
    *,
    user_prompt: str,
    system_prompt: str,
    model: str,
    url: str,
    timeout: float,
    max_tokens: int,
    keep_alive: str = "15m",
) -> str:
    """Call Ollama generate API. Returns the response text or empty string on failure.

    keep_alive: how long Ollama should keep the model resident after this call.
    "15m" = stays loaded for 15 minutes after last use. "-1" = never unload.
    """
    body = json.dumps({
        "model": model,
        "system": system_prompt,
        "prompt": user_prompt,
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
