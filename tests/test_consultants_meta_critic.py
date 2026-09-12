"""Phase 10 tests — multi-critic fan-out + meta-critic combine
at the xmax effort tier.

Coverage:
- ``build_meta_critic_messages`` produces an anonymized prompt
  (``Critic 1`` / ``Critic 2`` / ...) and never leaks model names.
- ``meta_critic_node`` happy path: writes consolidated
  ``critique`` / ``critic_decision`` / +1 reroute on
  needs_more_research, +0 on ready.
- ``meta_critic_node`` failure path: defaults to ready, surfaces
  the error in critique, doesn't bring down the council.
- ``critic_node`` in multi-critic mode (``lane_idx`` set) suppresses
  ``critic_decision`` / ``critique`` / ``critic_reroutes_used``
  writes (meta-critic is the sole writer); recorder still gets
  the per-lane ``model`` row for audit mapping.
- ``critic_node`` in single-critic mode (``lane_idx`` None) writes
  the full delta as before — backward-compatible.
- Runner attaches ``extra_models_by_role['critic']`` to GraphDeps
  ONLY at xmax. xhigh + critic_extras silently ignores them. xmax
  with no critic_extras leaves the dict empty (degrades to single-
  critic).
- Runner respects xmedium's critic-drop (mirrors medium).
"""

from __future__ import annotations

import logging

import pytest

from consultants import config as cc
from consultants.engine import council
from consultants.engine.storage import RoleTurn
from consultants.server import runner as prod_runner


# ---------------------- prompt assembly -------------------------- #

class TestMetaCriticPrompt:
    def test_anonymizes_critic_identities(self):
        msgs = council.build_meta_critic_messages(
            question="why?", plan="1. step",
            research_rounds=["finding A"],
            critic_verdicts=[
                "DECISION: ready\nA is fine.",
                "DECISION: needs_more_research\nMissing B.",
            ],
        )
        # System + user; user message body has anonymized Critic N.
        assert msgs[0]["role"] == "system"
        body = msgs[1]["content"]
        assert "Critic 1" in body or "CRITIC 1" in body
        assert "Critic 2" in body or "CRITIC 2" in body
        # Identity must NOT leak — the prompt builder only sees
        # the verdict text, not the originating model tag.
        assert "kimi-k2.6:cloud" not in body
        assert "deepseek-v4-pro:cloud" not in body
        assert "qwen3.5:cloud" not in body
        # Both verdicts are surfaced verbatim.
        assert "A is fine" in body
        assert "Missing B" in body

    def test_empty_verdicts_emits_explicit_note(self):
        # Defensive: meta-critic invoked with no critic verdicts
        # (e.g. all C critics tombstoned) gets an explicit note so
        # it doesn't hallucinate a synthesized critique out of
        # nothing.
        msgs = council.build_meta_critic_messages(
            question="q", plan="p",
            research_rounds=["r"],
            critic_verdicts=[],
        )
        body = msgs[1]["content"]
        assert "no critic verdicts" in body.lower()


# ---------------------- meta_critic_node ------------------------- #

class _StubChatClient:
    """Returns canned replies; records the payload it was given so
    tests can assert on prompt content."""
    def __init__(self, reply: str, *, prompt_tokens: int = 50,
                 completion_tokens: int = 20):
        self.reply = reply
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.calls: list[dict] = []

    def chat(self, payload: dict) -> dict:
        self.calls.append(payload)
        return {
            "choices": [{
                "message": {"content": self.reply},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
            },
        }


class TestMetaCriticNode:
    @staticmethod
    def _state_with_critics(decisions: list[str]) -> dict:
        # Build a CouncilState with C critic turns inlined into the
        # additive `turns` list. Imitates what LangGraph's reducer
        # would produce after a Send-fanout barrier.
        st = council.initial_state(
            question="why is the sky blue?",
            cwd="/p", models={"critic": "primary:cloud"},
            topology="council", effort="xmax",
        )
        st["plan"] = "1. measure"
        st["research"] = ["finding 1", "finding 2"]
        st["research_rounds_used"] = 1
        st["turns"] = [
            RoleTurn(role="critic", round=1,
                     content=f"DECISION: {d}\nReasoning {i}.",
                     prompt_tokens=10, completion_tokens=5,
                     duration_seconds=1.0)
            for i, d in enumerate(decisions, start=1)
        ]
        return st

    def test_happy_path_ready_writes_consolidated_critique(self):
        client = _StubChatClient(
            reply="DECISION: ready\nAll three critics agree.",
            prompt_tokens=120, completion_tokens=40,
        )
        st = self._state_with_critics(["ready", "ready", "ready"])
        update = council.meta_critic_node(
            st, chat_client=client, model="primary:cloud",
        )
        assert update["critic_decision"] == "ready"
        assert "All three critics agree" in update["critique"]
        assert update["critic_reroutes_used"] == 0
        # One LLM call, with the 3 critic verdicts in the prompt.
        assert len(client.calls) == 1
        body = client.calls[0]["messages"][1]["content"]
        assert "Reasoning 1" in body
        assert "Reasoning 2" in body
        assert "Reasoning 3" in body

    def test_disagreement_meta_critic_picks_side(self):
        # 2-of-3 critics say needs_more_research; meta-critic picks
        # needs_more_research and adds 1 reroute.
        client = _StubChatClient(
            reply="DECISION: needs_more_research\n"
                  "The dissent named foo.py:42 — investigate.",
        )
        st = self._state_with_critics([
            "needs_more_research", "ready", "needs_more_research",
        ])
        update = council.meta_critic_node(
            st, chat_client=client, model="primary:cloud",
        )
        assert update["critic_decision"] == "needs_more_research"
        assert update["critic_reroutes_used"] == 1

    def test_failure_defaults_to_ready_with_error_critique(self):
        class Boom:
            def chat(self, payload):
                raise RuntimeError("upstream 500")
        st = self._state_with_critics(["ready"])
        update = council.meta_critic_node(
            st, chat_client=Boom(), model="primary:cloud",
        )
        assert update["critic_decision"] == "ready"
        assert "meta-critic failed" in update["critique"]
        assert update["_role_failed"] == "meta_critic"

    def test_meta_critic_filters_to_current_round(self):
        # State carries critic turns from prior rounds (re-route
        # cycles). Meta-critic should look only at this round's
        # critics, not historical ones.
        client = _StubChatClient(
            reply="DECISION: ready\nLooks good.",
        )
        st = self._state_with_critics(["ready"])
        # Inject a stale prior-round critic turn.
        st["turns"].insert(0, RoleTurn(
            role="critic", round=1, content="OLD VERDICT",
            prompt_tokens=10, completion_tokens=5,
            duration_seconds=1.0,
        ))
        # Bump research_rounds_used so this_round becomes 2 — the
        # stale round-1 turn must not appear in the meta-critic's
        # prompt.
        st["research_rounds_used"] = 2
        # Add a current-round critic turn at round=2.
        st["turns"].append(RoleTurn(
            role="critic", round=2,
            content="DECISION: ready\nFresh verdict.",
            prompt_tokens=10, completion_tokens=5,
            duration_seconds=1.0,
        ))
        council.meta_critic_node(
            st, chat_client=client, model="primary:cloud",
        )
        body = client.calls[0]["messages"][1]["content"]
        assert "OLD VERDICT" not in body
        assert "Fresh verdict" in body


# ---------------------- critic_node multi-critic mode ----------- #

class TestCriticNodeMultiMode:
    """When critic_node is invoked with ``lane_idx`` (the runner's
    multi-critic-mode signal), it must NOT write critic_decision,
    critique, or critic_reroutes_used — meta-critic is the sole
    writer. The recorder still gets the per-lane row for audit."""

    def test_lane_idx_set_suppresses_decision_writes(self, tmp_path):
        from consultants.engine.recorder import (
            MessageRecorder, RecorderMeta,
        )
        meta = RecorderMeta(
            sid="csl-multi", cwd=str(tmp_path),
            question="q", effort="xmax", topology="council",
            models={"critic": "primary"},
        )
        rec = MessageRecorder(tmp_path / "transcript.db", meta=meta)
        try:
            client = _StubChatClient(
                reply="DECISION: needs_more_research\nGap at foo.",
                prompt_tokens=42, completion_tokens=11,
            )
            state = council.initial_state(
                question="q", cwd="/p", models={"critic": "primary"},
                topology="council", effort="xmax",
            )
            state["plan"] = "p"
            state["research"] = ["r"]
            # multi-critic mode signaled by lane_idx + model_override.
            state["lane_idx"] = 1
            state["model_override"] = "extra-model:cloud"
            update = council.critic_node(
                state, chat_client=client, model="primary",
                recorder=rec,
            )
            # critic_decision / critique / reroutes — all suppressed.
            assert "critic_decision" not in update
            assert "critique" not in update
            assert "critic_reroutes_used" not in update
            # turns + token totals still flow.
            assert "turns" in update
            assert update["turns"][0].role == "critic"
            # Recorder still got the per-lane row with the override
            # model — the audit map back to who-said-what.
            import sqlite3
            conn = sqlite3.connect(str(rec.db_path))
            row = conn.execute(
                "SELECT model, lane_idx FROM events "
                "WHERE kind='llm_call' AND role='critic' LIMIT 1"
            ).fetchone()
            conn.close()
            assert row[0] == "extra-model:cloud"
            assert row[1] == 1
        finally:
            rec.close()

    def test_lane_idx_none_writes_full_delta(self):
        # Single-critic mode — the legacy path. critic_node writes
        # critic_decision + critique + reroutes as before.
        client = _StubChatClient(
            reply="DECISION: ready\nlgtm",
        )
        state = council.initial_state(
            question="q", cwd="/p", models={"critic": "primary"},
            topology="council", effort="high",
        )
        state["plan"] = "p"
        state["research"] = ["r"]
        # No lane_idx -> single critic path
        update = council.critic_node(
            state, chat_client=client, model="primary",
        )
        assert update["critic_decision"] == "ready"
        assert "lgtm" in update["critique"]
        assert update["critic_reroutes_used"] == 0


# ---------------------- runner wiring at xmax ------------------- #

class TestRunnerXmaxWiring:
    """The runner attaches extra_models_by_role['critic'] to
    GraphDeps ONLY at xmax. xhigh+critic_extras silently ignores
    them (Phase 10 critic fan-out is xmax-only by design)."""

    def _build_session(self, tmp_path, effort: str,
                       researcher_extras: list[str] = None,
                       critic_extras: list[str] = None,
                       enabled_critic: bool = True):
        """Build a SessionState + cfg, mock the graph build, and
        return the captured GraphDeps."""
        from consultants.server.app import SessionState

        captured: dict = {}

        class _FakeCompiled:
            def stream(self, initial, *, stream_mode):
                yield ("values", dict(initial))

        def fake_build_council_graph(deps, *, tracer=None,
                                      checkpointer=None, interrupt_before=None):
            captured["deps"] = deps
            return _FakeCompiled()

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
            cfg.roles["researcher"].extra_models = list(
                researcher_extras or []
            )
            cfg.roles["critic"].extra_models = list(
                critic_extras or []
            )
            if not enabled_critic:
                cfg.roles["critic"].enabled = False
            state = SessionState(
                sid="csl-xmax-test", cwd=str(tmp_path),
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

    def test_xmax_attaches_critic_extras_to_deps(self, tmp_path):
        deps = self._build_session(
            tmp_path, "xmax",
            critic_extras=["deepseek-v4-pro:cloud", "qwen3.5:cloud"],
        )
        assert deps.extra_models_by_role.get("critic") == [
            "deepseek-v4-pro:cloud", "qwen3.5:cloud",
        ]

    def test_xhigh_silently_ignores_critic_extras(self, tmp_path):
        # critic_extras is set, but effort=xhigh — Phase 10 critic
        # fan-out is xmax-only by design. xhigh respects researcher
        # extras only.
        deps = self._build_session(
            tmp_path, "xhigh",
            researcher_extras=["kimi:cloud"],
            critic_extras=["deepseek:cloud"],
        )
        assert "researcher" in deps.extra_models_by_role
        assert "critic" not in deps.extra_models_by_role

    def test_xmax_no_critic_extras_is_single_critic(self, tmp_path):
        # xmax with empty critic_extras → no critic fan-out;
        # graph builds with the single-critic path.
        deps = self._build_session(
            tmp_path, "xmax",
            researcher_extras=["kimi:cloud"],
            critic_extras=[],
        )
        assert "critic" not in deps.extra_models_by_role

    def test_xmax_critic_disabled_skips_critic_extras(self, tmp_path):
        # If the user disabled the critic role entirely, even xmax
        # with critic_extras shouldn't try to fan it out — there's
        # no critic in the enabled set.
        deps = self._build_session(
            tmp_path, "xmax",
            critic_extras=["any:cloud"],
            enabled_critic=False,
        )
        assert "critic" not in deps.extra_models_by_role

    def test_xmedium_drops_critic_like_medium(self, tmp_path):
        # base_effort('xmedium') == 'medium' which drops the critic.
        # Verify by checking the deps' enabled_roles.
        deps = self._build_session(tmp_path, "xmedium")
        assert "critic" not in deps.enabled_roles

    def test_xhigh_keeps_critic_like_high(self, tmp_path):
        deps = self._build_session(tmp_path, "xhigh")
        assert "critic" in deps.enabled_roles

    def test_xmax_keeps_critic_like_max(self, tmp_path):
        deps = self._build_session(tmp_path, "xmax")
        assert "critic" in deps.enabled_roles

    def test_warn_extras_cost_fires_for_critic_at_xmax(
            self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING,
                             logger="consultants.server.runner"):
            self._build_session(
                tmp_path, "xmax",
                critic_extras=["deepseek-v4-pro:cloud"],
            )
        msg = "\n".join(r.getMessage() for r in caplog.records)
        # Both researcher (none here) and critic warnings fire as
        # appropriate. We only set critic extras → only critic fires.
        assert "critic" in msg
        assert "deepseek-v4-pro:cloud" in msg
        assert "xmax" in msg


# ---------------------- end-to-end via the graph builder ------- #

class TestGraphTopologyMultiCritic:
    """Verify the graph builder wires meta_critic into the topology
    only when critic extras are present + multi-critic active."""

    def _try_build(self, *, effort: str,
                   researcher_extras: list[str],
                   critic_extras: list[str]):
        """Build a real council graph (langgraph required). Skip if
        not available — these tests are integration with the
        consultants conda env."""
        pytest.importorskip("langgraph.graph")  # concrete leaf — ghost-dir proof
        from consultants.engine.graph import (
            GraphDeps, build_council_graph,
        )

        class _FakeChat:
            def chat(self, payload):
                return {"choices": [
                    {"message": {"content": "x"},
                     "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 0,
                              "completion_tokens": 0}}

        deps = GraphDeps(
            chat_clients={
                r: _FakeChat() for r in
                ("planner", "researcher", "critic", "synthesizer")
            },
            models={
                "planner": "p:cloud", "researcher": "r:cloud",
                "critic": "c:cloud", "synthesizer": "s:cloud",
            },
            enabled_roles=("planner", "researcher", "critic", "synthesizer"),
            cwd="/p",
            extra_models_by_role={
                k: v for k, v in {
                    "researcher": researcher_extras,
                    "critic": critic_extras,
                }.items() if v
            },
        )
        return build_council_graph(deps)

    def test_xmax_with_critic_extras_includes_meta_critic(self):
        compiled = self._try_build(
            effort="xmax",
            researcher_extras=[],
            critic_extras=["a:cloud", "b:cloud"],
        )
        graph = compiled.get_graph()
        # meta_critic node present.
        assert "meta_critic" in graph.nodes

    def test_xmax_without_critic_extras_omits_meta_critic(self):
        compiled = self._try_build(
            effort="xmax",
            researcher_extras=["a:cloud"],
            critic_extras=[],
        )
        graph = compiled.get_graph()
        assert "meta_critic" not in graph.nodes

    def test_xmax_with_critic_extras_includes_research_barrier(self):
        # Phase 10a regression: the fan-out conditional edge from
        # researcher fires per-Send-invocation, not per-barrier.
        # Without the research_barrier pass-through node, N×M
        # researcher lanes would each trigger their own critic
        # fan-out, spawning N×M×C critic invocations instead of C.
        # The fix inserts a single-invocation pass-through node
        # whose unconditional edge from researcher barriers cleanly,
        # so the downstream conditional fan-out fires exactly once.
        compiled = self._try_build(
            effort="xmax",
            researcher_extras=["a:cloud", "b:cloud"],
            critic_extras=["c:cloud", "d:cloud"],
        )
        graph = compiled.get_graph()
        assert "research_barrier" in graph.nodes
        assert "meta_critic" in graph.nodes

    def test_xmax_critic_invocation_count_equals_critic_models(self):
        # End-to-end count invariant: regardless of how many
        # researcher lanes fan out, the critic fires exactly
        # 1 + len(critic_extras) times. Caught on the first live
        # xmax smoke (csl-...-2a8f) where 6 researcher lanes
        # produced 18 critic invocations instead of 3.
        pytest.importorskip("langgraph.graph")  # concrete leaf — ghost-dir proof
        from consultants.engine.graph import (
            GraphDeps, build_council_graph,
        )

        # Track how many times each ROLE NODE fires. The meta_critic
        # node shares the critic's ChatClient (by design — meta-
        # critic IS the primary critic model), so we can't just
        # count chat calls; we have to distinguish via the system
        # prompt's "ROLE:" header which build_critic_messages and
        # build_meta_critic_messages set differently.
        critic_calls = {"count": 0}
        meta_critic_calls = {"count": 0}
        researcher_calls = {"count": 0}

        class _CountingChat:
            def __init__(self, role: str):
                self.role = role

            def chat(self, payload):
                if self.role == "critic":
                    # Distinguish meta_critic from critic via system
                    # message — both use this same ChatClient.
                    sys_msg = next(
                        (m["content"] for m in payload.get("messages", [])
                         if m.get("role") == "system"),
                        "",
                    )
                    if "meta-critic" in sys_msg.lower() \
                            or "ROLE: meta-critic" in sys_msg:
                        meta_critic_calls["count"] += 1
                    else:
                        critic_calls["count"] += 1
                    text = "DECISION: ready\nLooks good."
                elif self.role == "researcher":
                    researcher_calls["count"] += 1
                    text = "research finding for this lane"
                elif self.role == "planner":
                    # Emit 3 plan items so fan-out triggers.
                    text = "1. step a\n2. step b\n3. step c"
                else:  # synthesizer
                    text = "final synthesizer answer"
                return {
                    "choices": [{
                        "message": {"content": text},
                        "finish_reason": "stop",
                    }],
                    "usage": {"prompt_tokens": 1,
                              "completion_tokens": 1},
                }

        deps = GraphDeps(
            chat_clients={
                r: _CountingChat(role=r) for r in
                ("planner", "researcher", "critic", "synthesizer")
            },
            models={
                "planner": "p:cloud",
                "researcher": "r-primary:cloud",
                "critic": "c-primary:cloud",
                "synthesizer": "s:cloud",
            },
            enabled_roles=("planner", "researcher", "critic", "synthesizer"),
            cwd="/p",
            tool_executor=lambda *a, **k: "",
            extra_models_by_role={
                # 2 researcher models × 3 plan items = 6 lanes.
                "researcher": ["r-extra-1:cloud", "r-extra-2:cloud"],
                # 3 critic models = 3 critic invocations expected.
                "critic": ["c-extra-1:cloud", "c-extra-2:cloud"],
            },
        )
        compiled = build_council_graph(deps)
        # Fire the graph against a minimal initial state — we don't
        # care about the answer, only the invocation counts.
        from consultants.engine import council
        initial = council.initial_state(
            question="q", cwd="/p",
            models=deps.models,
            topology="council", effort="xmax",
        )
        # Drain the stream.
        for _ in compiled.stream(initial):
            pass

        # Critic must fire exactly C times = 1 (primary) + 2 extras = 3.
        # The pre-fix code would have produced 6 × 3 = 18 (or higher
        # depending on how many researcher lanes the dispatcher
        # multiplied across).
        assert critic_calls["count"] == 3, (
            f"critic fired {critic_calls['count']} times, expected 3 "
            f"(1 primary + 2 extras). Researcher fan-out should not "
            f"multiply critic invocations — that's the Phase 10a fix."
        )
        # Meta-critic fires exactly once, regardless of how many
        # critics ran or how many re-routes happened.
        assert meta_critic_calls["count"] == 1, (
            f"meta_critic fired {meta_critic_calls['count']} times, "
            f"expected 1."
        )
        # Sanity: researcher actually fanned out. 3 plan items × 3
        # models (primary + 2 extras) = 9 invocations.
        assert researcher_calls["count"] >= 6, (
            f"researcher fired {researcher_calls['count']} times, "
            f"expected >= 6 (>= 3 plan items × 2 models). If this "
            f"fails, the test setup is wrong, not Phase 10a."
        )
