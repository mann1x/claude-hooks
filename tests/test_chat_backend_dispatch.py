"""Tests for the v1.5 ``model_ref`` dispatch in hyde / reflect /
consolidate.

These cover the routing layer added in module 5: a ``llamafile://``
prefix goes through ``chat_backend.call``; bare identifiers keep
using the legacy ``_call_ollama`` / Ollama urllib code path.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from claude_hooks import hyde, reflect, consolidate


class TestHydeDispatch(unittest.TestCase):

    def test_bare_ref_uses_legacy_ollama(self):
        with patch.object(hyde, "_call_ollama",
                          return_value="bare-result here") as legacy:
            with patch.object(hyde, "chat_backend") as cb:
                out = hyde.expand_query("the prompt", model_ref="gemma4:e2b",
                                        cache_enabled=False)
        self.assertEqual(out, "bare-result here")
        legacy.assert_called_once()
        cb.call.assert_not_called()

    def test_llamafile_ref_uses_chat_backend(self):
        with patch.object(hyde, "_call_ollama") as legacy:
            with patch.object(hyde.chat_backend, "call",
                              return_value="llamafile result here") as cb:
                out = hyde.expand_query("the prompt",
                                        model_ref="llamafile://gemma",
                                        cache_enabled=False)
        self.assertEqual(out, "llamafile result here")
        cb.assert_called_once()
        legacy.assert_not_called()
        # And the model_ref propagated unchanged
        kwargs = cb.call_args.kwargs
        self.assertEqual(kwargs["model_ref"], "llamafile://gemma")

    def test_llamafile_ref_minlen_guard(self):
        # llamafile path must still drop sub-10-char responses
        with patch.object(hyde.chat_backend, "call",
                          return_value="short"):
            out = hyde.expand_query("p", model_ref="llamafile://g",
                                    fallback_model_ref=None,
                                    fallback_model="",
                                    cache_enabled=False)
        # Falls back to raw prompt since both primary + fallback empty
        self.assertEqual(out, "p")

    def test_grounded_llamafile_dispatch(self):
        with patch.object(hyde.chat_backend, "call",
                          return_value="grounded ans long enough") as cb:
            out = hyde.expand_query_with_context(
                "q", ["mem1 long enough text"],
                model_ref="llamafile://g",
                cache_enabled=False,
            )
        self.assertEqual(out, "grounded ans long enough")
        cb.assert_called()


class TestReflectDispatch(unittest.TestCase):

    def test_bare_ref_uses_legacy_ollama(self):
        with patch.object(reflect.urllib.request, "urlopen") as uo:
            uo.return_value.__enter__.return_value.read.return_value = (
                b'{"response":"- a rule\\n- another rule"}'
            )
            rules = reflect._call_ollama_reflect(
                "text", model="gemma4:e2b",
                url="http://x/api/generate", num_ctx=8192,
            )
        self.assertEqual(rules, ["- a rule", "- another rule"])

    def test_llamafile_ref_uses_chat_backend(self):
        from claude_hooks import chat_backend
        with patch.object(chat_backend, "call",
                          return_value="- rule one\n- rule two") as cb:
            rules = reflect._call_ollama_reflect(
                "text", model="llamafile://gemma",
                url="http://x", num_ctx=8192,
            )
        self.assertEqual(rules, ["- rule one", "- rule two"])
        cb.assert_called_once()
        self.assertEqual(cb.call_args.kwargs["model_ref"], "llamafile://gemma")

    def test_llamafile_ref_empty_returns_no_rules(self):
        from claude_hooks import chat_backend
        with patch.object(chat_backend, "call", return_value=""):
            self.assertEqual(
                reflect._call_ollama_reflect("t", model="llamafile://g",
                                             url="x", num_ctx=8192),
                [],
            )


class TestConsolidateDispatch(unittest.TestCase):

    def test_bare_ref_uses_legacy_ollama(self):
        with patch.object(consolidate.urllib.request, "urlopen") as uo:
            uo.return_value.__enter__.return_value.read.return_value = (
                b'{"response":"compressed text"}'
            )
            out = consolidate._compress(
                "long text", model="gemma4:e2b",
                url="http://x/api/generate", num_ctx=8192,
            )
        self.assertEqual(out, "compressed text")

    def test_llamafile_ref_uses_chat_backend(self):
        from claude_hooks import chat_backend
        with patch.object(chat_backend, "call",
                          return_value="compressed via llamafile") as cb:
            out = consolidate._compress(
                "long text", model="llamafile://gemma",
                url="x", num_ctx=8192,
            )
        self.assertEqual(out, "compressed via llamafile")
        cb.assert_called_once()
        self.assertEqual(cb.call_args.kwargs["model_ref"], "llamafile://gemma")

    def test_llamafile_ref_empty_returns_none(self):
        from claude_hooks import chat_backend
        with patch.object(chat_backend, "call", return_value=""):
            self.assertIsNone(consolidate._compress(
                "t", model="llamafile://g", url="x", num_ctx=8192,
            ))


if __name__ == "__main__":
    unittest.main()
