"""Tiny Ollama ``/api/chat`` wrapper for the advisor.

Speaks Ollama's native chat shape (not OpenAI compat) so we get
``prompt_eval_count`` / ``eval_count`` reliably and can pin ``num_ctx``
via the ``options`` block. Translates the response back into the
OpenAI-style ``choices[*].message`` shape the agent loop runner
expects.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Optional

log = logging.getLogger("claude_hooks.get_advice.chat_client")

DEFAULT_TIMEOUT_S = 600.0


class ChatClient:
    def __init__(self, base_url: str, *, timeout_s: float = DEFAULT_TIMEOUT_S):
        self.base_url = base_url.rstrip("/")
        if self.base_url.endswith("/v1"):
            self.base_url = self.base_url[: -len("/v1")]
        self.timeout_s = timeout_s
        # Cumulative usage across calls in this client's lifetime.
        # Caller resets per-session as needed.
        self.last_usage: dict[str, int] = {
            "prompt_eval_count": 0,
            "eval_count": 0,
        }

    def chat(self, payload: dict) -> dict:
        """POST /api/chat with the supplied (OpenAI-shape) payload.
        Translates the Ollama response back into OpenAI shape so the
        agent-loop runner can consume it unchanged."""
        body = self._to_ollama(payload)
        url = f"{self.base_url}/api/chat"
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                data = json.loads(resp.read())
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
            log.error("ollama chat error: %s", e)
            raise
        return self._from_ollama(data)

    def _to_ollama(self, payload: dict) -> dict:
        body: dict = {
            "model": payload.get("model"),
            "messages": payload.get("messages") or [],
            "stream": False,
        }
        opts = dict(payload.get("options") or {})
        if opts:
            body["options"] = opts
        if "tools" in payload and payload["tools"]:
            body["tools"] = payload["tools"]
        if "tool_choice" in payload:
            body["tool_choice"] = payload["tool_choice"]
        # think is accepted natively by Ollama
        if "think" in payload:
            body["think"] = payload["think"]
        return body

    def _from_ollama(self, data: dict) -> dict:
        msg = dict(data.get("message") or {})
        # Ollama may return tool_calls with arguments as dict; runner
        # is fine with both, but normalize to JSON strings to match
        # the OpenAI wire shape.
        tool_calls = msg.get("tool_calls") or []
        if tool_calls:
            normalized = []
            for tc in tool_calls:
                fn = dict(tc.get("function") or {})
                args = fn.get("arguments")
                if isinstance(args, dict):
                    fn["arguments"] = json.dumps(args)
                tc2 = dict(tc)
                tc2["function"] = fn
                # Ollama doesn't always supply an id; fabricate one so
                # tool_call_id round-trips cleanly.
                if not tc2.get("id"):
                    tc2["id"] = f"tc_{len(normalized)}"
                if not tc2.get("type"):
                    tc2["type"] = "function"
                normalized.append(tc2)
            msg["tool_calls"] = normalized
        finish_reason = "tool_calls" if tool_calls else "stop"
        # Track per-call usage so the caller can read it without
        # re-walking the response.
        prompt_tokens = int(data.get("prompt_eval_count") or 0)
        completion_tokens = int(data.get("eval_count") or 0)
        self.last_usage = {
            "prompt_eval_count": prompt_tokens,
            "eval_count": completion_tokens,
        }
        return {
            "choices": [{
                "message": msg,
                "finish_reason": finish_reason,
            }],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }
