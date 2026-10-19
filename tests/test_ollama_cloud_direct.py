"""Ollama Cloud used directly (https://ollama.com, no local Ollama) — ch-15.

The key comes from ``ollama.api_key_file`` in claude-hooks.json, else
``OLLAMA_API_KEY``; it is sent only to ollama.com, and never written into
config output.
"""

from __future__ import annotations

import urllib.error

import pytest

from claude_hooks import ollama_account, ollama_slots, ollama_upstream
from claude_hooks.get_advice import chat_client, ctx_probe


KEY = "test-key-not-real"


def _cfg(tmp_path, key=KEY):
    f = tmp_path / "ollama-api-key"
    f.write_text(f"\n{key}\n", encoding="utf-8")
    return {"ollama": {"api_key_file": str(f)}}


# --- the resolver -------------------------------------------------------- #

def test_the_file_wins_over_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "from-env")
    key, src = ollama_upstream.api_key_source(_cfg(tmp_path))
    assert (key, src) == (KEY, "config ollama.api_key_file")
    key, src = ollama_upstream.api_key_source({})
    assert (key, src) == ("from-env", "env OLLAMA_API_KEY")


def test_an_unreadable_file_falls_back_to_the_environment_and_says_so(
        tmp_path, monkeypatch):
    cfg = {"ollama": {"api_key_file": str(tmp_path / "missing")}}
    key, src = ollama_upstream.api_key_source(cfg)
    assert key is None and "unreadable" in src
    monkeypatch.setenv("OLLAMA_API_KEY", "from-env")
    assert ollama_upstream.api_key_source(cfg)[0] == "from-env"


def test_the_key_goes_only_to_ollama_com(tmp_path):
    cfg = _cfg(tmp_path)
    assert ollama_upstream.api_key("https://ollama.com", cfg) == KEY
    assert ollama_upstream.api_key("https://ollama.com/v1", cfg) == KEY
    for lan in ("http://192.168.1.2:11434", "http://localhost:11434",
                "https://ollama.com.evil.example", "http://notollama.com"):
        assert ollama_upstream.api_key(lan, cfg) is None, lan
    assert ollama_upstream.auth_headers("http://h:11434", cfg) == {}
    assert ollama_upstream.auth_headers("https://ollama.com", cfg) == {
        "Authorization": f"Bearer {KEY}"}


def test_explain_names_the_source_never_the_key(tmp_path, monkeypatch, capsys):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(ollama_upstream, "_key_config", lambda: cfg)
    monkeypatch.setattr(ollama_upstream, "_host_config", lambda: {})
    ollama_upstream.main(["--explain"])
    out = capsys.readouterr().out
    assert "ollama cloud key: set  (config ollama.api_key_file)" in out
    assert KEY not in out


# --- the clients --------------------------------------------------------- #

def test_a_client_on_ollama_com_takes_the_host_key(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(ollama_upstream, "_key_config", lambda: cfg)
    assert chat_client.ChatClient("https://ollama.com").api_key == KEY
    assert chat_client.OpenAICompatChatClient(
        "https://ollama.com/v1", backend="ollama").api_key == KEY
    assert chat_client.ChatClient("http://h:11434").api_key is None
    # An explicit key is never replaced.
    assert chat_client.ChatClient("https://ollama.com",
                                  api_key="mine").api_key == "mine"


def test_an_endpoint_key_file_is_read_and_beats_the_host_key(tmp_path,
                                                             monkeypatch):
    monkeypatch.setattr(ollama_upstream, "_key_config",
                        lambda: _cfg(tmp_path, "host-key"))
    f = tmp_path / "ep-key"
    f.write_text("endpoint-key\n", encoding="utf-8")
    eps = {"cloud": {"url": "https://ollama.com", "backend": "ollama",
                     "key_file": str(f)}}
    c = chat_client.make_agent_chat_client("@cloud/gemma4:31b", "http://x",
                                           endpoints=eps)
    assert c.api_key == "endpoint-key" and c.served_model == "gemma4:31b"
    del eps["cloud"]["key_file"]
    c = chat_client.make_agent_chat_client("@cloud/gemma4:31b", "http://x",
                                           endpoints=eps)
    assert c.api_key == "host-key"


# --- connection slots and the plan --------------------------------------- #

def test_every_model_on_ollama_com_is_a_cloud_call():
    assert ollama_slots.is_cloud("https://ollama.com", "gemma4:31b")


def test_the_plan_is_read_from_ollama_com_with_the_key(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(ollama_upstream, "_key_config", lambda: cfg)
    monkeypatch.setattr(ollama_slots, "_plan_server", "https://ollama.com")
    seen = {}

    def whoami(base_url, *, headers=None, **kw):
        seen[base_url] = headers
        return {"reachable": True, "signed_in": True, "plan": "pro"}
    monkeypatch.setattr(ollama_account, "whoami", whoami)
    assert ollama_slots._fetch_plan() == "pro"
    assert seen == {"https://ollama.com": {"Authorization": f"Bearer {KEY}"}}


# --- probes never cache "could not ask" ---------------------------------- #

def test_ctx_probe_does_not_cache_a_network_failure(monkeypatch):
    ctx_probe._cache.clear()

    def down(req, timeout=None):
        raise urllib.error.URLError("timed out")
    monkeypatch.setattr(ctx_probe.urllib.request, "urlopen", down)
    assert ctx_probe.probe_max_ctx("m", "https://ollama.com") is None
    assert ("https://ollama.com", "m") not in ctx_probe._cache


# --- config -------------------------------------------------------------- #

def test_endpoint_key_file_round_trips_and_the_inline_key_warns(
        tmp_path, caplog):
    from consultants import config as cc
    raw = {"endpoints": {
        "cloud": {"url": "https://ollama.com", "backend": "ollama",
                  "key_file": "~/.claude/k"},
        "old": {"url": "https://h", "backend": "ollama", "api_key": "inline"}}}
    with caplog.at_level("WARNING"):
        cfg = cc._merge_layer(cc.ConsultantsConfig(), raw)
    assert cfg.endpoints["cloud"].key_file == "~/.claude/k"
    assert any("put it in a file" in r.message for r in caplog.records)
    assert 'key_file = "~/.claude/k"' in cc._render(cfg)


# --- verify_deploy -------------------------------------------------------- #

def test_verify_fails_when_the_engines_use_ollama_com_without_a_key():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import verify_deploy as v
    r = v.Results()
    v.check_ollama_cloud_key(r, {}, "https://ollama.com")
    assert [row[0] for row in r.rows] == [v.FAIL]
    r = v.Results()
    v.check_ollama_cloud_key(r, {}, "http://h:11434")
    assert r.rows == []
