"""Tests for ``claude_hooks.chat_backend``.

Covers the four units of v1.5 module 4:

1. ``parse_model_ref`` prefix parsing
2. ``OllamaChatClient.generate`` — happy path, HTTP/network failure,
   malformed response, request wire-shape
3. ``LlamafileChatClient`` — daemon ensure + cache, retry-on-failure,
   daemon-down, request wire-shape
4. ``make_chat_client`` factory routing + ``call`` one-shot helper
"""

from __future__ import annotations

import io
import json
import socket
import unittest
import urllib.error
from contextlib import contextmanager
from unittest.mock import patch, MagicMock

from claude_hooks import chat_backend
from claude_hooks.chat_backend import (
    ChatBackendError,
    LlamafileChatClient,
    OllamaChatClient,
    UnknownBackendError,
    call,
    make_chat_client,
    parse_model_ref,
)

from tests._fixtures_net import FIXTURE_LAN_HOST_ALT


# ----------------------------------------------------------------- #
# helpers
# ----------------------------------------------------------------- #

def _fake_http_response(payload: dict, status: int = 200):
    """Return a context-manager mock that mimics urlopen's return."""
    body = json.dumps(payload).encode("utf-8")
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=MagicMock(read=lambda: body))
    cm.__exit__ = MagicMock(return_value=False)
    return cm


@contextmanager
def _capture_urlopen(payload: dict):
    """Patch urlopen to return ``payload`` and capture the Request."""
    captured = {}

    def _fake(req, timeout=None):
        captured["req"] = req
        captured["body"] = req.data
        captured["timeout"] = timeout
        return _fake_http_response(payload)

    with patch.object(chat_backend.urllib.request, "urlopen",
                      side_effect=_fake):
        yield captured


# ----------------------------------------------------------------- #
# parse_model_ref
# ----------------------------------------------------------------- #

class TestParseModelRef(unittest.TestCase):

    def test_llamafile_prefix(self):
        self.assertEqual(
            parse_model_ref("llamafile://gemma"),
            ("llamafile", "gemma"),
        )

    def test_llamafile_prefix_dotted_label(self):
        self.assertEqual(
            parse_model_ref("llamafile://qwen3-32b.iq4"),
            ("llamafile", "qwen3-32b.iq4"),
        )

    def test_bare_ollama(self):
        self.assertEqual(parse_model_ref("gemma4-e4b"), ("ollama", "gemma4-e4b"))

    def test_cloud_suffix(self):
        self.assertEqual(parse_model_ref("kimi-k2.6:cloud"),
                         ("ollama", "kimi-k2.6:cloud"))

    def test_empty_label_after_prefix_still_routes_to_llamafile(self):
        # parser doesn't validate the label — that's the registry's job.
        self.assertEqual(parse_model_ref("llamafile://"), ("llamafile", ""))


# ----------------------------------------------------------------- #
# OllamaChatClient
# ----------------------------------------------------------------- #

class TestOllamaChatClient(unittest.TestCase):

    def test_generate_happy_path(self):
        client = OllamaChatClient(url="http://127.0.0.1:11434/api/generate")
        with _capture_urlopen({"response": "  hello world  "}) as cap:
            out = client.generate("u", "s", "gemma", timeout=5,
                                  max_tokens=50, num_ctx=8192)
        self.assertEqual(out, "hello world")
        # Wire shape
        payload = json.loads(cap["body"])
        self.assertEqual(payload["model"], "gemma")
        self.assertEqual(payload["system"], "s")
        self.assertEqual(payload["prompt"], "u")
        self.assertFalse(payload["stream"])
        self.assertFalse(payload["think"])
        self.assertEqual(payload["options"]["num_predict"], 50)
        self.assertEqual(payload["options"]["num_ctx"], 8192)

    def test_generate_drops_num_ctx_when_unset(self):
        client = OllamaChatClient()
        with _capture_urlopen({"response": "x"}) as cap:
            client.generate("u", "s", "gemma", num_ctx=0,
                            timeout=5, max_tokens=10)
        payload = json.loads(cap["body"])
        self.assertNotIn("num_ctx", payload["options"])

    def test_generate_empty_response_field(self):
        client = OllamaChatClient()
        with _capture_urlopen({"response": ""}):
            self.assertEqual(client.generate("u", "s", "g", timeout=5,
                                             max_tokens=10, num_ctx=0),
                             "")

    def test_generate_network_error(self):
        client = OllamaChatClient()
        with patch.object(chat_backend.urllib.request, "urlopen",
                          side_effect=urllib.error.URLError("connect refused")):
            with self.assertRaises(ChatBackendError):
                client.generate("u", "s", "g", timeout=5,
                                max_tokens=10, num_ctx=0)

    def test_generate_socket_timeout(self):
        client = OllamaChatClient()
        with patch.object(chat_backend.urllib.request, "urlopen",
                          side_effect=socket.timeout("timed out")):
            with self.assertRaises(ChatBackendError):
                client.generate("u", "s", "g", timeout=5,
                                max_tokens=10, num_ctx=0)

    def test_generate_malformed_json(self):
        client = OllamaChatClient()
        bad = MagicMock()
        bad.__enter__ = MagicMock(return_value=MagicMock(
            read=lambda: b"not-json"))
        bad.__exit__ = MagicMock(return_value=False)
        with patch.object(chat_backend.urllib.request, "urlopen",
                          return_value=bad):
            with self.assertRaises(ChatBackendError):
                client.generate("u", "s", "g", timeout=5,
                                max_tokens=10, num_ctx=0)


# ----------------------------------------------------------------- #
# LlamafileChatClient
# ----------------------------------------------------------------- #

class TestLlamafileChatClient(unittest.TestCase):

    def test_init_rejects_empty_label(self):
        with self.assertRaises(UnknownBackendError):
            LlamafileChatClient(label="")

    def test_resolve_port_calls_daemon_then_caches(self):
        client = LlamafileChatClient(label="gemma", port_cache_ttl=60.0)
        with patch("claude_hooks.daemon_client.chat_model_ensure",
                   return_value={"ready": True, "port": 38093,
                                 "mode": "auto"}) as ensure:
            self.assertEqual(client._resolve_port(), 38093)
            self.assertEqual(client._resolve_port(), 38093)
        ensure.assert_called_once()

    def test_resolve_port_daemon_down(self):
        client = LlamafileChatClient(label="gemma")
        with patch("claude_hooks.daemon_client.chat_model_ensure",
                   return_value=None):
            with self.assertRaises(ChatBackendError) as ctx:
                client._resolve_port()
        self.assertIn("daemon unreachable", str(ctx.exception))

    def test_resolve_port_nack(self):
        client = LlamafileChatClient(label="gemma")
        with patch("claude_hooks.daemon_client.chat_model_ensure",
                   return_value={"available": False,
                                 "reason": "unknown_label"}):
            with self.assertRaises(ChatBackendError) as ctx:
                client._resolve_port()
        self.assertIn("unknown_label", str(ctx.exception))

    def test_resolve_port_daemon_exception(self):
        client = LlamafileChatClient(label="gemma")
        with patch("claude_hooks.daemon_client.chat_model_ensure",
                   side_effect=RuntimeError("boom")):
            with self.assertRaises(ChatBackendError) as ctx:
                client._resolve_port()
        self.assertIn("boom", str(ctx.exception))

    def test_resolve_port_no_daemon_mode(self):
        client = LlamafileChatClient(label="gemma", daemon_ensure=False)
        with self.assertRaises(UnknownBackendError):
            client._resolve_port()

    def test_generate_happy_path(self):
        client = LlamafileChatClient(label="gemma")
        with patch("claude_hooks.daemon_client.chat_model_ensure",
                   return_value={"ready": True, "port": 38093}):
            with _capture_urlopen({
                "choices": [{"message": {"content": "hello"}}]
            }) as cap:
                out = client.generate("u", "s", "gemma", timeout=5,
                                      max_tokens=100, num_ctx=8192)
        self.assertEqual(out, "hello")
        # Wire shape: OpenAI
        self.assertEqual(cap["req"].full_url,
                         "http://127.0.0.1:38093/v1/chat/completions")
        payload = json.loads(cap["body"])
        self.assertEqual(payload["model"], "gemma")
        self.assertEqual(payload["messages"][0],
                         {"role": "system", "content": "s"})
        self.assertEqual(payload["messages"][1],
                         {"role": "user", "content": "u"})
        self.assertFalse(payload["stream"])
        self.assertEqual(payload["max_tokens"], 100)

    def test_generate_retries_on_first_failure(self):
        client = LlamafileChatClient(label="gemma")
        attempts = {"n": 0}

        def _flaky(req, timeout=None):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise urllib.error.URLError("connection refused")
            return _fake_http_response({
                "choices": [{"message": {"content": "ok"}}]
            })

        with patch("claude_hooks.daemon_client.chat_model_ensure",
                   return_value={"ready": True, "port": 38093}) as ensure:
            with patch.object(chat_backend.urllib.request, "urlopen",
                              side_effect=_flaky):
                out = client.generate("u", "s", "gemma", timeout=5,
                                      max_tokens=10, num_ctx=0)
        self.assertEqual(out, "ok")
        self.assertEqual(attempts["n"], 2)
        # daemon was hit twice: cold-start + force-refresh on retry
        self.assertEqual(ensure.call_count, 2)

    def test_generate_retry_also_fails(self):
        client = LlamafileChatClient(label="gemma")
        with patch("claude_hooks.daemon_client.chat_model_ensure",
                   return_value={"ready": True, "port": 38093}):
            with patch.object(chat_backend.urllib.request, "urlopen",
                              side_effect=urllib.error.URLError("nope")):
                with self.assertRaises(ChatBackendError) as ctx:
                    client.generate("u", "s", "g", timeout=5,
                                    max_tokens=10, num_ctx=0)
        self.assertIn("after retry", str(ctx.exception))

    def test_generate_empty_choices_returns_empty(self):
        client = LlamafileChatClient(label="gemma")
        with patch("claude_hooks.daemon_client.chat_model_ensure",
                   return_value={"ready": True, "port": 38093}):
            with _capture_urlopen({"choices": []}):
                self.assertEqual(
                    client.generate("u", "s", "g", timeout=5,
                                    max_tokens=10, num_ctx=0),
                    "",
                )

    def test_generate_malformed_response(self):
        client = LlamafileChatClient(label="gemma")
        bad = MagicMock()
        bad.__enter__ = MagicMock(return_value=MagicMock(
            read=lambda: b"<<garbage>>"))
        bad.__exit__ = MagicMock(return_value=False)
        with patch("claude_hooks.daemon_client.chat_model_ensure",
                   return_value={"ready": True, "port": 38093}):
            with patch.object(chat_backend.urllib.request, "urlopen",
                              return_value=bad):
                with self.assertRaises(ChatBackendError):
                    client.generate("u", "s", "g", timeout=5,
                                    max_tokens=10, num_ctx=0)


# ----------------------------------------------------------------- #
# make_chat_client + call
# ----------------------------------------------------------------- #

class TestFactory(unittest.TestCase):

    def test_factory_returns_ollama_for_bare_ref(self):
        client, target = make_chat_client("gemma4-e4b")
        self.assertIsInstance(client, OllamaChatClient)
        self.assertEqual(target, "gemma4-e4b")

    def test_factory_returns_ollama_for_cloud_suffix(self):
        client, target = make_chat_client("kimi-k2.6:cloud")
        self.assertIsInstance(client, OllamaChatClient)
        self.assertEqual(target, "kimi-k2.6:cloud")

    def test_factory_returns_llamafile_strips_prefix(self):
        client, target = make_chat_client("llamafile://gemma-q5")
        self.assertIsInstance(client, LlamafileChatClient)
        self.assertEqual(target, "gemma-q5")
        self.assertEqual(client.label, "gemma-q5")

    def test_factory_passes_ollama_url(self):
        url = f"http://{FIXTURE_LAN_HOST_ALT}:11434/api/generate"
        client, _ = make_chat_client("x", ollama_url=url)
        self.assertEqual(client.url, url)


class TestCallOneShot(unittest.TestCase):

    def test_call_happy_path_ollama(self):
        with _capture_urlopen({"response": "yes"}):
            out = call("u", "s", "gemma", timeout=5)
        self.assertEqual(out, "yes")

    def test_call_graceful_on_network_error(self):
        with patch.object(chat_backend.urllib.request, "urlopen",
                          side_effect=urllib.error.URLError("nope")):
            self.assertEqual(call("u", "s", "gemma"), "")

    def test_call_raise_on_error_propagates(self):
        with patch.object(chat_backend.urllib.request, "urlopen",
                          side_effect=urllib.error.URLError("nope")):
            with self.assertRaises(ChatBackendError):
                call("u", "s", "gemma", raise_on_error=True)

    def test_call_llamafile_happy_path(self):
        with patch("claude_hooks.daemon_client.chat_model_ensure",
                   return_value={"ready": True, "port": 38093}):
            with _capture_urlopen({
                "choices": [{"message": {"content": "hi"}}]
            }):
                out = call("u", "s", "llamafile://g", timeout=5)
        self.assertEqual(out, "hi")

    def test_call_llamafile_daemon_down_graceful(self):
        with patch("claude_hooks.daemon_client.chat_model_ensure",
                   return_value=None):
            self.assertEqual(call("u", "s", "llamafile://g"), "")

    def test_call_llamafile_daemon_down_raise(self):
        with patch("claude_hooks.daemon_client.chat_model_ensure",
                   return_value=None):
            with self.assertRaises(ChatBackendError):
                call("u", "s", "llamafile://g", raise_on_error=True)


if __name__ == "__main__":
    unittest.main()
