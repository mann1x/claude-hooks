"""Plan Milestone B4: sampling diversity for every role."""
from __future__ import annotations

import json
import random
import sqlite3

import pytest

from claude_hooks.provider_caps import ProviderCaps
from consultants.engine import council
from consultants.engine import sampling_diversity as sd


class _Caps:
    def __init__(self, temperature=None):
        self._c = ProviderCaps("http://h", "m", temperature=temperature)

    def caps(self, model):
        return self._c


@pytest.fixture(autouse=True)
def _no_templates(monkeypatch):
    monkeypatch.setenv("CLAUDE_HOOKS_MODEL_SAMPLING", json.dumps({"*": None}))
    import claude_hooks.model_sampling as ms
    monkeypatch.setattr(ms, "templates", lambda cfg=None, shipped=None: {})


def test_same_model_instances_never_share_a_seed():
    rng = random.Random(1)
    # A tiny seed space forces collisions; uniqueness must still hold.
    seeds = []
    real = sd.SEED_SPACE
    sd.SEED_SPACE = 64
    try:
        for _ in range(64):
            seeds.append(sd.allocate("run-a", "researcher", "m", rng=rng)["seed"])
    finally:
        sd.SEED_SPACE = real
    assert len(set(seeds)) == 64
    # Another model, or another run, is a separate space.
    a = sd.allocate("run-b", "critic", "x", rng=random.Random(0))["seed"]
    b = sd.allocate("run-c", "critic", "x", rng=random.Random(0))["seed"]
    assert a == b


def test_integer_seed_gives_seed_plus_i_per_same_model_instance():
    layers = {"*": {"seed": 42}}
    got = [sd.allocate("run-int", "researcher", "m", layers=layers)["seed"]
           for _ in range(3)]
    other = sd.allocate("run-int", "critic", "n", layers=layers)["seed"]
    assert got == [42, 43, 44] and other == 42


def test_off_sends_no_seed_and_no_run_sends_nothing():
    assert "seed" not in sd.allocate("run-off", "planner", "m",
                                     layers={"*": {"seed": "off"}})
    assert sd.allocate(None, "planner", "m") == {}


def test_parity_cohort_seed_off_range_zero_adds_nothing():
    """M12 cohort 2: with seed off and no jitter, the request is what it
    was before B4 even where the backend declares a temperature."""
    layers = {"*": {"seed": "off", "temperature_range_pct": 0}}
    assert sd.allocate("run-p", "critic", "m", layers=layers,
                       chat_client=_Caps(0.6)) == {}


def test_jitter_stays_inside_the_range_and_the_clamp():
    rng = random.Random(7)
    for _ in range(500):
        t = sd.jitter(0.7, 2, rng)
        assert 0.686 <= t <= 0.714 and round(t, 3) == t
        t = sd.jitter(0.7, 50, rng)  # clamped to 10 %
        assert 0.63 <= t <= 0.77
    assert sd.jitter(0.0, 10, rng) == 0.0


def test_temperature_base_order(monkeypatch):
    import claude_hooks.model_sampling as ms
    assert sd.base_temperature("m", 0.3, _Caps(0.9)) == 0.3
    monkeypatch.setattr(ms, "sampling_for", lambda m, cfg=None: {"temperature": 0.7})
    assert sd.base_temperature("m", None, _Caps(0.9)) == 0.7
    monkeypatch.setattr(ms, "sampling_for", lambda m, cfg=None: {})
    assert sd.base_temperature("m", None, _Caps(0.9)) == 0.9
    assert sd.base_temperature("m", None, _Caps(None)) is None
    out = sd.allocate("run-t", "critic", "m", chat_client=_Caps(None))
    assert "temperature" not in out and "seed" in out  # unknown base: unset


def test_layers_most_specific_last():
    layers = {"*": {"temperature_range_pct": 5},
              "critic": {"seed": "off"},
              "critic@m": {"temperature": 0.2, "temperature_range_pct": 0}}
    assert sd.settings_for(layers, "critic", "m") == {
        "seed": "off", "temperature": 0.2, "temperature_range_pct": 0}
    assert sd.settings_for(layers, "planner", "m") == {
        "seed": "random", "temperature_range_pct": 5}


# --- engine wiring ------------------------------------------------------ #

class _Chat(_Caps):
    def __init__(self):
        super().__init__(0.6)
        self.calls = []

    def chat(self, payload):
        self.calls.append(payload)
        return {"choices": [{"message": {"content": "ok"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1}}


def test_every_role_instance_sends_its_own_sampler():
    from consultants import config as cc
    from consultants.engine import control
    state = {"runtime_control": control.runtime_control_defaults(
        cc.ConsultantsConfig(), effort="high")}
    chat = _Chat()
    for role in ("planner", "critic", "synthesizer"):
        s = council._instance_sampling(state, role, "m", chat)
        council._single_shot(chat, "m", [{"role": "user", "content": "q"}],
                             think=False, sampling=s, role=role)
    seeds = [c["options"]["seed"] for c in chat.calls]
    temps = [c["options"]["temperature"] for c in chat.calls]
    assert len(set(seeds)) == 3
    assert all(0.588 <= t <= 0.612 for t in temps)


def test_an_explicit_option_beats_the_instance_sampler():
    from consultants.engine import budget
    plan = budget.Budget(None, 1, 100, False)
    opts = council._budget_options({"temperature": 0.1}, plan,
                                   {"temperature": 0.7, "seed": 5})
    assert opts["temperature"] == 0.1 and opts["seed"] == 5


def test_seed_record_carries_the_run_salt_and_only_set_layers():
    from consultants import config as cc
    from consultants.engine import control
    cfg = cc.ConsultantsConfig()
    a = control.runtime_control_defaults(cfg, effort="high")
    b = control.runtime_control_defaults(cfg, effort="high")
    assert a["sampling_run"] != b["sampling_run"] and "sampling" not in a
    cfg.sampling = {"seed": 7}
    cfg.roles["critic"].model_overrides = {"m": {"temperature": 0.2}}
    rc = control.runtime_control_defaults(cfg, effort="high")
    assert rc["sampling"] == {"*": {"seed": 7}, "critic@m": {"temperature": 0.2}}


def test_client_records_the_realized_sampler(monkeypatch):
    from unittest.mock import patch
    from claude_hooks.get_advice import chat_client as cc

    class _R:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"message": {"content": "ok"}, "done": True,
                               "done_reason": "stop"}).encode()
    monkeypatch.setenv("CLAUDE_HOOKS_OLLAMA_SLOTS_DISABLE", "1")
    c = cc.ChatClient("http://h")
    c._probed_think["m"] = True
    with patch("urllib.request.urlopen", return_value=_R()):
        out = c.chat({"model": "m", "messages": [],
                      "options": {"seed": 11, "temperature": 0.701}})
    assert out["timings"]["sampler"] == {"seed": 11, "temperature": 0.701}


# --- config / CLI / show ------------------------------------------------ #

@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_sampling_config_and_cli(home, capsys):
    from consultants import cli
    from consultants import config as cc
    assert cli.main(["config", "set-sampling", "--seed", "123",
                     "--temperature-range-pct", "4", "--user"]) == 0
    assert cli.main(["config", "set-sampling", "--role", "critic", "--model",
                     "glm:cloud", "--temperature", "0.3", "--user"]) == 0
    capsys.readouterr()
    cfg = cc.load_config(home)
    assert cfg.sampling == {"seed": 123, "temperature_range_pct": 4.0}
    assert cfg.roles["critic"].model_overrides == {"glm:cloud": {"temperature": 0.3}}
    assert cli.main(["config", "set-sampling", "--seed", "-3", "--user"]) == 2
    assert cli.main(["config", "set-sampling", "--temperature-range-pct",
                     "25", "--user"]) == 0
    assert cc.load_config(home).sampling["temperature_range_pct"] == 10.0


def test_show_lists_realized_samplers(tmp_path):
    from consultants.cli import _session_samplers
    db = tmp_path / "transcript.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE events (event_id INTEGER PRIMARY KEY, kind TEXT, "
                "role TEXT, round INTEGER, lane_idx INTEGER, model TEXT, "
                "request_json TEXT)")
    for lane, seed in ((0, 1), (0, 1), (1, 2)):
        con.execute("INSERT INTO events (kind, role, round, lane_idx, model, "
                    "request_json) VALUES ('llm_call','researcher',1,?,'m',?)",
                    (lane, json.dumps({"options": {"seed": seed,
                                                   "temperature": 0.7}})))
    con.commit()
    con.close()
    assert _session_samplers(db) == [
        {"role": "researcher", "round": 1, "lane_idx": 0, "model": "m",
         "seed": 1, "temperature": 0.7},
        {"role": "researcher", "round": 1, "lane_idx": 1, "model": "m",
         "seed": 2, "temperature": 0.7}]
