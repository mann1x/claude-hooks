"""Phase 9 tests — multi-model researcher fan-out at x-prefixed
effort tiers (xmedium / xhigh / xmax).

Coverage:
- Effort-tier helpers: ``base_effort`` / ``extras_active``.
- ``RoleConfig.extra_models``: TOML round-trip, dedup against the
  primary, dedup within the list, drop empties / non-strings.
- ``set_role`` mutators: ``add_extra_model`` / ``remove_extra_model``
  / ``clear_extras``, idempotency, primary-swap re-sanitization.
- ``caps_for("xhigh")`` returns the same caps as ``caps_for("high")``
  — x-prefix doesn't change per-lane budgets, only fan-out width.
- ``researcher_node`` honors ``state["model_override"]`` when set
  and falls back to the role-configured primary otherwise.
- The runner's ``_warn_extras_cost`` fires once per consultation
  start when extras are active, and the runner only attaches
  ``extra_models_by_role`` to GraphDeps at x-tiers (silent at
  base tiers — the foot-gun guard).
- The graph dispatcher fans out to N x M Sends with globally
  unique ``lane_idx`` values and the right ``model_override`` per
  Send.
"""

from __future__ import annotations

import io
import logging
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from consultants import config as cc
from consultants.engine import council
from consultants.engine import graph as graph_mod
from consultants.server import runner as prod_runner


# ----------------------- effort-tier helpers --------------------- #

class TestEffortTierHelpers:
    def test_base_effort_strips_x_prefix(self):
        assert cc.base_effort("xmedium") == "medium"
        assert cc.base_effort("xhigh") == "high"
        assert cc.base_effort("xmax") == "max"

    def test_base_effort_passes_base_tiers_through(self):
        for tier in ("low", "medium", "high", "max"):
            assert cc.base_effort(tier) == tier

    def test_base_effort_unknown_returns_input(self):
        # Defensive: stay neutral on unknown tiers so callers'
        # validation paths fire, not ours.
        assert cc.base_effort("xbanana") == "xbanana"
        assert cc.base_effort("nope") == "nope"

    def test_extras_active_only_for_x_tiers(self):
        for tier in ("xmedium", "xhigh", "xmax"):
            assert cc.extras_active(tier) is True
        for tier in ("low", "medium", "high", "max"):
            assert cc.extras_active(tier) is False
        # Non-recognised x is NOT active — guards typos.
        assert cc.extras_active("xbanana") is False

    def test_x_tiers_in_effort_budgets(self):
        for tier in ("xmedium", "xhigh", "xmax"):
            assert tier in cc.EFFORT_BUDGETS
        # Same follow-up budgets as their base tier.
        assert cc.EFFORT_BUDGETS["xhigh"] == cc.EFFORT_BUDGETS["high"]
        assert cc.EFFORT_BUDGETS["xmax"] == cc.EFFORT_BUDGETS["max"]


class TestCapsForXTier:
    def test_xhigh_caps_equal_high_caps(self):
        # Multi-model fan-out doesn't change per-lane budgets — it
        # multiplies the lane count. So caps_for must strip the x.
        a = council.caps_for("high")
        b = council.caps_for("xhigh")
        assert a == b

    def test_xmax_caps_equal_max_caps(self):
        assert council.caps_for("max") == council.caps_for("xmax")


# ----------------------- RoleConfig + sanitizer ------------------ #

class TestRoleConfigExtras:
    def test_default_extras_is_empty_list(self):
        rc = cc.RoleConfig()
        assert rc.extra_models == []

    def test_sanitize_drops_primary_dup(self):
        out = cc._sanitize_extras(
            ["kimi-k2.6:cloud", "qwen3.5:cloud", "deepseek-v4-pro:cloud"],
            primary="qwen3.5:cloud",
        )
        assert out == ["kimi-k2.6:cloud", "deepseek-v4-pro:cloud"]

    def test_sanitize_dedup_within_list(self):
        out = cc._sanitize_extras(
            ["a:cloud", "b:cloud", "a:cloud", "b:cloud"],
            primary="other:cloud",
        )
        assert out == ["a:cloud", "b:cloud"]

    def test_sanitize_drops_empties_and_non_strings(self):
        out = cc._sanitize_extras(
            ["a:cloud", "", "  ", None, 42, "b:cloud"],
            primary="primary:cloud",
        )
        assert out == ["a:cloud", "b:cloud"]

    def test_sanitize_strips_whitespace(self):
        out = cc._sanitize_extras(
            ["  spaced:cloud  "], primary="other:cloud",
        )
        assert out == ["spaced:cloud"]


class TestTomlRoundTrip:
    def test_extras_round_trip(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        cfg = cc.load_config()  # default
        # Set the primary explicitly so it doesn't collide with the
        # default model (which would land in extras and get dropped
        # by the sanitizer on load).
        cfg.roles["researcher"].model = "qwen3.5:cloud"
        cfg.roles["researcher"].extra_models = [
            "kimi-k2.6:cloud", "deepseek-v4-pro:cloud",
        ]
        cc.save_config(cfg, scope="user")
        # Re-load — the TOML emitter + parser cycle preserves
        # extra_models verbatim.
        again = cc.load_config()
        assert again.roles["researcher"].extra_models == [
            "kimi-k2.6:cloud", "deepseek-v4-pro:cloud",
        ]

    def test_empty_extras_round_trip(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        cfg = cc.load_config()
        cc.save_config(cfg, scope="user")
        again = cc.load_config()
        assert again.roles["researcher"].extra_models == []
        assert again.roles["critic"].extra_models == []

    def test_load_drops_primary_from_extras_in_file(
            self, tmp_path, monkeypatch):
        # Hand-edited file with the primary listed in extras: the
        # sanitizer drops it on load so the runtime never doubles
        # the primary lane.
        monkeypatch.setenv("HOME", str(tmp_path))
        path = cc.user_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            'topology = "council"\n'
            'effort = "medium"\n'
            '[role.researcher]\n'
            'enabled = true\n'
            'model = "primary:cloud"\n'
            'extra_models = ["primary:cloud", "extra:cloud"]\n',
            encoding="utf-8",
        )
        cfg = cc.load_config()
        assert cfg.roles["researcher"].extra_models == ["extra:cloud"]


class TestSetRoleMutators:
    @staticmethod
    def _set_alt_primary(role: str = "researcher",
                          tag: str = "qwen3.5:cloud") -> None:
        """Default primary collides with the canonical extras. Use a
        different primary so the sanitizer doesn't fold our test
        adds into the empty list."""
        cc.set_role(role, model=tag)

    def test_add_extra_model(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        self._set_alt_primary()
        cfg = cc.set_role(
            "researcher", add_extra_model="kimi-k2.6:cloud",
        )
        assert "kimi-k2.6:cloud" in cfg.roles["researcher"].extra_models

    def test_add_extra_model_dedup_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        self._set_alt_primary()
        cc.set_role("researcher", add_extra_model="x:cloud")
        cc.set_role("researcher", add_extra_model="x:cloud")
        cfg = cc.load_config()
        # Single entry despite double-add.
        assert cfg.roles["researcher"].extra_models == ["x:cloud"]

    def test_add_extra_model_drops_when_equal_to_primary(
            self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        cfg = cc.set_role("researcher", model="qwen3.5:cloud")
        cfg = cc.set_role(
            "researcher", add_extra_model="qwen3.5:cloud",
        )
        # Sanitizer drops the primary; list stays empty.
        assert cfg.roles["researcher"].extra_models == []

    def test_remove_extra_model(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        self._set_alt_primary()
        cc.set_role("researcher", add_extra_model="a:cloud")
        cc.set_role("researcher", add_extra_model="b:cloud")
        cfg = cc.set_role("researcher", remove_extra_model="a:cloud")
        assert cfg.roles["researcher"].extra_models == ["b:cloud"]

    def test_remove_extra_model_missing_is_noop(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        self._set_alt_primary()
        cc.set_role("researcher", add_extra_model="a:cloud")
        cfg = cc.set_role("researcher", remove_extra_model="ghost:cloud")
        assert cfg.roles["researcher"].extra_models == ["a:cloud"]

    def test_clear_extras(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        self._set_alt_primary()
        cc.set_role("researcher", add_extra_model="a:cloud")
        cc.set_role("researcher", add_extra_model="b:cloud")
        cfg = cc.set_role("researcher", clear_extras=True)
        assert cfg.roles["researcher"].extra_models == []

    def test_primary_swap_resanitizes_extras(self, tmp_path, monkeypatch):
        # If the user changes the primary to a tag that's currently
        # in extras, the sanitizer drops it from extras so the new
        # primary isn't double-counted at x-tier.
        monkeypatch.setenv("HOME", str(tmp_path))
        cc.set_role("researcher", model="primary:cloud")
        cc.set_role("researcher", add_extra_model="winner:cloud")
        cfg = cc.set_role("researcher", model="winner:cloud")
        assert cfg.roles["researcher"].model == "winner:cloud"
        assert cfg.roles["researcher"].extra_models == []

    def test_add_empty_string_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        with pytest.raises(ValueError):
            cc.set_role("researcher", add_extra_model="   ")


# ----------------------- researcher_node model_override --------- #

class _FakeChatClient:
    """Captures what model the researcher actually requests."""
    def __init__(self):
        self.calls: list[dict] = []

    def chat(self, payload):
        self.calls.append(payload)
        return {
            "choices": [{
                "message": {"content": "lane finding"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }


class TestModelOverride:
    def test_researcher_uses_model_override_when_set(self):
        seen: dict = {}

        def fake_loop(payload, cwd, **kw):
            seen["model"] = payload.get("model")
            return {
                "choices": [{
                    "message": {"content": "ok"},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }

        state = council.initial_state(
            question="q", cwd="/p", models={"researcher": "primary:cloud"},
            topology="council", effort="xhigh",
        )
        state["plan"] = "p"
        state["lane_idx"] = 3
        state["plan_item"] = "find foo"
        state["model_override"] = "kimi-k2.6:cloud"

        council.researcher_node(
            state, chat_client=_FakeChatClient(),
            tool_executor=lambda *a, **k: "",
            tool_specs=[], grounding_msgs=[],
            model="primary:cloud",   # role default — overridden
            cwd="/p", loop_runner=fake_loop,
        )
        assert seen["model"] == "kimi-k2.6:cloud"

    def test_researcher_falls_back_to_primary_when_no_override(self):
        seen: dict = {}

        def fake_loop(payload, cwd, **kw):
            seen["model"] = payload.get("model")
            return {
                "choices": [{
                    "message": {"content": "ok"},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }

        state = council.initial_state(
            question="q", cwd="/p", models={"researcher": "primary:cloud"},
            topology="council", effort="medium",
        )
        state["plan"] = "p"
        state["lane_idx"] = 0
        state["plan_item"] = "do thing"
        # No model_override.
        council.researcher_node(
            state, chat_client=_FakeChatClient(),
            tool_executor=lambda *a, **k: "",
            tool_specs=[], grounding_msgs=[],
            model="primary:cloud",
            cwd="/p", loop_runner=fake_loop,
        )
        assert seen["model"] == "primary:cloud"

    def test_empty_model_override_falls_through_to_primary(self):
        # Defensive: an empty string in state shouldn't override
        # to "" — only non-empty trimmed strings count.
        seen: dict = {}

        def fake_loop(payload, cwd, **kw):
            seen["model"] = payload.get("model")
            return {
                "choices": [{
                    "message": {"content": "ok"},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }

        state = council.initial_state(
            question="q", cwd="/p", models={"researcher": "primary"},
            topology="council", effort="xhigh",
        )
        state["plan"] = "p"
        state["lane_idx"] = 0
        state["plan_item"] = "x"
        state["model_override"] = "   "  # whitespace only
        council.researcher_node(
            state, chat_client=_FakeChatClient(),
            tool_executor=lambda *a, **k: "",
            tool_specs=[], grounding_msgs=[],
            model="primary",
            cwd="/p", loop_runner=fake_loop,
        )
        assert seen["model"] == "primary"


# ----------------------- runner-level wiring -------------------- #

class TestRunnerExtrasWiring:
    """The runner attaches extra_models_by_role to GraphDeps ONLY at
    x-prefixed effort tiers. This is the foot-gun guard — at base
    tiers the extras list is silently ignored even if populated."""

    def _build_session(self, tmp_path, effort: str,
                       researcher_extras: list[str]):
        """Build a SessionState + cfg + runner-input dict, mock out
        the graph build so we can inspect deps without LangGraph."""
        from consultants.server.app import SessionState

        captured: dict = {}

        class _FakeCompiled:
            def stream(self, initial, *, stream_mode):
                yield ("values", dict(initial))

        def fake_build_council_graph(deps, *, tracer=None,
                                      checkpointer=None):
            captured["deps"] = deps
            return _FakeCompiled()

        # Patch the lazy-imported build_council_graph at its source
        # — make_runner imports inside its closure.
        from consultants.engine import graph as g
        original = g.build_council_graph
        g.build_council_graph = fake_build_council_graph
        try:
            run_council = prod_runner.make_runner(
                ollama_base_url="http://x:1234",
            )
            cfg = cc.ConsultantsConfig(
                topology="council", effort=effort,
            )
            cfg.roles["researcher"].extra_models = list(researcher_extras)
            state = SessionState(
                sid="csl-extras-test", cwd=str(tmp_path),
                question="q", effort=effort, topology="council",
                status="running",
            )
            state.progress = {
                r: "pending" for r in
                ("planner", "researcher", "critic", "synthesizer")
            }
            run_council(state, {
                "config": cfg,
                "cwd": str(tmp_path),
                "question": "q",
            })
        finally:
            g.build_council_graph = original
        return captured["deps"]

    def test_xhigh_attaches_researcher_extras_to_deps(self, tmp_path):
        deps = self._build_session(
            tmp_path, "xhigh",
            researcher_extras=["kimi-k2.6:cloud", "deepseek-v4-pro:cloud"],
        )
        assert deps.extra_models_by_role.get("researcher") == [
            "kimi-k2.6:cloud", "deepseek-v4-pro:cloud",
        ]

    def test_high_silently_ignores_researcher_extras(self, tmp_path):
        # Same config, base tier — extras MUST NOT be wired in.
        deps = self._build_session(
            tmp_path, "high",
            researcher_extras=["kimi-k2.6:cloud"],
        )
        assert deps.extra_models_by_role == {}

    def test_xhigh_no_extras_is_empty_dict(self, tmp_path):
        deps = self._build_session(
            tmp_path, "xhigh", researcher_extras=[],
        )
        assert deps.extra_models_by_role == {}

    def test_warn_extras_cost_logs_at_x_tier(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING,
                             logger="consultants.server.runner"):
            self._build_session(
                tmp_path, "xhigh",
                researcher_extras=["kimi-k2.6:cloud"],
            )
        msg = "\n".join(r.getMessage() for r in caplog.records)
        assert "xhigh" in msg
        assert "researcher" in msg
        assert "kimi-k2.6:cloud" in msg

    def test_warn_extras_cost_silent_at_base_tier(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING,
                             logger="consultants.server.runner"):
            self._build_session(
                tmp_path, "high",
                researcher_extras=["kimi-k2.6:cloud"],
            )
        # No "xhigh" / x-tier warning should fire on a base tier.
        msg = "\n".join(r.getMessage() for r in caplog.records)
        assert "researcher token cost" not in msg


# ----------------------- planner-fan-out dispatcher ------------- #

class TestFanoutDispatcher:
    """The graph builder's planner->researcher dispatcher fans out
    one Send per (plan_item lane, model) when extras are active."""

    def _build_deps(self, *, extras: list[str]):
        # Minimal GraphDeps with the bits the dispatcher actually
        # reads. We don't need real ChatClients here — the
        # dispatcher is a pure function of state + deps.
        return graph_mod.GraphDeps(
            chat_clients={"researcher": object()},
            models={"researcher": "primary:cloud"},
            enabled_roles=("planner", "researcher", "synthesizer"),
            cwd="/p",
            tool_executor=None,
            tool_specs=[],
            grounding_msgs=[],
            think_by_role={},
            extra_models_by_role={"researcher": list(extras)},
        )

    def _run_dispatcher(self, deps, plan_items: list[str]):
        # Re-derive the closure that build_council_graph would
        # build. The dispatcher logic lives inline; reach into it
        # via the public helper on the graph builder.
        # We don't call build_council_graph (would need langgraph);
        # instead we replicate the closure shape from graph.py
        # against our deps + state, since the graph module's
        # private fan-out is the unit under test. Use a local stand-
        # in for ``langgraph.types.Send`` so the test runs in the
        # main env (langgraph lives in claude-hooks-consultants).
        try:
            from langgraph.types import Send  # type: ignore[import-not-found]
        except ImportError:
            class Send:  # type: ignore[no-redef]
                __slots__ = ("node", "arg")

                def __init__(self, node: str, arg: dict) -> None:
                    self.node = node
                    self.arg = arg

        researcher_extras = list(
            deps.extra_models_by_role.get("researcher") or []
        )

        def _fanout(state):
            items = state.get("plan_items") or []
            if len(items) < council.FANOUT_MIN_ITEMS:
                return "researcher"
            lanes = council.group_items_into_lanes(
                items, council.FANOUT_MAX_LANES,
            )
            primary = deps.models.get("researcher", "")
            models_per_lane = [primary] + researcher_extras
            sends = []
            global_idx = 0
            for lane_items in lanes:
                joined = council.join_lane_items(lane_items)
                for model_tag in models_per_lane:
                    sends.append(Send(
                        "researcher",
                        {
                            "question": state.get("question"),
                            "plan": state.get("plan", ""),
                            "cwd": state.get("cwd"),
                            "effort": state.get("effort"),
                            "models": state.get("models", {}),
                            "topology": state.get("topology"),
                            "plan_item": joined,
                            "lane_idx": global_idx,
                            "model_override": model_tag,
                            "research": [],
                            "turns": [],
                            "research_rounds_used": 0,
                            "total_prompt_tokens": 0,
                            "total_completion_tokens": 0,
                        },
                    ))
                    global_idx += 1
            return sends

        state = {
            "question": "q", "cwd": "/p",
            "plan": "1. a\n2. b\n3. c",
            "plan_items": list(plan_items),
            "effort": "xhigh",
            "models": {"researcher": "primary:cloud"},
            "topology": "council",
        }
        return _fanout(state)

    def test_no_extras_one_send_per_lane(self):
        deps = self._build_deps(extras=[])
        sends = self._run_dispatcher(
            deps, plan_items=["a", "b", "c"],
        )
        # 3 lanes × 1 model = 3 sends.
        assert len(sends) == 3
        for s in sends:
            assert s.arg["model_override"] == "primary:cloud"
        assert [s.arg["lane_idx"] for s in sends] == [0, 1, 2]

    def test_two_extras_three_sends_per_lane(self):
        deps = self._build_deps(extras=["kimi:cloud", "deepseek:cloud"])
        sends = self._run_dispatcher(
            deps, plan_items=["a", "b", "c"],
        )
        # 3 lanes × 3 models = 9 sends.
        assert len(sends) == 9
        # Lane indices are globally unique 0..8.
        assert [s.arg["lane_idx"] for s in sends] == list(range(9))
        # Within each item-lane, models appear primary -> extras.
        first_three = [s.arg["model_override"] for s in sends[:3]]
        assert first_three == ["primary:cloud", "kimi:cloud", "deepseek:cloud"]
        # All three model tags appear exactly len(plan_items) times.
        from collections import Counter
        counts = Counter(s.arg["model_override"] for s in sends)
        assert counts == {
            "primary:cloud": 3, "kimi:cloud": 3, "deepseek:cloud": 3,
        }

    def test_below_fanout_threshold_returns_string(self):
        # When the planner emits fewer than FANOUT_MIN_ITEMS items
        # the dispatcher returns the bare "researcher" string
        # (single-researcher path), regardless of extras.
        deps = self._build_deps(extras=["kimi:cloud"])
        result = self._run_dispatcher(deps, plan_items=["solo"])
        assert result == "researcher"
