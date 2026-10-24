"""Plan Milestone B1/B2: output cap, window resolution, the stated cap,
budget knobs and their layering, and the hard cap that follows the cap."""
from __future__ import annotations

import json

import pytest

from claude_hooks.provider_caps import ProviderCaps
from consultants.engine import budget, council
from consultants.engine.stall_chat import (
    make_hard_cap_only_chat_fn, scaled_hard_cap,
)


class _Caps:
    """A client that answers ``caps()`` like the real ones."""

    def __init__(self, caps=None, *, granted=None, learned=None, probe=None):
        self._caps, self._granted = caps, granted
        self._learned, self._probe = learned, probe
        self.calls: list[dict] = []

    def caps(self, model):
        return self._caps

    def granted_window(self, model):
        return self._granted

    def learned_output_ceiling(self, model):
        return self._learned

    def context_length(self, model):
        return self._probe

    def chat(self, payload):
        self.calls.append(payload)
        return {"choices": [{"message": {"content": "ok"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2}}


def _caps(**kw):
    return ProviderCaps("http://h", "m", **kw)


# --- B1 ------------------------------------------------------------------ #

def test_output_budget_is_the_smallest_bound():
    assert budget.resolve_output_budget(262_144) == 96_000          # ceiling
    assert budget.resolve_output_budget(40_000) == 30_000           # 0.75 × ctx
    assert budget.resolve_output_budget(262_144, 65_536) == 65_536  # model max
    assert budget.resolve_output_budget(None) == 32_000             # unknown
    assert budget.resolve_output_budget(None, 8_192) == 8_192
    low = budget.knobs_from({"output_ceiling": 20_000})
    assert budget.resolve_output_budget(262_144, knobs=low) == 20_000


def test_reserve_scales_with_the_estimate():
    assert budget.reserve_tokens(100) == 1_024
    assert budget.reserve_tokens(100_000) == 12_000
    assert budget.reserve_tokens(100_001) == 12_001  # rounded up


def test_model_max_comes_from_caps_then_the_learned_ceiling():
    assert budget.model_max_output_of(
        _Caps(_caps(max_output_tokens=131_072)), "m") == 131_072
    assert budget.model_max_output_of(_Caps(_caps(), learned=8_000), "m") == 8_000
    assert budget.model_max_output_of(_Caps(_caps()), "m") is None


def test_overflow_keeps_its_own_cap_and_flags_compaction():
    big = "x" * (39_000 * int(budget.CHARS_PER_TOKEN))
    c = _Caps(_caps(cloud=True, context_length=40_000))
    p = budget.plan(c, "m", [{"role": "user", "content": big}])
    assert p.needs_compaction and p.cap_source == "context-overflow"
    assert p.output_tokens == 30_000 == p.role_cap


# --- B2 ------------------------------------------------------------------ #

@pytest.mark.parametrize("caps,expect", [
    # cloud: recommendations, then trained; nothing sent
    (_caps(cloud=True, context_length=202_752, trained_ctx=1_048_576),
     (202_752, "context_length", None)),
    (_caps(cloud=True, trained_ctx=262_144), (262_144, "trained", None)),
    # local: the Modelfile window, sent as num_ctx
    (_caps(cloud=False, declared_num_ctx=65_536, trained_ctx=262_144),
     (65_536, "modelfile", 65_536)),
    # local without one: the trained context, sent so the model loads it
    (_caps(cloud=False, trained_ctx=262_144), (262_144, "trained", 262_144)),
    # local with nothing declared: the default
    (_caps(cloud=False),
     (budget.DEFAULT_WINDOW_TOKENS, "default", budget.DEFAULT_WINDOW_TOKENS)),
    # cloud declaring nothing: the default, nothing sent
    (_caps(cloud=True), (budget.DEFAULT_WINDOW_TOKENS, "default", None)),
    # opencoti: /props session max, then n_ctx; nothing sent
    (_caps(backend="opencoti", session_ctx_max=2_097_152, declared_num_ctx=8192),
     (2_097_152, "session_ctx_max", None)),
    (_caps(backend="opencoti", declared_num_ctx=8192), (8192, "n_ctx", None)),
    # xOllama: never sent (a num_ctx reloads it)
    (_caps(backend="xollama", cloud=False, declared_num_ctx=32_768),
     (32_768, "modelfile", None)),
    # xOllama not loaded yet: the trained context, still not sent
    (_caps(backend="xollama", cloud=False, trained_ctx=131_072),
     (131_072, "trained", None)),
])
def test_window_resolution_per_backend(caps, expect):
    w = budget.resolve_window(_Caps(caps), "m")
    assert (w.tokens, w.source, w.send_num_ctx) == expect


def test_xollama_prefers_the_loaded_window_to_the_trained_one():
    c = _Caps(_caps(backend="xollama", cloud=False, trained_ctx=262_144))
    c.loaded_context = lambda model: 65_536
    w = budget.resolve_window(c, "m")
    assert (w.tokens, w.source, w.send_num_ctx) == (65_536, "loaded", None)


def test_a_pin_below_the_trained_context_wins_on_local_ollama():
    pin = budget.knobs_from({"num_ctx": 32_768})
    w = budget.resolve_window(
        _Caps(_caps(cloud=False, trained_ctx=262_144)), "m", pin)
    assert (w.tokens, w.source, w.send_num_ctx) == (32_768, "pinned", 32_768)


def test_pinned_window_is_sent_to_local_ollama_and_xollama_only():
    pin = budget.knobs_from({"num_ctx": 16_384})
    local = budget.resolve_window(_Caps(_caps(cloud=False)), "m", pin)
    cloud = budget.resolve_window(_Caps(_caps(cloud=True)), "m", pin)
    xo = budget.resolve_window(
        _Caps(_caps(backend="xollama", cloud=False)), "m", pin)
    oc = budget.resolve_window(
        _Caps(_caps(backend="opencoti", declared_num_ctx=8192)), "m", pin)
    assert (local.tokens, local.send_num_ctx) == (16_384, 16_384)
    assert (cloud.tokens, cloud.send_num_ctx) == (16_384, None)
    # xOllama loads at its default without one (csl-2026-10-24-1927-bc05:
    # a 262,144 load came back at 32,768); a pin is the same every call.
    assert (xo.tokens, xo.send_num_ctx) == (16_384, 16_384)
    assert (oc.tokens, oc.send_num_ctx) == (16_384, None)


def test_granted_window_wins_and_no_caps_keeps_the_old_probe():
    w = budget.resolve_window(_Caps(_caps(cloud=True, context_length=1),
                                    granted=500_000), "m")
    assert (w.tokens, w.source) == (500_000, "granted")
    w = budget.resolve_window(_Caps(None, probe=131_072), "m")
    assert (w.tokens, w.source) == (131_072, "probe")


def test_budget_options_send_num_ctx_unless_the_caller_set_one():
    c = _Caps(_caps(cloud=False, declared_num_ctx=65_536))
    p = budget.plan(c, "m", [{"role": "user", "content": "q"}])
    assert council._budget_options(None, p) == {
        "num_predict": 49_152, "num_ctx": 65_536}
    assert council._budget_options({"num_ctx": 8192}, p)["num_ctx"] == 8192


# --- the stated cap ------------------------------------------------------ #

def test_section_is_stated_once_and_stays_put():
    msgs = [{"role": "system", "content": "You are the critic."},
            {"role": "user", "content": "q"}]
    once = budget.with_output_budget_section(msgs, 30_000, 40_000)
    assert once[0]["content"].count(budget.OUTPUT_BUDGET_HEADING) == 1
    assert "capped at 30000 tokens" in once[0]["content"]
    assert budget.with_output_budget_section(once, 12_345, 40_000) == once
    assert msgs[0]["content"] == "You are the critic."  # input untouched
    assert budget.with_output_budget_section(msgs[1:], 30_000) == msgs[1:]


def test_whole_window_cap_asks_for_a_quarter_free():
    s = budget.output_budget_section(30_000, 32_000)
    assert "keep each reply under 22500 tokens" in s


def test_single_shot_states_the_role_cap_and_sends_num_ctx():
    c = _Caps(_caps(cloud=False, declared_num_ctx=65_536))
    council._single_shot(c, "m", [{"role": "system", "content": "S"},
                                  {"role": "user", "content": "q"}],
                         think=False)
    sent = c.calls[0]
    assert sent["options"]["num_ctx"] == 65_536
    assert "capped at 49152 tokens" in sent["messages"][0]["content"]


def test_announce_off_leaves_the_prompt_alone():
    c = _Caps(_caps(cloud=True, context_length=131_072))
    council._single_shot(c, "m", [{"role": "system", "content": "S"},
                                  {"role": "user", "content": "q"}],
                         think=False,
                         knobs=budget.knobs_from({"announce": False}))
    assert c.calls[0]["messages"][0]["content"] == "S"
    assert "num_ctx" not in c.calls[0]["options"]


# --- knob layering ------------------------------------------------------- #

def test_knobs_merge_most_specific_last():
    state = {"runtime_control": {"budget": {
        "*": {"output_ceiling": 50_000, "reserve_min": 2048},
        "critic": {"output_ceiling": 40_000},
        "critic@glm:cloud": {"output_ceiling": 30_000}}}}
    k = council._budget_knobs(state, "critic", "glm:cloud")
    assert (k.output_ceiling, k.reserve_min) == (30_000, 2048)
    assert council._budget_knobs(state, "critic", "other").output_ceiling == 40_000
    assert council._budget_knobs(state, "planner", "x").output_ceiling == 50_000
    assert council._budget_knobs({}, "planner", "x") is budget.DEFAULT_KNOBS


def test_seed_carries_only_what_was_set(tmp_path, monkeypatch):
    from consultants import config as cc
    from consultants.engine import control
    cfg = cc.ConsultantsConfig()
    rc = control.runtime_control_defaults(cfg, effort="high")
    assert "budget" not in rc  # M12 parity: a default run seeds nothing
    cfg.budget = {"output_ceiling": 64_000}
    cfg.roles["researcher"].ctx_max = 65_536
    cfg.roles["researcher"].ctx_max_explicit = True
    cfg.roles["critic"].model_overrides = {"glm:cloud": {"num_ctx": 8192}}
    rc = control.runtime_control_defaults(cfg, effort="high")
    assert rc["budget"] == {"*": {"output_ceiling": 64_000},
                            "researcher": {"num_ctx": 65_536},
                            "critic@glm:cloud": {"num_ctx": 8192}}


# --- config + CLI -------------------------------------------------------- #

@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_toml_roundtrip_and_bad_values_dropped(home):
    from consultants import config as cc
    p = home / ".claude" / "consultants-config.toml"
    p.parent.mkdir(parents=True)
    p.write_text(
        '[budget]\noutput_ceiling = 64000\nreserve_share = 5.0\nbogus = 1\n'
        '[role.researcher.budget]\nnum_ctx = 65536\n'
        '[role.researcher.models."glm-5.3:cloud"]\noutput_window_share = 0.5\n',
        encoding="utf-8")
    cfg = cc.load_config(home)
    assert cfg.budget == {"output_ceiling": 64000}
    assert cfg.roles["researcher"].budget == {"num_ctx": 65536}
    assert cfg.roles["researcher"].model_overrides == {
        "glm-5.3:cloud": {"output_window_share": 0.5}}
    cc.set_budget({"announce": "off"}, scope="user", cwd=home)
    again = cc.load_config(home)
    assert again.budget == {"output_ceiling": 64000, "announce": False}
    assert again.roles["researcher"].model_overrides == {
        "glm-5.3:cloud": {"output_window_share": 0.5}}


def test_set_budget_cli(home, capsys):
    from consultants import cli
    from consultants import config as cc
    assert cli.main(["config", "set-budget", "--role", "critic", "--model",
                     "@bs2", "--num-ctx", "131072", "--user"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["roles"]["critic"]["models"] == {"@bs2": {"num_ctx": 131072}}
    assert cli.main(["config", "set-budget", "--role", "critic", "--model",
                     "@bs2", "--clear", "num_ctx", "--user"]) == 0
    capsys.readouterr()
    assert cc.load_config(home).roles["critic"].model_overrides == {}
    assert cli.main(["config", "set-budget", "--model", "x", "--num-ctx",
                     "1", "--user"]) == 2
    assert cli.main(["config", "set-budget", "--reserve-share", "3",
                     "--user"]) == 2


# --- hard cap follows the cap ------------------------------------------- #

def test_hard_cap_stretches_with_the_output_budget():
    ref = 32_768
    assert scaled_hard_cap(780.0, {"options": {"num_predict": 32_768}}, ref) == 780.0
    assert scaled_hard_cap(780.0, {"options": {"num_predict": 4096}}, ref) == 780.0
    assert scaled_hard_cap(780.0, {"options": {"num_predict": 98_304}}, ref) \
        == pytest.approx(2340.0)
    assert scaled_hard_cap(780.0, {"options": {"num_predict": 98_304}}, None) == 780.0


def test_hard_cap_only_wrapper_uses_the_stretched_cap():
    import time
    fn = make_hard_cap_only_chat_fn(lambda p: (time.sleep(0.15), {"ok": 1})[1],
                                    hard_cap_s=0.1,
                                    hard_cap_reference_output=1000)
    assert fn({"options": {"num_predict": 4000}}) == {"ok": 1}


# --- PolyKV window mode (plan B2, owner 2026-10-14) ---------------------- #

def test_props_dca_fields_are_recorded_not_used():
    from claude_hooks.provider_caps import caps_fields_from_props
    f = caps_fields_from_props({"opencoti": {
        "kv": {"kv_pool_cells": 2_097_152, "session_ctx_max": 262_144},
        "dca": {"enabled": True, "chunk_size": 8192, "yarn_factor": 4.0}}})
    assert (f["dca_enabled"], f["dca_yarn_factor"]) == (True, 4.0)
    w = budget.resolve_window(_Caps(_caps(backend="opencoti", **f)), "m")
    # The engine's per-session ceiling, not the pool, not trained × factor.
    assert (w.tokens, w.source, w.send_num_ctx) == (262_144, "session_ctx_max", None)


def test_dynamic_ignores_the_per_request_grant():
    """No ``num_ctx`` sent, so X-Context-Window is one request's
    settlement (83 on a 21-token call), never the session window."""
    from claude_hooks.get_advice import chat_client as cc
    c = cc.OpenAICompatChatClient("http://oc:8240")
    assert c.granted_window("m") is None


def test_endpoint_window_mode(home, capsys):
    from consultants import cli
    from consultants import config as cc
    assert cli.main(["config", "set-endpoint", "bs2", "--url", "http://h:8240",
                     "--backend", "opencoti", "--user"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["endpoints"]["bs2"]["window"] == "dynamic"
    assert cli.main(["config", "set-endpoint", "bs2", "--window", "assured",
                     "--user"]) == 2
    assert "Milestone F" in capsys.readouterr().err
    assert cc.load_config(home).endpoints["bs2"].window == "dynamic"


def test_grant_is_kept_only_from_a_request_that_stated_a_window():
    from claude_hooks.get_advice import chat_client as cc
    c = cc.ChatClient("http://h")
    c._note_grant({"model": "m", "options": {}}, {"granted_window": 83})
    assert c.granted_window("m") is None
    c._note_grant({"model": "m", "options": {"num_ctx": 65536}},
                  {"granted_window": 49152})
    c._note_grant({"model": "m", "options": {"num_ctx": 65536}},
                  {"granted_window": 65536})
    assert c.granted_window("m") == 49152  # a booking never shrinks or moves
