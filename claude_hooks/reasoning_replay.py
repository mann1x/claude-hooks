"""What happens to a past turn's reasoning on the wire (plan C4).

Three modes, set per (role, model) (``[compaction] reasoning_replay``):

``none``    the reasoning is dropped from the history (the default, and
            what the fork measured right for ollama / xollama / opencoti:
            their templates either drop the field or a replayed think is
            prefill the model cannot use).
``all``     the reasoning field stays on the assistant message. Probed
            first (:func:`probe`): when the server's chat template drops
            the field, the reasoning never reaches the model and the mode
            falls back to ``inline``.
``inline``  the "goose fallback" (fork ``8baed60ce``): the reasoning goes
            into the content as ``<think>…</think>`` ahead of the reply.

The estimator charges the history as it stands, so whichever mode is in
force is also what gets counted — the fork measured a 2.23x error when
the estimate assumed one mode and the wire carried another.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Optional

from claude_hooks import token_calib

log = logging.getLogger("claude_hooks.reasoning_replay")

MODES = ("none", "all", "inline")

#: Prompt-count difference that shows the field reached the template.
_PROBE_REASONING = "Considering the options carefully. " * 12
_PROBE_MIN_DELTA = 30

_cache: dict[tuple, Optional[bool]] = {}
_lock = threading.Lock()


def _reasoning(msg: dict) -> str:
    for key in token_calib.REASONING_KEYS:
        v = msg.get(key)
        if isinstance(v, str) and v.strip():
            return v
    return ""


def apply(msg: dict, mode: str) -> dict:
    """``msg`` (an assistant turn about to join the history) under ``mode``."""
    out = dict(msg)
    thinking = _reasoning(out)
    if mode == "all":
        return out
    for key in token_calib.REASONING_KEYS:
        out.pop(key, None)
    if mode == "inline" and thinking:
        content = out.get("content") or ""
        out["content"] = f"<think>\n{thinking}\n</think>\n\n{content}".rstrip()
    return out


def _prompt_tokens(resp: dict) -> int:
    usage = (resp or {}).get("usage") or {}
    return int(usage.get("prompt_tokens") or (resp or {}).get(
        "prompt_eval_count") or 0)


def probe(chat_client: Any, model: str) -> Optional[bool]:
    """Whether a reasoning field on a past assistant turn reaches the
    prompt: the same two-turn request with and without it, compared by
    the provider's prompt count. ``None`` when the counts do not say."""
    key = (getattr(chat_client, "base_url", None) or id(chat_client), model)
    with _lock:
        if key in _cache:
            return _cache[key]

    def ask(with_reasoning: bool) -> int:
        prior = {"role": "assistant", "content": "A"}
        if with_reasoning:
            prior["thinking"] = _PROBE_REASONING
            prior["reasoning_content"] = _PROBE_REASONING
        return _prompt_tokens(chat_client.chat({
            "model": model, "stream": False, "think": False,
            "messages": [{"role": "user", "content": "Reply with A."}, prior,
                         {"role": "user", "content": "Reply with B."}],
            "options": {"num_predict": 1}}))

    try:
        bare, carried = ask(False), ask(True)
    except Exception:
        log.debug("reasoning replay probe failed for %s", model, exc_info=True)
        return None  # a network failure is not cached
    answer: Optional[bool] = None
    if bare and carried:
        answer = (carried - bare) >= _PROBE_MIN_DELTA
    with _lock:
        _cache[key] = answer
    return answer


def resolve(mode: Optional[str], chat_client: Any, model: str) -> str:
    """The mode to use: ``all`` only where the probe shows the field
    arrives, ``inline`` where it shows it is dropped."""
    mode = mode if mode in MODES else "none"
    if mode != "all":
        return mode
    reaches = probe(chat_client, model)
    if reaches is False:
        log.info("reasoning replay: %s drops the reasoning field; using "
                 "inline <think> instead", model)
        return "inline"
    return "all"


def clear_cache() -> None:
    with _lock:
        _cache.clear()


__all__ = ["MODES", "apply", "probe", "resolve", "clear_cache"]
