"""OpenAI-compatible client (opencoti / llama.cpp), ``@endpoint``
references, per-model routing and the ``[endpoints]`` config (plan A1).

Stream and reply shapes follow what the live opencoti on :8240 answered
on 2026-10-14 (llama.cpp ``timings``, ``reasoning_content`` deltas,
tool-call deltas by index, ``X-Context-Window``).
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from claude_hooks.get_advice import chat_client as cc


class _Resp:
    def __init__(self, body=None, lines=None, headers=None):
        self._body = json.dumps(body or {}).encode()
        self._lines = lines or []
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body

    def __iter__(self):
        return iter(self._lines)


def _sse(*objs, done=True, ping=False):
    out = [b": ping\n"] if ping else []
    out += [b"data: " + json.dumps(o).encode() + b"\n" for o in objs]
    if done:
        out.append(b"data: [DONE]\n")
    return out


class _Seq:
    def __init__(self, items):
        self.items, self.requests = list(items), []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        return self.items.pop(0)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("CLAUDE_HOOKS_OLLAMA_SLOTS_DISABLE", "1")
    monkeypatch.setattr(cc.time, "sleep", lambda s: None)


def _client(**kw):
    c = cc.OpenAICompatChatClient("http://oc:8240/v1", **kw)
    c.served_model = "/models/q.gguf"
    return c


def test_request_body_uses_llamacpp_names_and_served_model():
    seq = _Seq([_Resp({"choices": [{"message": {"content": "ok"},
                                    "finish_reason": "stop"}],
                       "usage": {"prompt_tokens": 5, "completion_tokens": 1}})])
    with patch("urllib.request.urlopen", side_effect=seq):
        _client().chat({"model": "@bs2", "messages": [{"role": "user", "content": "x"}],
                        "think": False,
                        "options": {"num_predict": 64, "temperature": 0.3,
                                    "num_keep": 4, "think_budget_message": "stop",
                                    "seed": 7, "top_k": None}})
    req = seq.requests[0]
    assert req.full_url == "http://oc:8240/v1/chat/completions"
    body = json.loads(req.data)
    assert body["model"] == "/models/q.gguf"
    assert body["max_tokens"] == 64 and body["temperature"] == 0.3
    assert body["n_keep"] == 4 and body["seed"] == 7
    assert body["reasoning_budget_message"] == "stop"
    assert body["reasoning_budget_tokens"] == 0  # think: false on opencoti
    assert "top_k" not in body and "num_predict" not in body and "think" not in body


def test_think_false_is_not_translated_for_plain_llamacpp():
    c = _client(backend="llamacpp")
    body = c._prepare({"model": "m", "messages": [], "think": False},
                      strip_think=False, stream=False)
    assert "reasoning_budget_tokens" not in body


def test_reply_and_timings_are_translated():
    reply = {"choices": [{"message": {"role": "assistant", "content": "PONG",
                                      "reasoning_content": "think"},
                          "finish_reason": "length"}],
             "usage": {"prompt_tokens": 18, "completion_tokens": 3},
             "timings": {"cache_n": 10, "prompt_n": 8, "prompt_ms": 50.0,
                         "predicted_n": 3, "predicted_ms": 30.0}}
    seq = _Seq([_Resp(reply, headers={"X-Context-Window": "83"})])
    with patch("urllib.request.urlopen", side_effect=seq):
        out = _client().chat({"model": "@bs2", "messages": []})
    ch = out["choices"][0]
    assert ch["message"]["content"] == "PONG"
    assert ch["message"]["reasoning_content"] == "think"
    assert ch["done_reason"] == "length"
    assert out["usage"]["prompt_tokens"] == 18
    t = out["timings"]
    assert t["cached_prompt_tokens"] == 10
    assert t["prompt_eval_ms"] == 50 and t["eval_ms"] == 30
    assert t["granted_window"] == 83


def test_sse_stream_merges_content_reasoning_and_tool_call_fragments():
    lines = _sse(
        {"choices": [{"delta": {"reasoning_content": "hm"}}]},
        {"choices": [{"delta": {"content": "Re"}}]},
        {"choices": [{"delta": {"content": "ading"}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1",
            "function": {"name": "read_file", "arguments": "{\"pa"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0,
            "function": {"arguments": "th\":\"a\"}"}}]},
                      "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 4},
         "timings": {"cache_n": 2}},
        ping=True)
    seen = []
    with patch("urllib.request.urlopen", side_effect=_Seq([_Resp(lines=lines)])):
        out = _client().chat_streamed({"model": "@bs2", "messages": []},
                                      on_token=seen.append)
    ch = out["choices"][0]
    assert ch["message"]["content"] == "Reading"
    assert ch["message"]["reasoning_content"] == "hm"
    assert ch["finish_reason"] == "tool_calls"
    tc = ch["message"]["tool_calls"][0]
    assert tc["id"] == "c1" and tc["function"]["name"] == "read_file"
    assert json.loads(tc["function"]["arguments"]) == {"path": "a"}
    assert out["usage"]["prompt_tokens"] == 9
    assert seen[0] == ""  # the keepalive ping counted as liveness
    assert "ttft_ms" in out["timings"]


def test_stream_error_frame_is_a_transport_retry():
    bad = _Resp(lines=_sse({"error": {"message": "slot died"}}, done=False))
    good = _Resp(lines=_sse({"choices": [{"delta": {"content": "ok"},
                                          "finish_reason": "stop"}]}))
    with patch("urllib.request.urlopen", side_effect=_Seq([bad, good])):
        out = _client(max_retries=2, retry_base_delay_s=0.0).chat_streamed(
            {"model": "@bs2", "messages": []})
    assert out["choices"][0]["message"]["content"] == "ok"


def test_endpoint_refs():
    assert cc.parse_endpoint_ref("@bs2") == ("bs2", None)
    assert cc.parse_endpoint_ref("@bs2/qwen3:8b") == ("bs2", "qwen3:8b")
    assert cc.parse_endpoint_ref("qwen3:8b") is None
    assert cc.parse_endpoint_ref("igovet/glm-5.2-opencode:latest") is None
    assert cc.route_key("@a/x") != cc.route_key("@b/x")
    assert cc.route_key("glm:cloud") == cc.route_key("qwen3:8b") == "ollama"


def test_factory_builds_the_backend_the_endpoint_names():
    eps = {"oc": {"url": "http://oc:8240", "backend": "opencoti", "model": "m.gguf"},
           "xo": {"url": "http://xo:22434", "backend": "xollama", "api_key": "k"}}
    oc = cc.make_agent_chat_client("@oc", "http://ollama", endpoints=eps)
    assert isinstance(oc, cc.OpenAICompatChatClient) and oc.served_model == "m.gguf"
    xo = cc.make_agent_chat_client("@xo/qwen3:8b", "http://ollama", endpoints=eps)
    assert type(xo) is cc.ChatClient
    assert xo.base_url == "http://xo:22434" and xo.api_key == "k"
    assert xo._real("@xo/qwen3:8b") == "qwen3:8b"
    with pytest.raises(ValueError, match="not configured"):
        cc.make_agent_chat_client("@nope", "http://ollama", endpoints=eps)


def test_role_client_is_plain_when_all_models_share_a_backend():
    c = cc.make_role_chat_client(["glm:cloud", "kimi:cloud"], "http://ollama")
    assert type(c) is cc.ChatClient


def test_role_client_routes_each_lane_by_its_model():
    eps = {"oc": {"url": "http://oc:8240", "backend": "opencoti", "model": "m"}}
    c = cc.make_role_chat_client(["glm:cloud", "@oc"], "http://ollama",
                                 endpoints=eps)
    assert isinstance(c, cc.ModelRoutingChatClient)
    ollama_reply = _Resp({"message": {"content": "from ollama"}, "done": True,
                          "done_reason": "stop"})
    oc_reply = _Resp({"choices": [{"message": {"content": "from oc"},
                                   "finish_reason": "stop"}]})
    seq = _Seq([ollama_reply, oc_reply])
    with patch("urllib.request.urlopen", side_effect=seq):
        a = c.chat({"model": "glm:cloud", "messages": []})
        b = c.chat({"model": "@oc", "messages": []})
    assert a["choices"][0]["message"]["content"] == "from ollama"
    assert b["choices"][0]["message"]["content"] == "from oc"
    assert seq.requests[0].full_url == "http://ollama/api/chat"
    assert seq.requests[1].full_url == "http://oc:8240/v1/chat/completions"
    assert c.last_timings is b["timings"]


def test_endpoint_config_roundtrip_and_cli(tmp_path, monkeypatch, capsys):
    from consultants import cli
    from consultants import config as ccfg
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["config", "set-endpoint", "bs2", "--url",
                   "http://192.168.178.2:8240/", "--backend", "opencoti",
                   "--api-key", "secret", "--user"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["endpoints"]["bs2"] == {"url": "http://192.168.178.2:8240",
                                       "backend": "opencoti", "model": None,
                                       "api_key_set": True,
                                       "key_file": None,
                                       "window": "dynamic"}
    assert "secret" not in json.dumps(out)
    cfg = ccfg.load_config(tmp_path)
    assert cfg.endpoints["bs2"].api_key == "secret"
    with pytest.raises(SystemExit):
        cli.main(["config", "set-endpoint", "x", "--url", "http://h",
                  "--backend", "vllm", "--user"])
    capsys.readouterr()
    assert cli.main(["config", "remove-endpoint", "bs2", "--user"]) == 0
    assert "bs2" not in ccfg.load_config(tmp_path).endpoints
