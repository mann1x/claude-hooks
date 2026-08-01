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
# The tool addendum
# ===================================================================== #
class TestToolAddendum(unittest.TestCase):
    """The 2026-08-01 live bench recorded 0 tool calls across 18 tooled
    trials. The surface was live; the prompts were not. Wiring a role to
    a toolbox while its system prompt describes a job that involves
    looking at nothing produces a knob that costs schema tokens and
    changes no behaviour."""

    def test_no_tools_means_no_addendum(self):
        self.assertEqual(council.build_tool_addendum("critic", None), "")
        self.assertEqual(council.build_tool_addendum("critic", []), "")

    def test_unknown_role_gets_no_addendum(self):
        """Better silent than wrong: a role with no directive would
        otherwise be handed a tool list and no reason to use it."""
        self.assertEqual(council.build_tool_addendum("researcher", _spec()),
                         "")
        self.assertEqual(council.build_tool_addendum(None, _spec()), "")

    def test_every_toolable_role_has_a_directive(self):
        """The regression guard for the bug this fixes. A role added to
        TOOLABLE_ROLES without a directive gets tools it is never told
        about — indistinguishable, from the outside, from the knob
        simply not working."""
        for role in TOOLABLE_ROLES:
            self.assertIn(role, council._ROLE_TOOL_DIRECTIVE, role)
            self.assertTrue(council.build_tool_addendum(role, _spec()), role)

    def test_names_come_from_the_specs_not_a_hardcoded_list(self):
        """A prose list goes stale the moment a provider is enabled —
        which is exactly how the researcher ended up being told about
        six tools while eleven were on offer."""
        out = council.build_tool_addendum(
            "critic", _spec("git_blame") + _spec("weird_new_tool"))
        self.assertIn("git_blame", out)
        self.assertIn("weird_new_tool", out)
        self.assertNotIn("read_file", out)

    def test_critic_directive_overrides_the_default_to_ready_pressure(self):
        """CRITIC_SYSTEM says 'default to ready' and 'each extra round
        costs another full agent loop'. Left unaddressed, a model reads
        that as 'investigating is expensive, say ready' — which is
        precisely what the transcripts showed. The directive has to
        distinguish a cheap tool call from an expensive research round
        and redefine what counts as a concrete missing fact."""
        d = council._ROLE_TOOL_DIRECTIVE["critic"]
        self.assertIn("concrete", d)
        self.assertIn("not another research round", d)

    def test_verifying_roles_are_told_to_check_equality(self):
        """The v1.2 bench's coherent failure: the tooled critic verified
        *existence* and stopped. It grepped the symbol, found it, and
        called a claim of `MAX_ATTEMPTS = 5` accurate against a file
        saying 15 — a grep that 'succeeds' survives both a wrong
        constant and a wrong line number."""
        for role in ("critic", "adversary"):
            d = council._ROLE_TOOL_DIRECTIVE[role]
            self.assertIn("EQUALITY, NOT EXISTENCE", d, role)

    def test_critic_and_adversary_are_told_not_to_silently_correct(self):
        """Worse than a miss: on hard-02 the critic fetched the right
        line, wrote it down, and still called the report accurate. The
        correction never left the model, and the synthesizer kept
        relaying the wrong cite."""
        for role in ("critic", "adversary"):
            self.assertIn("silently correct",
                          council._ROLE_TOOL_DIRECTIVE[role].lower(), role)

    def test_critic_correction_contract_is_attributable(self):
        """'Something was wrong' is not actionable downstream. The
        block names the report it corrects, using the header the critic
        and the synthesizer both see."""
        d = council._ROLE_TOOL_DIRECTIVE["critic"]
        self.assertIn("CORRECTIONS:", d)
        self.assertIn("RESEARCHER REPORT (round N)", d)
        self.assertIn("report N: claimed", d)

    def test_a_correction_does_not_trigger_another_research_round(self):
        """A resolved discrepancy is not a missing fact. Without this
        the critic reroutes on every off-by-one and the council loops
        on questions it has already answered."""
        d = council._ROLE_TOOL_DIRECTIVE["critic"]
        self.assertIn("NOT grounds for `needs_more_research`", d)

    def test_synthesizer_is_told_to_honour_corrections(self):
        """The other half of the channel. A correction the synthesizer
        ignores is the same wrong cite reaching the user, having cost
        an extra tool call to discover."""
        d = council._ROLE_TOOL_DIRECTIVE["synthesizer"]
        self.assertIn("CORRECTIONS:", d)
        self.assertIn("supersedes", d)

    def test_meta_critic_carries_corrections_forward(self):
        """The x-tier hole. In multi-critic mode the meta-critic's
        consolidated verdict REPLACES the individual critics', so a
        correction it drops is lost before the synthesizer sees it —
        the feature would work at low effort and silently degrade at
        exactly the tier that runs the most lanes."""
        d = council._ROLE_TOOL_DIRECTIVE["meta_critic"]
        self.assertIn("CORRECTIONS:", d)
        self.assertIn("REPLACES", d)

    def test_correction_contract_is_consistent_across_the_chain(self):
        """Producer, consolidator and consumer must name the same
        block, or the channel silently drops at a hop."""
        for role in ("critic", "meta_critic", "synthesizer"):
            self.assertIn("CORRECTIONS:", council._ROLE_TOOL_DIRECTIVE[role],
                          role)

    def test_adversary_directive_addresses_the_verify_gap(self):
        """ADVERSARY_SYSTEM asks it to catch 'fabricated or
        mis-attributed path:line citations' while giving it no way to
        check one — it could only judge whether a claim was reported."""
        d = council._ROLE_TOOL_DIRECTIVE["adversary"]
        self.assertIn("path:line", d)
        self.assertIn("NOT a refutation", d)

    def test_addendum_lands_on_the_system_turn(self):
        msgs = [{"role": "system", "content": "SYS"},
                {"role": "user", "content": "U"}]
        out = council._with_tool_addendum(msgs, "critic", _spec())
        self.assertTrue(out[0]["content"].startswith("SYS"))
        self.assertIn("TOOLS AVAILABLE TO YOU", out[0]["content"])
        self.assertEqual(out[1]["content"], "U")

    def test_does_not_mutate_the_caller_s_messages(self):
        """The same message list is reused across x-tier lanes. In-place
        appending would stack one addendum per lane, and the last lane
        would carry N copies."""
        msgs = [{"role": "system", "content": "SYS"}]
        council._with_tool_addendum(msgs, "critic", _spec())
        council._with_tool_addendum(msgs, "critic", _spec())
        self.assertEqual(msgs[0]["content"], "SYS")

    def test_missing_system_turn_still_carries_the_directive(self):
        out = council._with_tool_addendum(
            [{"role": "user", "content": "U"}], "critic", _spec())
        self.assertEqual(out[0]["role"], "system")
        self.assertIn("TOOLS AVAILABLE TO YOU", out[0]["content"])

    def test_untooled_role_never_sees_it(self):
        """A role that falls through to _single_shot must not be told
        about tools it will not be offered."""
        chat = FakeChat()
        council._role_turn(chat, "m", [{"role": "system", "content": "SYS"}])
        self.assertEqual(chat.payloads[0]["messages"][0]["content"], "SYS")

    def test_tooled_role_payload_carries_it(self):
        captured = {}

        def fake_loop(payload, cwd, **kw):
            captured["msgs"] = payload["messages"]
            return {"message": {"content": "ok"}}

        council._role_turn(FakeChat(), "m",
                           [{"role": "system", "content": "SYS"}],
                           tool_specs=_spec(), tool_executor=lambda *a: "",
                           loop_runner=fake_loop, role="critic")
        self.assertIn("TOOLS AVAILABLE TO YOU", captured["msgs"][0]["content"])

    def test_fallback_to_single_shot_drops_the_addendum(self):
        """When the loop dies the role answers tool-free, so a directive
        telling it to go look would be actively misleading."""
        chat = FakeChat()

        def boom(payload, cwd, **kw):
            raise RuntimeError("nope")

        council._role_turn(chat, "m", [{"role": "system", "content": "SYS"}],
                           tool_specs=_spec(), tool_executor=lambda *a: "",
                           loop_runner=boom, role="critic")
        self.assertEqual(chat.payloads[0]["messages"][0]["content"], "SYS")


class TestExtraToolsNote(unittest.TestCase):
    """RESEARCHER_SYSTEM and the tool-plan prompt name their six tools
    in prose, and a model works from that list rather than the schema
    array. Enabling a provider puts tools in the payload that the role
    has effectively been told do not exist."""

    def test_default_surface_adds_nothing(self):
        """Load-bearing for cohort-2 parity: rewriting the prose would
        change the default prompt byte-for-byte and invalidate the M11c
        corpus, so only the difference is named."""
        from claude_hooks.caliber_proxy.tools import openai_tool_specs
        self.assertEqual(
            council.build_extra_tools_note(openai_tool_specs()), "")

    def test_no_tools_adds_nothing(self):
        self.assertEqual(council.build_extra_tools_note(None), "")

    def test_names_only_the_extras(self):
        from claude_hooks.caliber_proxy.tools import openai_tool_specs
        note = council.build_extra_tools_note(
            openai_tool_specs() + _spec("git_blame"))
        self.assertIn("git_blame", note)
        self.assertNotIn("read_file", note)

    def test_lands_on_the_last_system_turn(self):
        msgs = [{"role": "system", "content": "GROUNDING"},
                {"role": "system", "content": "ROLE"},
                {"role": "user", "content": "U"}]
        out = council._with_extra_tools_note(msgs, _spec("git_blame"))
        self.assertEqual(out[0]["content"], "GROUNDING")
        self.assertIn("ALSO AVAILABLE", out[1]["content"])

    def test_identity_when_empty(self):
        """Returning the same object, not a copy, keeps the default
        researcher path allocation-identical to pre-addendum."""
        msgs = [{"role": "system", "content": "S"}]
        self.assertIs(council._with_extra_tools_note(msgs, None), msgs)

    def test_does_not_mutate(self):
        msgs = [{"role": "system", "content": "S"}]
        council._with_extra_tools_note(msgs, _spec("git_blame"))
        self.assertEqual(msgs[0]["content"], "S")


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
