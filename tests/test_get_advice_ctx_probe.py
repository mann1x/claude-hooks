"""Tests for ``claude_hooks.get_advice.ctx_probe``."""

from __future__ import annotations

from unittest.mock import patch


from claude_hooks.get_advice import ctx_probe


class TestExtractContextLength:
    def test_qwen_arch(self):
        data = {"model_info": {"qwen3.context_length": 128000,
                               "qwen3.something_else": 1}}
        assert ctx_probe._extract_context_length(data) == 128000

    def test_llama_arch(self):
        data = {"model_info": {"llama.context_length": 65536}}
        assert ctx_probe._extract_context_length(data) == 65536

    def test_string_value_coerced(self):
        data = {"model_info": {"foo.context_length": "32768"}}
        assert ctx_probe._extract_context_length(data) == 32768

    def test_zero_rejected(self):
        data = {"model_info": {"x.context_length": 0}}
        assert ctx_probe._extract_context_length(data) is None

    def test_falls_back_to_parameters_block(self):
        data = {"parameters": "stop \"<eos>\"\nnum_ctx 8192\n"}
        assert ctx_probe._extract_context_length(data) == 8192

    def test_no_keys_returns_none(self):
        assert ctx_probe._extract_context_length({}) is None
        assert ctx_probe._extract_context_length({"model_info": {}}) is None


class TestProbeCaching:
    def setup_method(self):
        ctx_probe.reset_cache()

    def test_cache_hit_avoids_second_call(self):
        calls = {"n": 0}

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def read(self):
                import json
                return json.dumps({"model_info":
                                   {"x.context_length": 16384}}).encode()

        def fake_open(req, timeout=10.0):
            calls["n"] += 1
            return _Resp()

        with patch("urllib.request.urlopen", side_effect=fake_open):
            v1 = ctx_probe.probe_max_ctx("foo", "http://x")
            v2 = ctx_probe.probe_max_ctx("foo", "http://x")
        assert v1 == 16384 and v2 == 16384
        assert calls["n"] == 1  # second call hit the cache

    def test_force_bypasses_cache(self):
        calls = {"n": 0}

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def read(self):
                import json
                return json.dumps({"model_info":
                                   {"x.context_length": 16384}}).encode()

        def fake_open(req, timeout=10.0):
            calls["n"] += 1
            return _Resp()

        with patch("urllib.request.urlopen", side_effect=fake_open):
            ctx_probe.probe_max_ctx("foo", "http://x")
            ctx_probe.probe_max_ctx("foo", "http://x", force=True)
        assert calls["n"] == 2


class TestBaseUrlDefault:
    def test_uses_env(self, monkeypatch):
        monkeypatch.setenv("CALIBER_GROUNDING_UPSTREAM",
                           "http://other.host:9999/v1")
        # /v1 stripped, trailing slash removed.
        assert ctx_probe.base_url_default() == "http://other.host:9999"

    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("CALIBER_GROUNDING_UPSTREAM", raising=False)
        assert ctx_probe.base_url_default() == ctx_probe.DEFAULT_BASE_URL


class TestProbeFailures:
    def setup_method(self):
        ctx_probe.reset_cache()

    def test_network_error_returns_none(self):
        import urllib.error

        def boom(req, timeout=10.0):
            raise urllib.error.URLError("network down")

        with patch("urllib.request.urlopen", side_effect=boom):
            assert ctx_probe.probe_max_ctx("zzz", "http://x") is None

    def test_empty_model_returns_none(self):
        assert ctx_probe.probe_max_ctx("", "http://x") is None
