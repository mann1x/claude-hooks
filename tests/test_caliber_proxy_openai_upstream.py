"""Tests for v1.5 module 7: caliber-grounding-proxy ``openai_compat``
upstream backend.

Covers:
- ``default_upstream_backend`` env var parsing + invalid-value fallback
- ``chat_completions`` skips Ollama translation and POSTs to
  ``/v1/chat/completions`` when backend=openai_compat
- Response passes through unchanged (no _to_openai_response wrap)
- Retry budget still applies on 5xx
"""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch, MagicMock

# Skip the whole module if httpx isn't available — caliber_proxy is
# an optional install.
try:
    import httpx  # noqa: F401
except ImportError:  # pragma: no cover
    raise unittest.SkipTest("httpx not installed")

from claude_hooks.caliber_proxy import ollama


def _make_resp(json_body, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body
    resp.text = json.dumps(json_body)
    return resp


class TestDefaultUpstreamBackend(unittest.TestCase):

    def test_default_is_ollama(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CALIBER_GROUNDING_UPSTREAM_BACKEND", None)
            self.assertEqual(ollama.default_upstream_backend(), "ollama")

    def test_openai_compat_accepted(self):
        with patch.dict(os.environ,
                        {"CALIBER_GROUNDING_UPSTREAM_BACKEND": "openai_compat"}):
            self.assertEqual(ollama.default_upstream_backend(), "openai_compat")

    def test_case_insensitive(self):
        with patch.dict(os.environ,
                        {"CALIBER_GROUNDING_UPSTREAM_BACKEND": "OPENAI_COMPAT"}):
            self.assertEqual(ollama.default_upstream_backend(), "openai_compat")

    def test_invalid_value_falls_back(self):
        with patch.dict(os.environ,
                        {"CALIBER_GROUNDING_UPSTREAM_BACKEND": "bogus"}):
            self.assertEqual(ollama.default_upstream_backend(), "ollama")


class TestOpenaiCompatPassthrough(unittest.TestCase):

    def setUp(self):
        # Close any cached client between tests.
        ollama.close()
        self.env_patch = patch.dict(
            os.environ,
            {
                "CALIBER_GROUNDING_UPSTREAM_BACKEND": "openai_compat",
                "CALIBER_GROUNDING_UPSTREAM": "http://127.0.0.1:38094",
            },
        )
        self.env_patch.start()

    def tearDown(self):
        self.env_patch.stop()
        ollama.close()

    def test_posts_to_v1_chat_completions_with_no_translation(self):
        captured = {}
        openai_resp = {
            "choices": [{
                "message": {"role": "assistant", "content": "hi"},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": 5,
                "completion_tokens": 2,
                "total_tokens": 7,
            },
        }
        fake_client = MagicMock()

        def _post(url, json=None, headers=None):
            captured["url"] = url
            captured["body"] = json
            return _make_resp(openai_resp)

        fake_client.post.side_effect = _post
        with patch.object(ollama, "_get_client", return_value=fake_client):
            out = ollama.chat_completions({
                "model": "gemma",
                "messages": [{"role": "user", "content": "hi"}],
            })

        self.assertEqual(captured["url"],
                         "http://127.0.0.1:38094/v1/chat/completions")
        # No Ollama translation: messages list unchanged, stream forced
        self.assertEqual(captured["body"]["messages"],
                         [{"role": "user", "content": "hi"}])
        self.assertFalse(captured["body"]["stream"])
        # Response returned verbatim
        self.assertEqual(out, openai_resp)

    def test_retry_on_5xx(self):
        attempts = {"n": 0}
        openai_resp = {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

        def _post(url, json=None, headers=None):
            attempts["n"] += 1
            if attempts["n"] < 2:
                return _make_resp({"error": "transient"}, status_code=503)
            return _make_resp(openai_resp)

        fake_client = MagicMock()
        fake_client.post.side_effect = _post
        with patch.object(ollama, "_get_client", return_value=fake_client):
            with patch.object(ollama.time, "sleep"):
                out = ollama.chat_completions({
                    "model": "gemma",
                    "messages": [{"role": "user", "content": "hi"}],
                })

        self.assertEqual(attempts["n"], 2)
        self.assertEqual(out["choices"][0]["message"]["content"], "ok")


class TestOllamaBackendStillWorks(unittest.TestCase):
    """Make sure the default backend path is untouched."""

    def setUp(self):
        ollama.close()
        self.env_patch = patch.dict(
            os.environ,
            {"CALIBER_GROUNDING_UPSTREAM": "http://localhost:11434"},
        )
        self.env_patch.start()
        # Explicitly unset BACKEND to confirm default
        os.environ.pop("CALIBER_GROUNDING_UPSTREAM_BACKEND", None)

    def tearDown(self):
        self.env_patch.stop()
        ollama.close()

    def test_default_path_targets_api_chat(self):
        captured = {}
        ollama_resp = {
            "message": {"role": "assistant", "content": "hi", "tool_calls": []},
            "prompt_eval_count": 5,
            "eval_count": 2,
        }
        fake_client = MagicMock()

        def _post(url, json=None, headers=None):
            captured["url"] = url
            captured["body"] = json
            return _make_resp(ollama_resp)

        fake_client.post.side_effect = _post
        with patch.object(ollama, "_get_client", return_value=fake_client):
            out = ollama.chat_completions({
                "model": "gemma",
                "messages": [{"role": "user", "content": "hi"}],
            })

        self.assertEqual(captured["url"], "http://localhost:11434/api/chat")
        # Translated into OpenAI shape on output
        self.assertIn("choices", out)
        self.assertEqual(out["choices"][0]["message"]["content"], "hi")


if __name__ == "__main__":
    unittest.main()
