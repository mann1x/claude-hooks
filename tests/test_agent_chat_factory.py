"""Tests for v1.5 module 6: agent-loop ChatClient factory + llamafile
variant.

Covers:
- ``make_agent_chat_client`` routing per ``model_ref``
- ``LlamafileAgentChatClient.chat()`` happy path + retry semantics
- ``last_usage`` mapping from OpenAI ``usage`` -> Ollama field names
- Port-cache TTL on the agent client
"""

from __future__ import annotations

import json
import unittest
import urllib.error
from unittest.mock import patch, MagicMock

from claude_hooks.get_advice import chat_client as cc_mod
from claude_hooks.get_advice.chat_client import (
    ChatClient,
    LlamafileAgentChatClient,
    make_agent_chat_client,
)


def _fake_http_response(payload: dict):
    body = json.dumps(payload).encode("utf-8")
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=MagicMock(read=lambda: body))
    cm.__exit__ = MagicMock(return_value=False)
    return cm


# ---------------------------------------------------------------- #
# Factory routing
# ---------------------------------------------------------------- #

class TestFactory(unittest.TestCase):

    def test_bare_ref_returns_ollama_chat_client(self):
        client = make_agent_chat_client("gemma4-e4b",
                                        "http://localhost:11434")
        self.assertIsInstance(client, ChatClient)

    def test_cloud_suffix_returns_ollama_chat_client(self):
        client = make_agent_chat_client("kimi-k2.6:cloud",
                                        "http://localhost:11434")
        self.assertIsInstance(client, ChatClient)

    def test_llamafile_prefix_returns_llamafile_client(self):
        client = make_agent_chat_client("llamafile://gemma-local",
                                        "http://localhost:11434")
        self.assertIsInstance(client, LlamafileAgentChatClient)
        self.assertEqual(client.label, "gemma-local")

    def test_factory_passes_kwargs(self):
        # Both paths accept tuning kwargs identically.
        ollama = make_agent_chat_client("foo", "http://x:1",
                                        timeout_s=42.0, max_retries=3)
        self.assertEqual(ollama.timeout_s, 42.0)
        self.assertEqual(ollama.max_retries, 3)

        lf = make_agent_chat_client("llamafile://g", "http://x:1",
                                    timeout_s=7.0, max_retries=2)
        self.assertEqual(lf.timeout_s, 7.0)
        self.assertEqual(lf.max_retries, 2)


# ---------------------------------------------------------------- #
# LlamafileAgentChatClient
# ---------------------------------------------------------------- #

class TestLlamafileAgentChatClient(unittest.TestCase):

    def test_init_rejects_empty_label(self):
        with self.assertRaises(ValueError):
            LlamafileAgentChatClient("")

    def test_resolve_port_calls_daemon_then_caches(self):
        client = LlamafileAgentChatClient("gemma", port_cache_ttl=60.0)
        with patch("claude_hooks.daemon_client.chat_model_ensure",
                   return_value={"ready": True, "port": 38093}) as ensure:
            self.assertEqual(client._resolve_port(), 38093)
            self.assertEqual(client._resolve_port(), 38093)
        ensure.assert_called_once()

    def test_resolve_port_daemon_down(self):
        client = LlamafileAgentChatClient("gemma")
        with patch("claude_hooks.daemon_client.chat_model_ensure",
                   return_value=None):
            with self.assertRaises(RuntimeError):
                client._resolve_port()

    def test_chat_happy_path_openai_passthrough(self):
        client = LlamafileAgentChatClient("gemma")
        openai_resp = {
            "choices": [{
                "message": {"role": "assistant", "content": "hello"},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 5,
                "total_tokens": 17,
            },
        }
        captured = {}

        def _fake(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data)
            return _fake_http_response(openai_resp)

        with patch("claude_hooks.daemon_client.chat_model_ensure",
                   return_value={"ready": True, "port": 38094}):
            with patch.object(cc_mod.urllib.request, "urlopen",
                              side_effect=_fake):
                resp = client.chat({
                    "model": "gemma",
                    "messages": [{"role": "user", "content": "hi"}],
                    "options": {"num_predict": 100},
                })

        self.assertEqual(captured["url"],
                         "http://127.0.0.1:38094/v1/chat/completions")
        # num_predict -> max_tokens
        self.assertEqual(captured["body"]["max_tokens"], 100)
        # OpenAI shape returned verbatim
        self.assertEqual(resp, openai_resp)
        # last_usage rewritten to Ollama field names
        self.assertEqual(client.last_usage["prompt_eval_count"], 12)
        self.assertEqual(client.last_usage["eval_count"], 5)

    def test_chat_tools_and_tool_choice_passthrough(self):
        client = LlamafileAgentChatClient("gemma")
        captured = {}

        def _fake(req, timeout=None):
            captured["body"] = json.loads(req.data)
            return _fake_http_response({
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0},
            })

        tools = [{"type": "function", "function": {"name": "ls"}}]
        with patch("claude_hooks.daemon_client.chat_model_ensure",
                   return_value={"ready": True, "port": 38094}):
            with patch.object(cc_mod.urllib.request, "urlopen",
                              side_effect=_fake):
                client.chat({
                    "model": "gemma",
                    "messages": [{"role": "user", "content": "hi"}],
                    "tools": tools,
                    "tool_choice": "auto",
                })

        self.assertEqual(captured["body"]["tools"], tools)
        self.assertEqual(captured["body"]["tool_choice"], "auto")

    def test_chat_retries_on_5xx(self):
        client = LlamafileAgentChatClient(
            "gemma", retry_base_delay_s=0.01,
            retry_max_delay_s=0.02, max_retries=3,
        )
        attempts = {"n": 0}

        def _flaky(req, timeout=None):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise urllib.error.HTTPError(
                    req.full_url, 503, "Service Unavailable", {}, None,
                )
            return _fake_http_response({
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            })

        with patch("claude_hooks.daemon_client.chat_model_ensure",
                   return_value={"ready": True, "port": 38094}):
            with patch.object(cc_mod.urllib.request, "urlopen",
                              side_effect=_flaky):
                with patch.object(cc_mod.time, "sleep"):
                    resp = client.chat({"model": "gemma", "messages": []})

        self.assertEqual(attempts["n"], 3)
        self.assertEqual(resp["choices"][0]["message"]["content"], "ok")

    def test_chat_re_ensures_on_connection_failure(self):
        client = LlamafileAgentChatClient(
            "gemma", retry_base_delay_s=0.01,
            retry_max_delay_s=0.02, max_retries=2,
        )
        attempts = {"n": 0}

        def _flaky(req, timeout=None):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise urllib.error.URLError("connection refused")
            return _fake_http_response({
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0},
            })

        with patch("claude_hooks.daemon_client.chat_model_ensure",
                   return_value={"ready": True, "port": 38094}) as ensure:
            with patch.object(cc_mod.urllib.request, "urlopen",
                              side_effect=_flaky):
                with patch.object(cc_mod.time, "sleep"):
                    client.chat({"model": "gemma", "messages": []})

        # Cold-start ensure + force-refresh ensure on attempt 0's
        # connection failure = 2 calls minimum.
        self.assertGreaterEqual(ensure.call_count, 2)

    def test_chat_propagates_4xx_immediately(self):
        client = LlamafileAgentChatClient("gemma", max_retries=3)

        def _bad(req, timeout=None):
            raise urllib.error.HTTPError(
                req.full_url, 400, "Bad Request", {}, None,
            )

        with patch("claude_hooks.daemon_client.chat_model_ensure",
                   return_value={"ready": True, "port": 38094}):
            with patch.object(cc_mod.urllib.request, "urlopen",
                              side_effect=_bad):
                with self.assertRaises(RuntimeError) as ctx:
                    client.chat({"model": "gemma", "messages": []})
        self.assertIn("400", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
