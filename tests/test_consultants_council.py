"""Tests for ``consultants.engine.council`` and the topology helper
in ``consultants.engine.graph``.

These tests run in the main ``claude-hooks`` conda env — they do NOT
import langgraph. ``build_council_graph`` itself is exercised in a
follow-up integration test that requires the
``claude-hooks-consultants`` env.
"""

from __future__ import annotations

import pytest

from consultants.engine import council, graph
from consultants.engine.storage import RoleTurn


# ----------------------- fake chat client ------------------------- #

class FakeChatClient:
    """Minimal chat-client stand-in. Returns a queued response per
    call; raises on overflow so test bugs surface."""

    def __init__(self, replies: list[dict]):
        self._replies = list(replies)
        self.calls: list[dict] = []

    def chat(self, payload: dict) -> dict:
        self.calls.append(payload)
        if not self._replies:
            raise AssertionError("FakeChatClient out of replies")
        return self._replies.pop(0)


def _completion(text: str, *, prompt_tokens: int = 10,
                completion_tokens: int = 5) -> dict:
    """OpenAI-shape completion response, matching what
    ChatClient._from_ollama produces."""
    return {
        "choices": [{
            "message": {"content": text},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


# ----------------------- effort caps ------------------------------ #

class TestEffortCaps:
    def test_low_disables_critic_loop(self):
        c = council.caps_for("low")
        assert c.researcher_rounds_max == 1
        assert c.critic_reroutes_max == 0

    def test_medium_default(self):
        c = council.caps_for("medium")
        assert c.researcher_rounds_max == 1
        assert c.critic_reroutes_max == 1

    def test_high_allows_more_loops(self):
        c = council.caps_for("high")
        assert c.researcher_rounds_max == 3
        assert c.critic_reroutes_max == 2

    def test_unknown_falls_back_to_medium(self):
        assert council.caps_for("nonsense") == council.caps_for("medium")


# ----------------------- critic decision parser ------------------- #

class TestParseCriticDecision:
    @pytest.mark.parametrize("text,expected", [
        ("DECISION: ready\nThe evidence covers all gaps.", "ready"),
        ("decision: READY", "ready"),
        ("DECISION: needs_more_research\n- check util.py:42", "needs_more_research"),
        ("Some preamble.\n\nDECISION: ready\nGood enough.", "ready"),
        ("DECISION:ready", "ready"),  # tight spacing
        ("  DECISION : needs_more_research  ", "needs_more_research"),
    ])
    def test_explicit_decisions(self, text, expected):
        assert council.parse_critic_decision(text) == expected

    def test_missing_decision_defaults_to_ready(self):
        assert council.parse_critic_decision("looks fine to me") == "ready"

    def test_empty_string_defaults_to_ready(self):
        assert council.parse_critic_decision("") == "ready"

    def test_none_safe(self):
        assert council.parse_critic_decision(None) == "ready"

    def test_first_match_wins(self):
        text = "DECISION: ready\nDECISION: needs_more_research"
        assert council.parse_critic_decision(text) == "ready"


# ----------------------- routing ---------------------------------- #

class TestRouteAfterCritic:
    def _state(self, **overrides):
        s = {
            "critic_decision": "ready",
            "research_rounds_used": 1,
            "critic_reroutes_used": 0,
            "effort": "medium",
        }
        s.update(overrides)
        return s

    def test_ready_goes_to_synthesizer(self):
        assert council.route_after_critic(self._state()) == \
            council.ROUTE_SYNTHESIZER

    def test_needs_more_with_budget_loops_back(self):
        s = self._state(critic_decision="needs_more_research",
                        research_rounds_used=1,
                        critic_reroutes_used=0,
                        effort="high")
        assert council.route_after_critic(s) == council.ROUTE_RESEARCHER

    def test_reroute_cap_forces_synthesizer(self):
        # medium caps: critic_reroutes_max=1
        s = self._state(critic_decision="needs_more_research",
                        critic_reroutes_used=1,
                        effort="medium")
        assert council.route_after_critic(s) == council.ROUTE_SYNTHESIZER

    def test_research_round_cap_forces_synthesizer(self):
        s = self._state(critic_decision="needs_more_research",
                        research_rounds_used=1,
                        critic_reroutes_used=0,
                        effort="medium")  # researcher_rounds_max=1
        assert council.route_after_critic(s) == council.ROUTE_SYNTHESIZER

    def test_high_effort_allows_multiple_loops(self):
        s = self._state(critic_decision="needs_more_research",
                        research_rounds_used=2,
                        critic_reroutes_used=1,
                        effort="high")  # rounds_max=3, reroutes_max=2
        assert council.route_after_critic(s) == council.ROUTE_RESEARCHER

    def test_missing_decision_defaults_ready(self):
        s = self._state(critic_decision=None)
        assert council.route_after_critic(s) == council.ROUTE_SYNTHESIZER


# ----------------------- prompt builders -------------------------- #

class TestPromptBuilders:
    def test_planner_messages_have_system_and_user(self):
        msgs = council.build_planner_messages("How does X work?")
        assert msgs[0]["role"] == "system"
        assert "PLANNER" in msgs[0]["content"]
        assert msgs[1] == {"role": "user", "content": "How does X work?"}

    def test_planner_strips_whitespace(self):
        msgs = council.build_planner_messages("  q  \n")
        assert msgs[1]["content"] == "q"

    def test_researcher_includes_plan_and_grounding(self):
        ground = [{"role": "system", "content": "GROUNDING-BLOCK"}]
        msgs = council.build_researcher_messages(
            "q", "1. step\n2. step", [], ground,
        )
        assert msgs[0]["content"] == "GROUNDING-BLOCK"
        assert any("RESEARCHER" in m["content"] for m in msgs
                   if m["role"] == "system")
        body = msgs[-1]["content"]
        assert "USER QUESTION:\nq" in body
        assert "PLAN FROM PLANNER:\n1. step" in body
        assert "PRIOR REPORT" not in body  # no prior rounds

    def test_researcher_includes_prior_rounds_when_present(self):
        msgs = council.build_researcher_messages(
            "q", "p", ["found A at f.py:10", "found B at g.py:20"], [],
        )
        body = msgs[-1]["content"]
        assert "PRIOR REPORT (round 1)" in body
        assert "PRIOR REPORT (round 2)" in body
        assert "found A at f.py:10" in body
        assert "critic asked for MORE" in body

    def test_critic_messages_include_reports(self):
        msgs = council.build_critic_messages(
            "q", "p", ["report1", "report2"],
        )
        body = msgs[-1]["content"]
        assert "RESEARCHER REPORT (round 1)" in body
        assert "RESEARCHER REPORT (round 2)" in body
        assert "report1" in body
        assert "report2" in body

    def test_synthesizer_includes_critique_when_present(self):
        msgs = council.build_synthesizer_messages(
            "q", "p", ["r1"], "DECISION: ready\nGood.",
        )
        body = msgs[-1]["content"]
        assert "CRITIC'S VERDICT" in body
        assert "DECISION: ready" in body

    def test_synthesizer_skips_critique_when_none(self):
        msgs = council.build_synthesizer_messages("q", "p", ["r1"], None)
        body = msgs[-1]["content"]
        assert "CRITIC'S VERDICT" not in body


# ----------------------- planner_node ----------------------------- #

class TestPlannerNode:
    def test_records_plan_and_turn(self):
        client = FakeChatClient([_completion("1. step\n2. step")])
        state = council.initial_state(
            question="q", cwd="/tmp", models={"planner": "m"},
            topology="council", effort="medium",
        )
        update = council.planner_node(state, chat_client=client, model="m")
        assert update["plan"] == "1. step\n2. step"
        assert len(update["turns"]) == 1
        t = update["turns"][0]
        assert isinstance(t, RoleTurn)
        assert t.role == "planner"
        assert t.prompt_tokens == 10
        assert t.completion_tokens == 5

    def test_failure_records_error(self):
        class BoomClient:
            def chat(self, payload):
                raise RuntimeError("upstream 500")
        state = council.initial_state(
            question="q", cwd="/tmp", models={"planner": "m"},
            topology="council", effort="medium",
        )
        update = council.planner_node(state, chat_client=BoomClient(),
                                      model="m")
        assert "planner failed" in update["error"]
        assert update["_role_failed"] == "planner"

    def test_token_totals_accumulate(self):
        client = FakeChatClient([_completion("p", prompt_tokens=100,
                                             completion_tokens=50)])
        state = council.initial_state(
            question="q", cwd="/tmp", models={"planner": "m"},
            topology="council", effort="medium",
        )
        state["total_prompt_tokens"] = 5
        state["total_completion_tokens"] = 7
        update = council.planner_node(state, chat_client=client, model="m")
        assert update["total_prompt_tokens"] == 105
        assert update["total_completion_tokens"] == 57


# ----------------------- researcher_node -------------------------- #

class TestResearcherNode:
    def test_uses_loop_runner_and_records_round(self):
        # Stub loop_runner: returns a final response without calling
        # any tools or chat_fn. Lets us avoid importing run_loop.
        def fake_loop(payload, cwd, *, config, tool_specs, chat_fn,
                      tool_executor, preseed_builder=None):
            return _completion("found X at f.py:10",
                               prompt_tokens=200, completion_tokens=80)

        client = FakeChatClient([])  # never called via fake_loop
        state = council.initial_state(
            question="q", cwd="/proj",
            models={"researcher": "m"},
            topology="council", effort="medium",
        )
        state["plan"] = "1. read f.py"
        update = council.researcher_node(
            state,
            chat_client=client,
            tool_executor=lambda *a, **k: "",
            tool_specs=[],
            grounding_msgs=[],
            model="m",
            cwd="/proj",
            loop_runner=fake_loop,
        )
        assert update["research"] == ["found X at f.py:10"]
        assert update["research_rounds_used"] == 1
        assert update["turns"][0].role == "researcher"
        assert update["turns"][0].round == 1
        assert update["total_prompt_tokens"] == 200

    def test_subsequent_round_increments_count(self):
        def fake_loop(payload, cwd, **kw):
            return _completion("round 2 findings")

        state = council.initial_state(
            question="q", cwd="/proj",
            models={"researcher": "m"},
            topology="council", effort="high",
        )
        state["plan"] = "p"
        state["research"] = ["round 1 prior"]
        state["research_rounds_used"] = 1
        update = council.researcher_node(
            state, chat_client=FakeChatClient([]),
            tool_executor=lambda *a, **k: "",
            tool_specs=[], grounding_msgs=[], model="m", cwd="/proj",
            loop_runner=fake_loop,
        )
        assert update["research"] == ["round 1 prior", "round 2 findings"]
        assert update["research_rounds_used"] == 2
        assert update["turns"][0].round == 2

    def test_loop_failure_returns_error(self):
        def boom(payload, cwd, **kw):
            raise RuntimeError("ollama unreachable")
        state = council.initial_state(
            question="q", cwd="/proj",
            models={"researcher": "m"},
            topology="council", effort="medium",
        )
        state["plan"] = "p"
        update = council.researcher_node(
            state, chat_client=FakeChatClient([]),
            tool_executor=lambda *a, **k: "",
            tool_specs=[], grounding_msgs=[], model="m", cwd="/proj",
            loop_runner=boom,
        )
        assert "researcher failed" in update["error"]


# ----------------------- critic_node ------------------------------ #

class TestCriticNode:
    def test_ready_decision_no_reroute_increment(self):
        client = FakeChatClient([_completion(
            "DECISION: ready\nLooks complete.")])
        state = council.initial_state(
            question="q", cwd="/p", models={"critic": "m"},
            topology="council", effort="medium",
        )
        state["plan"] = "p"
        state["research"] = ["r1"]
        state["research_rounds_used"] = 1
        update = council.critic_node(state, chat_client=client, model="m")
        assert update["critic_decision"] == "ready"
        assert update["critic_reroutes_used"] == 0

    def test_needs_more_increments_reroutes(self):
        client = FakeChatClient([_completion(
            "DECISION: needs_more_research\n- gap A\n- gap B")])
        state = council.initial_state(
            question="q", cwd="/p", models={"critic": "m"},
            topology="council", effort="high",
        )
        state["plan"] = "p"
        state["research"] = ["r1"]
        state["research_rounds_used"] = 1
        state["critic_reroutes_used"] = 0
        update = council.critic_node(state, chat_client=client, model="m")
        assert update["critic_decision"] == "needs_more_research"
        assert update["critic_reroutes_used"] == 1

    def test_failure_defaults_to_ready(self):
        class BoomClient:
            def chat(self, payload):
                raise RuntimeError("500")
        state = council.initial_state(
            question="q", cwd="/p", models={"critic": "m"},
            topology="council", effort="medium",
        )
        state["plan"] = "p"
        state["research"] = ["r1"]
        update = council.critic_node(state, chat_client=BoomClient(),
                                     model="m")
        assert update["critic_decision"] == "ready"
        assert "critic failed" in update["error"]


# ----------------------- synthesizer_node ------------------------- #

class TestSynthesizerNode:
    def test_records_final_answer(self):
        client = FakeChatClient([_completion("**Answer**: yes.")])
        state = council.initial_state(
            question="q", cwd="/p", models={"synthesizer": "m"},
            topology="council", effort="medium",
        )
        state["plan"] = "p"
        state["research"] = ["r1"]
        state["critique"] = "DECISION: ready\nGood."
        update = council.synthesizer_node(state, chat_client=client,
                                          model="m")
        assert update["final_answer"] == "**Answer**: yes."
        assert update["turns"][0].role == "synthesizer"

    def test_failure_produces_placeholder_answer(self):
        class BoomClient:
            def chat(self, payload):
                raise RuntimeError("crash")
        state = council.initial_state(
            question="q", cwd="/p", models={"synthesizer": "m"},
            topology="council", effort="medium",
        )
        update = council.synthesizer_node(state, chat_client=BoomClient(),
                                          model="m")
        assert "consultation incomplete" in update["final_answer"]
        assert update["_role_failed"] == "synthesizer"


# ----------------------- topology builder ------------------------- #

class TestPlanTopology:
    def test_full_council(self):
        edges = graph.plan_topology(
            ("planner", "researcher", "critic", "synthesizer")
        )
        assert ("START", "planner") in edges
        assert ("planner", "researcher") in edges
        assert ("researcher", "critic") in edges
        # Critic's outgoing edge is conditional, NOT in this list.
        assert not any(src == "critic" for src, _ in edges)
        assert ("synthesizer", "END") in edges

    def test_no_critic(self):
        edges = graph.plan_topology(
            ("planner", "researcher", "synthesizer")
        )
        assert ("START", "planner") in edges
        assert ("planner", "researcher") in edges
        assert ("researcher", "synthesizer") in edges
        assert ("synthesizer", "END") in edges

    def test_planner_only(self):
        edges = graph.plan_topology(("planner", "synthesizer"))
        assert ("START", "planner") in edges
        assert ("planner", "synthesizer") in edges
        assert ("synthesizer", "END") in edges

    def test_researcher_only(self):
        edges = graph.plan_topology(("researcher", "synthesizer"))
        assert ("START", "researcher") in edges
        assert ("researcher", "synthesizer") in edges

    def test_planner_plus_critic_no_researcher(self):
        # Ends in critic so we DON'T emit critic->synthesizer here;
        # build_council_graph adds an unconditional edge in this
        # specific config (researcher disabled).
        edges = graph.plan_topology(("planner", "critic", "synthesizer"))
        assert ("START", "planner") in edges
        assert ("planner", "critic") in edges
        assert not any(src == "critic" for src, _ in edges)
        assert ("synthesizer", "END") in edges

    def test_synthesizer_only_pathological(self):
        # validate_pipeline rejects this in the config layer, but
        # plan_topology is defensive.
        edges = graph.plan_topology(("synthesizer",))
        assert ("START", "synthesizer") in edges
        assert ("synthesizer", "END") in edges

    def test_synthesizer_required(self):
        with pytest.raises(ValueError):
            graph.plan_topology(("planner",))


# ----------------------- build_council_graph ---------------------- #

class TestBuildCouncilGraph:
    def test_skipped_when_langgraph_missing(self):
        """If langgraph isn't installed, build_council_graph raises a
        helpful RuntimeError rather than ImportError. We can't easily
        force ImportError here without monkey-patching sys.modules,
        so this test only runs when langgraph is genuinely absent."""
        try:
            import langgraph  # noqa: F401
        except ImportError:
            pass
        else:
            pytest.skip("langgraph is installed in this env")
        deps = graph.GraphDeps(
            chat_clients={}, models={"synthesizer": "m"},
            enabled_roles=("synthesizer",), cwd="/p",
        )
        with pytest.raises(RuntimeError, match="langgraph is not installed"):
            graph.build_council_graph(deps)


# ----------------------- initial_state ---------------------------- #

class TestInitialState:
    def test_has_all_keys(self):
        s = council.initial_state(
            question="q", cwd="/p", models={"planner": "m"},
            topology="council", effort="medium",
        )
        for k in ("question", "cwd", "models", "topology", "effort",
                  "plan", "research", "critique", "critic_decision",
                  "final_answer", "turns",
                  "research_rounds_used", "critic_reroutes_used",
                  "total_prompt_tokens", "total_completion_tokens",
                  "retries_by_role"):
            assert k in s, f"missing key: {k}"

    def test_defaults_zero_counters(self):
        s = council.initial_state(
            question="q", cwd="/p", models={}, topology="council",
            effort="medium",
        )
        assert s["research_rounds_used"] == 0
        assert s["critic_reroutes_used"] == 0
        assert s["total_prompt_tokens"] == 0
        assert s["turns"] == []
