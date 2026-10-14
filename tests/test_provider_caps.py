"""ProviderCaps probes and the Ollama account reads (plan A1/A3).

Fixtures are the shapes live servers answered with on 2026-10-14
(Ollama 0.13 local proxy: ``/api/me``, model recommendations) and the
opencoti ``/props`` / ``/health`` fields documented in report 01.
"""

from __future__ import annotations

import urllib.error

import pytest

from claude_hooks import ollama_account, provider_caps


RECS = {"recommendations": [
    {"model": "gemma4:31b-cloud", "context_length": 262144,
     "max_output_tokens": 262144, "required_plan": "free",
     "thinking": {"values": [False, True], "default": False}},
    {"model": "glm-5.3-flash:cloud", "context_length": 1048576,
     "max_output_tokens": 1048576, "required_plan": "pro",
     "thinking": {"values": ["low", "high", "max"], "default": "max"}},
    {"model": "qwen3.5:4b", "vram_bytes": 6000000000, "context_length": 0,
     "thinking": {"values": [False, True], "default": True}},
]}
TAGS = {"models": [
    {"name": "gemma4:31b-cloud", "remote_host": "https://ollama.com:443",
     "remote_model": "gemma4:31b", "details": {"family": "gemma4"}},
    {"name": "glm-tpl2:latest", "remote_host": "https://ollama.com:443",
     "remote_model": "glm-5.3-flash:cloud"},
    {"name": "qwen3:8b", "details": {"family": "qwen3"},
     "capabilities": ["completion", "tools", "thinking"]},
]}
SHOW_LOCAL = {
    "parameters": 'num_ctx 32768\ntemperature 0.6\nstop "<|im_end|>"\n'
                  'think_budget_message "Time is up.\\nAnswer now."',
    "model_info": {"qwen3.context_length": 40960},
    "details": {"family": "qwen3"},
    "capabilities": ["completion", "tools", "thinking"],
}


class FakeServer:
    """``fetch(url, data=None, headers=None) -> (status, body)``."""

    def __init__(self, routes: dict, down: bool = False):
        self.routes = routes
        self.down = down
        self.calls: list[str] = []

    def __call__(self, url, *, data=None, headers=None, timeout=None):
        self.calls.append(url)
        if self.down:
            raise urllib.error.URLError("connection refused")
        for suffix, answer in self.routes.items():
            if url.endswith(suffix):
                return answer
        return 404, None


@pytest.fixture(autouse=True)
def _clean():
    provider_caps.clear_cache()
    ollama_account.clear_cache()
    yield
    provider_caps.clear_cache()
    ollama_account.clear_cache()


def _server(**extra):
    routes = {
        "/api/tags": (200, TAGS),
        "/api/experimental/model-recommendations": (200, RECS),
        "/api/show": (200, SHOW_LOCAL),
        "/api/me": (200, {"name": "someone", "email": "x@y", "plan": "pro"}),
    }
    routes.update(extra)
    return FakeServer(routes)


def test_show_parsing_reads_modelfile_facts():
    f = provider_caps.caps_fields_from_show(SHOW_LOCAL)
    assert f["declared_num_ctx"] == 32768
    assert f["trained_ctx"] == 40960
    assert f["temperature"] == 0.6
    assert f["family"] == "qwen3"
    assert f["budget_message"] == "Time is up.\nAnswer now."
    assert "thinking" in f["capabilities"]


def test_local_model_caps():
    srv = _server()
    caps = provider_caps.ollama_caps("http://h", "qwen3:8b", fetch=srv)
    assert caps.backend == provider_caps.OLLAMA
    assert caps.cloud is False
    assert caps.declared_num_ctx == 32768
    assert caps.thinking is True and caps.tools is True
    assert caps.context_length is None  # no recommendation for it


def test_cloud_model_caps_come_from_recommendations():
    srv = _server(**{"/api/show": (200, {"model_info": {}})})
    caps = provider_caps.ollama_caps("http://h", "gemma4:31b-cloud", fetch=srv)
    assert caps.cloud is True
    assert caps.remote_host == "https://ollama.com:443"
    assert caps.context_length == 262144
    assert caps.max_output_tokens == 262144
    assert caps.thinking_values == ("off", "on")
    assert caps.thinking_default == "off"
    assert caps.required_plan == "free"


def test_retemplated_cloud_tag_matches_by_remote_model():
    srv = _server(**{"/api/show": (200, {})})
    caps = provider_caps.ollama_caps("http://h", "glm-tpl2:latest", fetch=srv)
    assert caps.cloud is True  # remote_host, not the name, decides
    assert caps.thinking_values == ("low", "high", "max")
    assert caps.required_plan == "pro"


def test_local_recommendation_zero_window_means_model_decides():
    recs = ollama_account.parse_recommendations(RECS)
    qwen = next(r for r in recs if r["model"] == "qwen3.5:4b")
    assert "context_length" not in qwen
    assert qwen["vram_bytes"] == 6000000000


def test_xollama_detected_by_features_not_version():
    srv = _server(**{"/api/xollama": (200, {"xollama": True, "version": "0.0.0",
                                            "features": ["context_window_v1",
                                                         "council_chat_state_v1"]})})
    caps = provider_caps.ollama_caps("http://x", "qwen3:8b", fetch=srv)
    assert caps.backend == provider_caps.XOLLAMA
    assert caps.has("context_window_v1")


def test_stock_ollama_404_on_xollama_route():
    caps = provider_caps.ollama_caps("http://h", "qwen3:8b", fetch=_server())
    assert caps.backend == provider_caps.OLLAMA
    assert caps.features == ()


def test_unreachable_server_is_not_cached_and_falls_back_to_name_rule():
    down = FakeServer({}, down=True)
    caps = provider_caps.ollama_caps("http://h", "kimi-k2.6:cloud", fetch=down)
    assert caps.cloud is True  # name rule, only because nothing answered
    assert caps.context_length is None
    assert provider_caps.cached("http://h", "kimi-k2.6:cloud") is None


def test_caps_are_cached_per_server_and_model():
    srv = _server()
    provider_caps.ollama_caps("http://h", "qwen3:8b", fetch=srv)
    n = len(srv.calls)
    provider_caps.ollama_caps("http://h", "qwen3:8b", fetch=srv)
    assert len(srv.calls) == n
    provider_caps.ollama_caps("http://other", "qwen3:8b", fetch=srv)
    assert len(srv.calls) > n


def test_opencoti_props_and_boot_id():
    srv = FakeServer({
        "/health": (200, {"status": "ok", "boot_id": "b1"}),
        "/props": (200, {"build_info": "b9999-ignored",
                         "features": ["polykv_pools_v1", "kv_pressure_v1"],
                         "total_slots": 4,
                         "opencoti": {"polykv": {"pools_enabled": True},
                                      "kv": {"kv_pool_cells": 2097152,
                                             "session_ctx_max": 262144,
                                             "largest_admissible": 262144}},
                         "default_generation_settings": {
                             "n_ctx": 65536, "params": {"temperature": 0.7}}}),
    })
    caps = provider_caps.openai_compat_caps("http://o/v1", "m", fetch=srv)
    assert caps.backend == provider_caps.OPENCOTI
    assert caps.boot_id == "b1"
    assert caps.declared_num_ctx == 65536
    assert caps.temperature == 0.7
    assert caps.has("kv_pressure_v1")
    assert caps.kv_pool_cells == 2097152
    assert caps.session_ctx_max == 262144
    assert caps.largest_admissible == 262144
    assert caps.total_slots == 4
    stale = provider_caps.with_boot_id(caps, "b2")
    assert stale.boot_id == "b2"
    assert provider_caps.cached("http://o/v1", "m") is None


def test_whoami_keeps_only_plan_and_signed_in():
    acct = ollama_account.whoami("http://h", fetch=_server())
    assert acct == {"reachable": True, "signed_in": True, "plan": "pro"}


def test_whoami_signed_out_and_unreachable():
    out = ollama_account.parse_whoami(401, {"signin_url": "https://ollama.com/connect?x"})
    assert out["signed_in"] is False and out["signin_url"].startswith("https://")
    assert ollama_account.parse_whoami(503, None) == {"reachable": False,
                                                      "signed_in": False}
    assert ollama_account.parse_whoami(200, {"Name": "n"})["plan"] == "free"


def test_status_flags_plan_shortfall():
    srv = _server(**{"/api/me": (200, {"name": "n", "plan": "free"})})
    st = ollama_account.status("http://h", fetch=srv)
    rows = {r["model"]: r for r in st["models"]}
    assert rows["glm-5.3-flash:cloud"]["plan_ok"] is False
    assert rows["glm-5.3-flash:cloud"]["pulled"] is False
    assert rows["gemma4:31b-cloud"]["plan_ok"] is True
    assert "qwen3.5:4b" not in rows  # local recommendations are not listed


def test_account_cli_prints_status(monkeypatch, capsys):
    import json as _json
    from consultants import cli
    seen = {}

    def fake_status(url, models=None):
        seen["url"], seen["models"] = url, models
        return {"base_url": url, "account": {"reachable": True,
                                             "signed_in": True, "plan": "pro"},
                "models": []}

    monkeypatch.setattr(ollama_account, "status", fake_status)
    rc = cli.main(["account", "--ollama-url", "http://h/", "--model", "a:cloud"])
    assert rc == 0
    out = _json.loads(capsys.readouterr().out)
    assert out["account"]["plan"] == "pro"
    assert seen == {"url": "http://h", "models": ["a:cloud"]}
