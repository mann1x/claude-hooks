"""Plan Milestone B3: thinking per backend, level fixed per (role, model)."""
from __future__ import annotations

import io
import json
import urllib.error
from unittest.mock import patch

import pytest

from claude_hooks import provider_caps
from claude_hooks.get_advice import chat_client as cc
from consultants.engine import council


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("CLAUDE_HOOKS_OLLAMA_SLOTS_DISABLE", "1")
    monkeypatch.setattr(cc.time, "sleep", lambda s: None)
    provider_caps.clear_cache()
    yield
    provider_caps.clear_cache()


class _Resp:
    def __init__(self, body, headers=None):
        self._b = json.dumps(body).encode()
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._b


class _Seq:
    def __init__(self, items):
        self.items, self.bodies = list(items), []

    def __call__(self, req, timeout=None):
        self.bodies.append(json.loads(req.data) if req.data else None)
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _ok(**extra):
    return _Resp({"message": {"content": "ok"}, "done": True,
                  "done_reason": "stop", **extra})


def _client():
    c = cc.ChatClient("http://h")
    c._probed_think["m"] = True  # no /api/show round trip
    return c


# --- values ------------------------------------------------------------- #

@pytest.mark.parametrize("raw,norm", [
    ("auto", True), ("on", True), (True, True), ("off", False), (False, False),
    ("xhigh", "max"), ("HIGH", "high"), (8192, 8192), ("4096", 4096), (0, False),
])
def test_normalize(raw, norm):
    assert cc.normalize_think(raw) == norm


def test_fractions_of_the_smaller_bound():
    assert cc.think_budget_tokens("max", 10_000, 262_144) == 8_000
    assert cc.think_budget_tokens("high", 96_000, 32_768) == 16_384
    assert cc.think_budget_tokens("minimal", 16_000) == 1_000
    assert cc.think_budget_tokens("xhigh", None, 10_000) == 8_000
    assert cc.think_budget_tokens(5_000, None) == 5_000
    assert cc.think_budget_tokens(True, 10_000) is None
    assert cc.think_budget_tokens("medium", None, None) is None


# --- Ollama ------------------------------------------------------------- #

def test_ollama_level_sent_only_when_the_service_accepts_it():
    provider_caps._store(provider_caps.ProviderCaps(
        "http://h", "m", cloud=True, thinking_values=("off", "on")))
    seq = _Seq([_ok()])
    with patch("urllib.request.urlopen", side_effect=seq):
        _client().chat({"model": "m", "messages": [], "think": "high"})
    assert seq.bodies[0]["think"] is True
    provider_caps._store(provider_caps.ProviderCaps(
        "http://h", "m", cloud=True, thinking_values=("off", "low", "high", "max")))
    seq = _Seq([_ok()])
    with patch("urllib.request.urlopen", side_effect=seq):
        _client().chat({"model": "m", "messages": [], "think": "xhigh"})
    assert seq.bodies[0]["think"] == "max"


def test_ollama_token_count_is_think_true_plus_a_budget_read_back():
    seq = _Seq([_ok(think_budget_tokens=4096)])
    with patch("urllib.request.urlopen", side_effect=seq):
        out = _client().chat({"model": "m", "messages": [], "think": 4096})
    assert seq.bodies[0]["think"] is True
    assert seq.bodies[0]["options"]["think_budget"] == 4096
    assert out["timings"]["think_budget_tokens"] == 4096
    assert out["timings"]["think_budget_source"] == "read_back"


def _http400(msg):
    return urllib.error.HTTPError("http://h/api/chat", 400, "bad", {},
                                  io.BytesIO(msg.encode()))


def test_a_refused_level_is_retried_as_true_before_stripping():
    seq = _Seq([_http400('{"error":"invalid \'think\' value: max"}'), _ok()])
    with patch("urllib.request.urlopen", side_effect=seq):
        _client().chat({"model": "m", "messages": [], "think": "max"})
    assert [b.get("think") for b in seq.bodies] == ["max", True]


def test_a_refused_true_is_stripped_as_before():
    seq = _Seq([_http400('{"error":"\\"m\\" does not support thinking"}'), _ok()])
    c = _client()
    with patch("urllib.request.urlopen", side_effect=seq):
        c.chat({"model": "m", "messages": [], "think": True})
    assert "think" not in seq.bodies[1]
    assert "m" in c._unsupported_think


# --- opencoti ----------------------------------------------------------- #

def _oc():
    c = cc.OpenAICompatChatClient("http://oc:8240")
    c.served_model = "q.gguf"
    c._context_length["@bs2"] = 262_144
    return c


@pytest.mark.parametrize("think,tokens", [
    ("high", 16_000), ("max", 25_600), ("minimal", 2_000), (3_000, 3_000),
    (False, 0)])
def test_opencoti_level_becomes_reasoning_budget_tokens(think, tokens):
    body = _oc()._prepare({"model": "@bs2", "messages": [], "think": think,
                           "options": {"num_predict": 32_000}},
                          strip_think=False, stream=False)
    assert body["reasoning_budget_tokens"] == tokens


def test_opencoti_true_leaves_the_model_budget():
    body = _oc()._prepare({"model": "@bs2", "messages": [], "think": True,
                           "options": {"num_predict": 32_000}},
                          strip_think=False, stream=False)
    assert "reasoning_budget_tokens" not in body


def test_sent_budget_lands_in_timings():
    reply = _Resp({"choices": [{"message": {"content": "ok"},
                                "finish_reason": "stop"}]})
    with patch("urllib.request.urlopen", side_effect=_Seq([reply])):
        out = _oc().chat({"model": "@bs2", "messages": [], "think": "medium",
                          "options": {"num_predict": 8_000}})
    assert out["timings"]["think_budget_tokens"] == 2_000
    assert out["timings"]["think_budget_source"] == "sent"


# --- capped thinking compares against the allowance --------------------- #

def test_capped_detection_uses_the_enforced_budget():
    from claude_hooks.agent_loop import runner
    seen = {}

    def fake_detect(msg, *, budget_tokens, budget_message, completion_tokens):
        seen["budget"], seen["message"] = budget_tokens, budget_message
        return None, "reasoning-within-budget"

    cfg = runner.LoopConfig(thinking_budget_message="Out of budget.")
    with patch.object(runner.capped_thinking, "detect", fake_detect):
        runner._capped_thinking_note(
            {"thinking": "x"}, {"timings": {"think_budget_tokens": 2_000}},
            {"options": {"num_predict": 32_000}}, cfg, lambda p: {},
            iteration=1)
    assert seen == {"budget": 2_000, "message": "Out of budget."}
    with patch.object(runner.capped_thinking, "detect", fake_detect):
        runner._capped_thinking_note(
            {"thinking": "x"}, {}, {"options": {"num_predict": 32_000}},
            cfg, lambda p: {}, iteration=1)
    assert seen["budget"] == 32_000  # no allowance known: the output cap


# --- (role, model) resolution + config ---------------------------------- #

def test_per_model_think_beats_the_role_and_never_follows_effort():
    state = {"runtime_control": {"think": {"critic@glm:cloud": "max"}}}
    assert council._think_for(state, "critic", "glm:cloud", "medium") == "max"
    assert council._think_for(state, "critic", "other", "medium") == "medium"
    assert council._think_for({}, "critic", "glm:cloud", "medium") == "medium"


def test_think_share_is_stated_only_where_it_is_known():
    class _C:
        def __init__(self, backend):
            self._caps = provider_caps.ProviderCaps("http://h", "m",
                                                    backend=backend)

        def caps(self, model):
            return self._caps
    assert council._stated_think_share(_C("opencoti"), "m", "high",
                                       32_000, 262_144) == ("high", 16_000)
    assert council._stated_think_share(_C("ollama"), "m", "high",
                                       32_000, 262_144) is None
    assert council._stated_think_share(_C("ollama"), "m", 4_000,
                                       32_000, None) == ("4000", 4_000)
    assert council._stated_think_share(_C("opencoti"), "m", True,
                                       32_000, None) is None


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_think_config_roundtrip_seed_and_cli(home, capsys):
    from consultants import cli
    from consultants import config as ccfg
    from consultants.engine import control
    assert cli.main(["config", "set-think", "critic", "xhigh", "--user"]) == 0
    assert cli.main(["config", "set-think", "researcher", "8192", "--model",
                     "@bs2", "--user"]) == 0
    capsys.readouterr()
    cfg = ccfg.load_config(home)
    assert cfg.roles["critic"].think == "max"
    assert cfg.roles["researcher"].model_overrides == {"@bs2": {"think": 8192}}
    # A later unrelated save keeps the role think (it used to be dropped).
    ccfg.set_budget({"announce": False}, scope="user", cwd=home)
    assert ccfg.load_config(home).roles["critic"].think == "max"
    rc = control.runtime_control_defaults(ccfg.load_config(home), effort="high")
    assert rc["think"] == {"researcher@@bs2": 8192, "critic": "max"}
    assert cli.main(["config", "set-think", "critic", "huge", "--user"]) == 2
    assert cli.main(["config", "set-think", "researcher", "--model", "@bs2",
                     "--clear", "--user"]) == 0
    assert ccfg.load_config(home).roles["researcher"].model_overrides == {}


def test_default_run_seeds_no_think_record():
    from consultants import config as ccfg
    from consultants.engine import control
    rc = control.runtime_control_defaults(ccfg.ConsultantsConfig(), effort="high")
    assert "think" not in rc


# --- closing B: template layer, read-back, loaded window ---------------- #

def test_template_think_sits_between_explicit_role_and_default(monkeypatch):
    import claude_hooks.model_sampling as ms
    monkeypatch.setattr(ms, "templates", lambda cfg=None, shipped=None:
                        {"glm*": {"think": "high", "temperature": 0.7}})
    assert ms.sampling_for("glm-5.3:cloud") == {"temperature": 0.7}  # never sent
    assert council._think_for({}, "critic", "glm-5.3:cloud", "medium") == "high"
    explicit = {"runtime_control": {"think": {"critic": "low"}}}
    assert council._think_for(explicit, "critic", "glm-5.3:cloud", "low") == "low"
    per_model = {"runtime_control": {"think": {"critic": "low",
                                               "critic@glm-5.3:cloud": "max"}}}
    assert council._think_for(per_model, "critic", "glm-5.3:cloud", "low") == "max"
    assert council._think_for({}, "critic", "other", "medium") == "medium"


def test_think_budget_read_back_is_cached_and_skips_cloud():
    c = cc.ChatClient("http://h")
    seq = _Seq([_Resp({"think_budget_tokens": 2000})])
    with patch("urllib.request.urlopen", side_effect=seq):
        assert c.think_budget("m", "medium", 8000, 32768) == 2000
        assert c.think_budget("m", "medium", 8000, 32768) == 2000  # cached
    assert seq.bodies == [{"model": "m", "think": "medium",
                           "options": {"num_predict": 8000, "num_ctx": 32768}}]
    provider_caps._store(provider_caps.ProviderCaps("http://h", "x:cloud",
                                                    cloud=True))
    with patch("urllib.request.urlopen", side_effect=AssertionError):
        assert c.think_budget("x:cloud", "high", 8000) is None


def test_stated_share_reads_back_on_a_local_ollama_server():
    class _C:
        def caps(self, model):
            return provider_caps.ProviderCaps("http://h", "m",
                                              backend="xollama", cloud=False)

        def think_budget(self, model, think, cap, ctx):
            return 4000 if think == "high" else None
    assert council._stated_think_share(_C(), "m", "high", 8000, 32768) \
        == ("high", 4000)
    assert council._stated_think_share(_C(), "m", "low", 8000, 32768) is None


def test_streamed_final_chunk_keeps_the_enforced_budget():
    c = cc.ChatClient("http://h")
    c._probed_think["m"] = True
    lines = [json.dumps({"message": {"role": "assistant", "content": "No."},
                         "done": False}).encode() + b"\n",
             json.dumps({"done": True, "done_reason": "stop", "eval_count": 3,
                         "think_budget": "low", "think_budget_tokens": 500,
                         "cached_prompt_tokens": 12}).encode() + b"\n"]

    class _S(_Resp):
        def __iter__(self):
            return iter(lines)
    with patch("urllib.request.urlopen", side_effect=_Seq([_S({})])):
        out = c.chat_streamed({"model": "m", "messages": [], "think": "low"})
    assert out["timings"]["think_budget_tokens"] == 500
    assert out["timings"]["think_budget_source"] == "read_back"
    assert out["timings"]["cached_prompt_tokens"] == 12


def test_xollama_window_is_the_loaded_one_then_trained():
    from consultants.engine import budget

    class _C:
        def __init__(self, loaded):
            self.loaded = loaded

        def caps(self, model):
            return provider_caps.ProviderCaps(
                "http://h", "m", backend="xollama", cloud=False,
                trained_ctx=262_144)

        def loaded_context(self, model):
            return self.loaded
    w = budget.resolve_window(_C(32_768), "m")
    assert (w.tokens, w.source, w.send_num_ctx) == (32_768, "loaded", None)
    # Not loaded yet: the trained context (owner, 2026-10-19), never sent.
    w = budget.resolve_window(_C(None), "m")
    assert (w.tokens, w.source, w.send_num_ctx) == (262_144, "trained", None)


def test_loaded_context_reads_api_ps_and_caches_briefly():
    calls = []

    def fetch(url, **kw):
        calls.append(url)
        return 200, {"models": [{"name": "q:8b", "context_length": 32768}]}
    assert provider_caps.loaded_context("http://h", "q:8b", fetch=fetch) == 32768
    assert provider_caps.loaded_context("http://h", "q:8b", fetch=fetch) == 32768
    assert provider_caps.loaded_context("http://h", "other", fetch=fetch) is None
    assert calls == ["http://h/api/ps", "http://h/api/ps"]


def test_streamed_placed_call_keeps_xollamas_cache_count():
    # xOllama's final stream chunk names the share prompt_eval_cached_count;
    # the reader kept only cached_prompt_tokens, so every streamed PolyKV
    # call (every researcher turn) read as 0 cached (ch-10, 2026-10-24).
    c = cc.ChatClient("http://h")
    lines = [json.dumps({"message": {"role": "assistant", "content": "ok"},
                         "done": False}).encode() + b"\n",
             json.dumps({"done": True, "done_reason": "stop", "eval_count": 2,
                         "prompt_eval_count": 1224,
                         "prompt_eval_cached_count": 1152}).encode() + b"\n"]

    class _S(_Resp):
        def __iter__(self):
            return iter(lines)
    payload = {"model": "m", "messages": [{"role": "user", "content": "q"}],
               "placement": {"session_id": "run~researcher~x", "pool_id": "1"}}
    with patch("urllib.request.urlopen", side_effect=_Seq([_S({})])):
        out = c.chat_streamed(payload)
    assert out["timings"]["polykv"]["cache_n"] == 1152
