"""M-B: uniform role tool access.

Before M-B the researcher was the only default role that could call a
tool. planner / critic / meta_critic / synthesizer / adversary each ran
one tool-free call, so they could reason about the researcher's text but
never check it — which is why the CitationLinter had to exist at all.
The 2026-05-18 forensic traced a fabricated filename to a researcher
lane with zero tool calls, and the critic could not catch it because the
critic had no way to look.

Two properties dominate:

1. **Off by default, byte-identically.** The knob changes cost, not
   correctness — one LLM call per tool iteration per role per lane, and
   critic fans out per lane at the x-tiers. So an ungated role must be
   called with exactly the argument list it had before M-B, and
   ``_role_turn`` without tools must be indistinguishable from
   ``_single_shot``.
2. **Degradation is always downward.** Every failure in the tool path
   falls back to a single shot. Losing a critic's verdict because its
   loop misbehaved is strictly worse than a critic that reasons without
   having looked.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any, Optional

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from consultants.engine import council  # noqa: E402
from consultants.engine.graph import (  # noqa: E402
    TOOLABLE_ROLES,
    GraphDeps,
    _tools_for,
)


# ===================================================================== #
# Doubles
# ===================================================================== #
class FakeChat:
    """Records every payload it is handed."""

    def __init__(self, text="verdict: ready", raise_on_call=False):
        self.payloads: list[dict] = []
        self._text = text
        self._raise = raise_on_call

    def chat(self, payload: dict) -> dict:
        self.payloads.append(payload)
        if self._raise:
            raise RuntimeError("chat is down")
        # Usage lives under a ``usage`` key — see council._usage_from,
        # which accepts either OpenAI or Ollama field names inside it.
        return {"message": {"content": self._text},
                "usage": {"prompt_eval_count": 11, "eval_count": 7}}


def _spec(name="grep"):
    return [{"type": "function",
             "function": {"name": name, "description": "", "parameters": {}}}]


# ===================================================================== #
# _role_turn: the dispatch decision
# ===================================================================== #
class TestRoleTurnDispatch(unittest.TestCase):
    def test_no_tools_is_a_single_shot(self):
        chat = FakeChat()
        text, pt, ct = council._role_turn(chat, "m", [{"role": "user",
                                                       "content": "q"}])
        self.assertEqual(text, "verdict: ready")
        self.assertEqual((pt, ct), (11, 7))
        self.assertEqual(len(chat.payloads), 1)
        self.assertNotIn("tools", chat.payloads[0],
                         "an ungated role must not advertise tools")

    def test_specs_without_executor_is_still_a_single_shot(self):
        """Half-wired is not wired. Advertising tools the model cannot
        actually call would earn tool_calls we then drop on the floor."""
        chat = FakeChat()
        council._role_turn(chat, "m", [{"role": "user", "content": "q"}],
                           tool_specs=_spec(), tool_executor=None)
        self.assertNotIn("tools", chat.payloads[0])

    def test_executor_without_specs_is_still_a_single_shot(self):
        chat = FakeChat()
        council._role_turn(chat, "m", [{"role": "user", "content": "q"}],
                           tool_specs=[], tool_executor=lambda *a: "x")
        self.assertNotIn("tools", chat.payloads[0])

    def test_both_supplied_runs_the_loop(self):
        seen = {}

        def fake_loop(payload, cwd, **kw):
            seen.update(kw)
            seen["cwd"] = cwd
            return {"message": {"content": "looped"},
                    "usage": {"prompt_tokens": 3, "completion_tokens": 2}}

        text, pt, ct = council._role_turn(
            FakeChat(), "m", [{"role": "user", "content": "q"}],
            tool_specs=_spec(), tool_executor=lambda *a: "out",
            cwd="/proj", loop_runner=fake_loop, role="critic")
        self.assertEqual(text, "looped")
        self.assertEqual((pt, ct), (3, 2))
        self.assertEqual(seen["cwd"], "/proj")
        self.assertEqual(seen["tool_specs"], _spec())


class TestRoleTurnCaps(unittest.TestCase):
    def test_caps_are_tighter_than_the_researcher_s(self):
        """These roles check a handful of specific claims; they do not
        research. If a critic needs eight calls, the plan was wrong."""
        from claude_hooks.agent_loop.runner import (
            DEFAULT_MAX_TOOL_CALLS_PER_TURN,
        )
        self.assertLess(council.ROLE_TOOL_MAX_CALLS_PER_TURN,
                        DEFAULT_MAX_TOOL_CALLS_PER_TURN)

    def test_force_answer_fires_before_the_iteration_cap(self):
        """Otherwise a role can exhaust its iterations mid-tool-call and
        return no prose at all."""
        self.assertLess(council.ROLE_TOOL_FORCE_ANSWER_AFTER,
                        council.ROLE_TOOL_MAX_ITERATIONS)

    def test_first_tool_call_is_not_forced(self):
        """A critic with nothing to verify must be free to answer at
        once rather than burning a call proving it."""
        captured = {}

        def fake_loop(payload, cwd, **kw):
            captured["cfg"] = kw["config"]
            return {"message": {"content": "ok"}}

        council._role_turn(FakeChat(), "m", [], tool_specs=_spec(),
                           tool_executor=lambda *a: "", loop_runner=fake_loop)
        self.assertFalse(captured["cfg"].force_first_tool_call)
        self.assertEqual(captured["cfg"].max_iterations,
                         council.ROLE_TOOL_MAX_ITERATIONS)


class TestRoleTurnDegradesDownward(unittest.TestCase):
    def test_loop_exception_falls_back_to_a_single_shot(self):
        chat = FakeChat()

        def boom(payload, cwd, **kw):
            raise RuntimeError("loop exploded")

        text, _pt, _ct = council._role_turn(
            chat, "m", [{"role": "user", "content": "q"}],
            tool_specs=_spec(), tool_executor=lambda *a: "",
            loop_runner=boom, role="critic")
        self.assertEqual(text, "verdict: ready")
        self.assertEqual(len(chat.payloads), 1, "fell back to one call")

    def test_empty_loop_text_forces_a_tool_free_call(self):
        """The 2026-05-07 audit failure, generalised: a loop that ends
        mid-tool-call returns no prose, and a role returning '' is a
        silent hole downstream."""
        chat = FakeChat()

        def empty(payload, cwd, **kw):
            return {"message": {"content": "   "}}

        text, _pt, _ct = council._role_turn(
            chat, "m", [{"role": "user", "content": "q"}],
            tool_specs=_spec(), tool_executor=lambda *a: "",
            loop_runner=empty, role="synthesizer")
        self.assertEqual(text, "verdict: ready")
        self.assertEqual(len(chat.payloads), 1)

    def test_a_dead_chat_still_raises(self):
        """Degradation is downward, not silent: if the fallback single
        shot also fails there is nothing left to return, and the node's
        own tombstone branch must see the exception."""
        def boom(payload, cwd, **kw):
            raise RuntimeError("loop exploded")

        with self.assertRaises(RuntimeError):
            council._role_turn(
                FakeChat(raise_on_call=True), "m", [],
                tool_specs=_spec(), tool_executor=lambda *a: "",
                loop_runner=boom, role="critic")


# ===================================================================== #
# The graph gate
# ===================================================================== #
def _deps(**kw) -> GraphDeps:
    base = dict(chat_clients={}, models={}, enabled_roles=(), cwd="/proj")
    base.update(kw)
    return GraphDeps(**base)


class TestGraphGate(unittest.TestCase):
    def test_default_is_no_tooled_roles(self):
        self.assertEqual(_deps().tooled_roles, ())

    def test_ungated_role_gets_no_extra_kwargs(self):
        """Returning {} rather than tool_specs=None matters: an ungated
        role must be called with exactly its pre-M-B argument list."""
        d = _deps(tool_specs=_spec(), tool_executor=lambda *a: "")
        self.assertEqual(_tools_for(d, "critic"), {})

    def test_gated_role_gets_all_three(self):
        d = _deps(tooled_roles=("critic",), tool_specs=_spec(),
                  tool_executor=lambda *a: "")
        got = _tools_for(d, "critic")
        self.assertEqual(sorted(got), ["cwd", "tool_executor", "tool_specs"])
        self.assertEqual(got["cwd"], "/proj")

    def test_role_not_in_the_list_stays_ungated(self):
        d = _deps(tooled_roles=("critic",), tool_specs=_spec(),
                  tool_executor=lambda *a: "")
        self.assertEqual(_tools_for(d, "synthesizer"), {})

    def test_listed_but_no_surface_available(self):
        """A role cannot be handed tools that do not exist — e.g. the
        registry failed to build."""
        self.assertEqual(
            _tools_for(_deps(tooled_roles=("critic",)), "critic"), {})

    def test_researcher_is_not_in_the_toolable_list(self):
        """Its loop predates this gate and is wired directly; adding it
        here would give it two competing tool paths."""
        self.assertNotIn("researcher", TOOLABLE_ROLES)

    def test_toolable_roles_covers_every_single_shot_role(self):
        """If a new single-shot role is added, it must be listed here or
        it silently misses the uniform-access guarantee."""
        self.assertEqual(
            set(TOOLABLE_ROLES),
            {"planner", "critic", "meta_critic", "synthesizer", "adversary"})


# ===================================================================== #
# Node signatures accept the kwargs
# ===================================================================== #
class TestNodesAcceptTools(unittest.TestCase):
    def test_every_toolable_node_takes_the_three_kwargs(self):
        import inspect
        nodes = {
            "planner": council.planner_node,
            "critic": council.critic_node,
            "meta_critic": council.meta_critic_node,
            "synthesizer": council.synthesizer_node,
            "adversary": council.adversary_node,
        }
        self.assertEqual(set(nodes), set(TOOLABLE_ROLES))
        for role, fn in nodes.items():
            params = inspect.signature(fn).parameters
            for kw in ("tool_specs", "tool_executor", "cwd"):
                self.assertIn(kw, params, f"{role} missing {kw}")
            self.assertIsNone(params["tool_specs"].default,
                              f"{role}: tool_specs must default to None")

    def test_no_toolable_node_still_calls_single_shot_directly(self):
        """Regression guard. A node left on ``_single_shot`` silently
        opts out of uniform access while looking wired."""
        import inspect
        for name in ("planner_node", "critic_node", "meta_critic_node",
                     "synthesizer_node", "adversary_node"):
            src = inspect.getsource(getattr(council, name))
            self.assertNotIn("_single_shot(", src,
                             f"{name} must route through _role_turn")
            self.assertIn("_role_turn(", src)


# ===================================================================== #
# Config
# ===================================================================== #
class TestConfigKnob(unittest.TestCase):
    def test_all_roles_defaults_off(self):
        import consultants.config as cc
        self.assertFalse(cc.ConsultantsConfig().tools.all_roles)

    def test_round_trips_through_toml(self):
        import tomllib

        import consultants.config as cc
        cfg = cc.ConsultantsConfig()
        cfg.tools.all_roles = True
        raw = tomllib.loads(cc._render(cfg, override_flag=None))
        self.assertTrue(raw["tools"]["all_roles"])
        merged = cc._merge_layer(cc.ConsultantsConfig(), raw)
        self.assertTrue(merged.tools.all_roles)


if __name__ == "__main__":
    unittest.main()
