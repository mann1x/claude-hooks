"""M4 — dynamic critic dial: revive the dead ``critic_strictness`` +
add the injectable ``adversarial_focus`` attack brief.

The dial lives in ``state['runtime_control']`` and is threaded into
BOTH the critic and meta-critic prompts so a live ``POST /control``
mutation (or the boot-time seed from ``adversary_strictness``) actually
changes the next critic call's behavior. The default (``normal``
strictness + empty focus) appends NOTHING, so the prompt is
byte-identical to v1 — the cohort-2 parity contract.

Cohorts:
- ``_append_critic_dial`` / directive table (pure).
- ``build_critic_messages`` + ``build_meta_critic_messages``: directive
  + focus appear when non-default; default → byte-identical.
- ``_read_critic_dial`` reads runtime_control with parity fallback.
- ``critic_node`` + ``meta_critic_node`` thread the live dial into the
  next prompt (stub chat).
- ``runtime_control_defaults`` seeds the dial from ``adversary_strictness``.
- ``build_runtime_control_delta`` accepts ``adversarial`` + the focus
  brief and rejects bad input.

All langgraph-free (council node fns are pure-python) → runs in BOTH
envs.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _parity_helpers import StableChat  # noqa: E402

from consultants import config as cc  # noqa: E402
from consultants.engine import council  # noqa: E402
from consultants.engine import control  # noqa: E402
from consultants.server import control as scontrol  # noqa: E402


# ----------------------- pure directive -------------------------- #

class TestAppendCriticDial:
    def test_normal_appends_nothing(self):
        parts = ["BASE"]
        council._append_critic_dial(
            parts, strictness="normal", adversarial_focus="")
        assert parts == ["BASE"]

    def test_unknown_strictness_appends_nothing(self):
        parts = ["BASE"]
        council._append_critic_dial(
            parts, strictness="bogus", adversarial_focus="")
        assert parts == ["BASE"]

    @pytest.mark.parametrize("strictness,needle", [
        ("lax", "STRICTNESS: lax"),
        ("strict", "STRICTNESS: strict"),
        ("adversarial", "STRICTNESS: adversarial"),
    ])
    def test_directive_appended(self, strictness, needle):
        parts = ["BASE"]
        council._append_critic_dial(
            parts, strictness=strictness, adversarial_focus="")
        assert any(needle in p for p in parts)

    def test_focus_block_appended(self):
        parts = ["BASE"]
        council._append_critic_dial(
            parts, strictness="normal",
            adversarial_focus="the 'O(1)' claim is suspect")
        joined = "\n".join(parts)
        assert "ADVERSARIAL FOCUS" in joined
        assert "O(1)" in joined

    def test_blank_focus_appends_nothing(self):
        parts = ["BASE"]
        council._append_critic_dial(
            parts, strictness="normal", adversarial_focus="   \n ")
        assert parts == ["BASE"]


# ----------------------- builders -------------------------------- #

def _critic_user(msgs):
    return msgs[-1]["content"]


class TestBuildCriticMessages:
    def test_default_is_byte_identical(self):
        a = council.build_critic_messages("q?", "1. plan", ["r1"])
        b = council.build_critic_messages(
            "q?", "1. plan", ["r1"],
            strictness="normal", adversarial_focus="")
        assert a == b

    def test_strict_threads_directive(self):
        msgs = council.build_critic_messages(
            "q?", "1. plan", ["r1"], strictness="strict")
        assert "STRICTNESS: strict" in _critic_user(msgs)

    def test_adversarial_with_focus(self):
        msgs = council.build_critic_messages(
            "q?", "1. plan", ["r1"], strictness="adversarial",
            adversarial_focus="prove the cache invariant breaks")
        body = _critic_user(msgs)
        assert "STRICTNESS: adversarial" in body
        assert "ADVERSARIAL FOCUS" in body
        assert "cache invariant" in body

    def test_system_prompt_unchanged(self):
        # The dial rides the user message; CRITIC_SYSTEM is constant.
        msgs = council.build_critic_messages(
            "q?", "1. plan", ["r1"], strictness="adversarial")
        assert msgs[0]["content"] == council.CRITIC_SYSTEM


class TestBuildMetaCriticMessages:
    def test_default_is_byte_identical(self):
        a = council.build_meta_critic_messages("q?", "p", ["r1"], ["v1"])
        b = council.build_meta_critic_messages(
            "q?", "p", ["r1"], ["v1"],
            strictness="normal", adversarial_focus="")
        assert a == b

    def test_strict_threads_directive(self):
        msgs = council.build_meta_critic_messages(
            "q?", "p", ["r1"], ["v1"], strictness="strict")
        body = msgs[-1]["content"]
        assert "STRICTNESS: strict" in body
        # the closing synth instruction still comes last
        assert body.rstrip().endswith("critique paragraph.")

    def test_focus_threads(self):
        msgs = council.build_meta_critic_messages(
            "q?", "p", ["r1"], [], strictness="adversarial",
            adversarial_focus="attack the concurrency claim")
        body = msgs[-1]["content"]
        assert "ADVERSARIAL FOCUS" in body
        assert "concurrency claim" in body


# ----------------------- dial reader ----------------------------- #

class TestReadCriticDial:
    def test_absent_runtime_control_is_default(self):
        assert council._read_critic_dial({}) == ("normal", "")

    def test_reads_live_values(self):
        st = {"runtime_control": {
            "critic_strictness": "adversarial",
            "adversarial_focus": "break it",
        }}
        assert council._read_critic_dial(st) == ("adversarial", "break it")

    def test_partial_runtime_control(self):
        st = {"runtime_control": {"critic_strictness": "strict"}}
        assert council._read_critic_dial(st) == ("strict", "")


# ----------------------- node threading -------------------------- #

def _critic_state(**over):
    st = {
        "question": "why is the sky blue?",
        "plan": "1. measure",
        "research": ["finding one"],
        "research_rounds_used": 1,
    }
    st.update(over)
    return st


class TestCriticNodeThreadsDial:
    def test_default_no_directive(self):
        stub = StableChat("DECISION: ready\nlooks fine")
        council.critic_node(_critic_state(), chat_client=stub, model="m")
        body = stub.calls[0]["messages"][-1]["content"]
        assert "STRICTNESS:" not in body
        assert "ADVERSARIAL FOCUS" not in body

    def test_live_dial_threads(self):
        stub = StableChat("DECISION: ready\nlooks fine")
        st = _critic_state(runtime_control={
            "critic_strictness": "adversarial",
            "adversarial_focus": "the perf claim is unverified",
        })
        council.critic_node(st, chat_client=stub, model="m")
        body = stub.calls[0]["messages"][-1]["content"]
        assert "STRICTNESS: adversarial" in body
        assert "perf claim" in body


class TestMetaCriticNodeThreadsDial:
    def test_live_dial_threads(self):
        stub = StableChat(
            "DECISION: ready\nconsolidated critique paragraph.")
        st = _critic_state(turns=[], runtime_control={
            "critic_strictness": "strict",
        })
        council.meta_critic_node(st, chat_client=stub, model="m")
        body = stub.calls[0]["messages"][-1]["content"]
        assert "STRICTNESS: strict" in body


# ----------------------- boot-time seed -------------------------- #

class TestSeedFromConfig:
    @pytest.mark.parametrize("adv,expected", [
        ("soft", "lax"),
        ("normal", "normal"),
        ("strict", "strict"),
    ])
    def test_seed_maps_adversary_strictness(self, adv, expected):
        cfg = cc.ConsultantsConfig()
        cfg.adversary_strictness = adv
        rc = control.runtime_control_defaults(cfg, effort="high")
        assert rc["critic_strictness"] == expected

    def test_default_seed_is_normal(self):
        cfg = cc.ConsultantsConfig()
        rc = control.runtime_control_defaults(cfg, effort="medium")
        assert rc["critic_strictness"] == "normal"


# ----------------------- server control delta -------------------- #

class TestControlDelta:
    def test_adversarial_strictness_accepted(self):
        out = scontrol.build_runtime_control_delta(
            {"critic_strictness": "adversarial"})
        assert out["runtime_control"]["critic_strictness"] == "adversarial"

    def test_bad_strictness_rejected(self):
        with pytest.raises(scontrol.ControlInputError):
            scontrol.build_runtime_control_delta(
                {"critic_strictness": "savage"})

    def test_focus_accepted(self):
        out = scontrol.build_runtime_control_delta(
            {"adversarial_focus": "attack the O(1) claim"})
        assert out["runtime_control"]["adversarial_focus"] \
            == "attack the O(1) claim"

    def test_empty_focus_clears(self):
        out = scontrol.build_runtime_control_delta(
            {"adversarial_focus": ""})
        assert out["runtime_control"]["adversarial_focus"] == ""

    def test_non_string_focus_rejected(self):
        with pytest.raises(scontrol.ControlInputError):
            scontrol.build_runtime_control_delta(
                {"adversarial_focus": 123})

    def test_overlong_focus_rejected(self):
        big = "x" * (scontrol.ADVERSARIAL_FOCUS_MAX_CHARS + 1)
        with pytest.raises(scontrol.ControlInputError):
            scontrol.build_runtime_control_delta(
                {"adversarial_focus": big})

    def test_adversarial_in_valid_values(self):
        assert "adversarial" in scontrol.VALID_STRICTNESS_VALUES
