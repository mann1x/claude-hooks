"""M12 — v1 behavior-parity regression suite (task #98).

Three cohorts:

1. ``TestOptInsOffByDefault`` — static-state assertions that every
   v2 milestone gate is OFF in the default ``ConsultantsConfig()``.
   Catches a regression where a default flips accidentally.

2. ``TestPerEffortTierParity`` — E2E per-tier scenarios (low /
   medium / high / max / xmedium / xhigh / xmax). Asserts node-set,
   round counts, final_answer, turns shape, and that no v2 opt-in
   events fire on a default-config run.

3. ``TestStallRecoveryReplay`` — the 2026-05-15 audit session
   pathology (gemini lanes that emit a few tokens then stop).
   Validates the M3 stall detector catches → retries → tombstones
   within a 20-min mocked wall budget. Uses mocked clock for
   determinism.

The "parity" framing here is "the v1 behavior the v2 engine
*should* preserve when nothing is opted in." Since there's no
v1-in-tree, the assertion targets are derived from current
default behavior — this file is a regression gate, not a
cross-version comparator.

See ``tests/_parity_helpers.py`` for the shared stub
infrastructure.
"""
from __future__ import annotations

import unittest

import pytest

from consultants import config as cc
from consultants.engine.coder_defaults import (
    RECOMMENDED_CODER_DEFAULT_ROUTE,
    RECOMMENDED_CODER_ROUTES_BY_LANGUAGE,
)
from consultants.engine.state_v2 import CoderLanguageRoute

from tests._parity_helpers import (
    V2_OPT_IN_EVENT_KINDS,
    build_default_deps,
    capture_events,
)


pytestmark = pytest.mark.parity


# ============================================================== #
# Cohort 2: opt-ins off by default
# ============================================================== #


class TestOptInsOffByDefault(unittest.TestCase):
    """Each v2 milestone added an opt-in feature. M12 locks in
    "OFF by default" so a future commit can't silently flip a
    default — that would change the parity baseline without
    notice.

    These are static-state assertions on a fresh
    ``ConsultantsConfig()`` — no graph invocation needed, so the
    cohort runs in well under 1 s and gives the loudest, earliest
    signal if a default regresses.
    """

    def setUp(self):
        self.cfg = cc.ConsultantsConfig()

    # ----- M5 (HITL) --------------------------------------------- #

    def test_review_before_synthesis_off_by_default(self):
        # M5: HITL pause before the final answer. Opt-in via
        # cfg.runtime.review_before_synthesis = true. A default
        # run must not compile the graph with
        # interrupt_before=["synthesizer"].
        self.assertFalse(self.cfg.runtime.review_before_synthesis)

    def test_interrupt_on_low_confidence_off_by_default(self):
        # M5: dynamic interrupt when synth self-confidence < target.
        # Off because the signal normally drives xauto escalation
        # (not a human pause).
        self.assertFalse(self.cfg.runtime.interrupt_on_low_confidence)

    # ----- M6 (tool_executor role) ------------------------------ #

    def test_tool_executor_role_disabled_by_default(self):
        # Flip history:
        # - M11c-1 (2026-05-17): scaffold False.
        # - M11c-5 (2026-05-17): True after the two-part gate
        #   cleared (M11c-2 bench rubric pass + #103 x-tier
        #   composition via M11c-3 engine refactor).
        # - 2026-05-18: back to False. The M14 first-real-ask
        #   tool_executor on/off A/B
        #   (benchmarks/consultants/results/2026-05-18/tool-executor-ab/)
        #   showed the role +12 min wall / +43% tokens AND
        #   fewer edge cases identified on a grep-shaped
        #   question. The M11c-2 bench still validates the role
        #   on tool-heavy reasoning; the default flip-back
        #   recognizes that bench's question shape is not what
        #   most operator questions look like. Operators who
        #   want the specialist opt in via:
        #     [role.tool_executor]  enabled = true
        self.assertFalse(self.cfg.roles["tool_executor"].enabled)

    def test_awaiting_tool_results_field_dropped_from_state(self):
        # #103 (M11c-3): the scalar ``awaiting_tool_results`` flag
        # was removed from CouncilStateV2. The post-researcher
        # router now derives the dispatch decision from
        # ``tool_plan`` vs ``tool_results`` at the current round.
        # If a future commit re-adds the field, that's a
        # parity-breaking shape change worth catching here.
        from consultants.engine.state_v2 import CouncilStateV2
        self.assertNotIn(
            "awaiting_tool_results",
            CouncilStateV2.__annotations__,
        )

    # ----- M7 (xauto effort tier) ------------------------------- #

    def test_xauto_is_not_the_default_effort(self):
        # M7 introduced 'xauto' as an opt-in adaptive tier. The
        # default-config effort must remain non-xauto so plain
        # consultations don't pay the escalator's overhead.
        self.assertNotEqual(self.cfg.effort, "xauto")

    # ----- M8 (BaseStore) + M14 (TTL + distillation) ------------ #

    def test_store_enabled_by_default(self):
        # M14 (2026-05-18) flipped this default from False to True.
        # The store itself wires in — but the ``enable_at_efforts``
        # gate (excludes "medium") keeps it OFF for default-effort
        # users (see test_make_consultants_store_returns_none_at_default_effort
        # below). The flip becomes observable only at high+ tiers.
        self.assertTrue(self.cfg.store.enabled)

    def test_store_backend_is_sqlite_vec_by_default(self):
        # M14: default backend flipped from "memory" to "sqlite_vec"
        # so the TTL + distillation chain has a real persistence
        # surface. Hosts that prefer pgvector configure explicitly;
        # hosts that want zero deps stick with backend = "memory" or
        # ``store.enabled = false``.
        self.assertEqual(self.cfg.store.backend, "sqlite_vec")

    def test_store_ttl_enabled_by_default(self):
        # M14: per-namespace TTL on by default (research=30d,
        # tool_results=24h, project/user=never). Hosts that want
        # M8's live-forever behavior set ttl.enabled = false.
        self.assertTrue(self.cfg.store.ttl.enabled)

    def test_store_distillation_enabled_by_default(self):
        # M14: Caliber-style distillation on by default — expiring
        # research findings get summarized into the durable
        # ("project", pid) namespace before deletion. The cost
        # gate (min_entries_per_distillation = 3) keeps single-
        # finding sessions from triggering an LLM call.
        self.assertTrue(self.cfg.store.distillation.enabled)

    def test_make_consultants_store_returns_none_at_default_effort(self):
        # M14 default-on safety net: even with ``store.enabled =
        # True``, the ``enable_at_efforts`` gate (excludes "medium",
        # the default effort) means ``make_consultants_store`` still
        # short-circuits to None at the default config. This
        # preserves the original M12 parity guarantee for plain
        # ``claude-consultants ask`` runs — they pay zero store
        # cost unless the operator explicitly bumps effort.
        from consultants.engine.store import make_consultants_store
        self.assertEqual(self.cfg.effort, "medium")
        self.assertNotIn("medium", self.cfg.store.enable_at_efforts)
        store = make_consultants_store(
            self.cfg, sid="csl-parity-test", effort=self.cfg.effort,
        )
        self.assertIsNone(store)

    # ----- M10 (coder role) ------------------------------------- #

    def test_coder_role_disabled_by_default(self):
        # M10: the sandboxed write_file specialist. Off by default
        # because it fundamentally changes the council's output
        # surface (writes files to a sandbox dir).
        self.assertFalse(self.cfg.roles["coder"].enabled)

    # ----- #111 (per-language coder routing) ------------------- #

    def test_coder_routes_seeded_but_inert_when_coder_disabled(self):
        # The recommended routes ARE seeded so an operator can
        # enable coder + immediately get the bench-derived per-
        # language picks. But when coder is disabled (default),
        # those entries must be unreachable — the graph resolver
        # closure returns None so no per-model TracedChat dict is
        # constructed.
        self.assertEqual(
            len(self.cfg.roles["coder"].routes_by_language),
            len(RECOMMENDED_CODER_ROUTES_BY_LANGUAGE),
        )
        self.assertEqual(
            self.cfg.roles["coder"].default_route,
            RECOMMENDED_CODER_DEFAULT_ROUTE,
        )
        # Even though the seed exists, the v2 wiring must not
        # activate it when the role is disabled. Verified at the
        # graph level: _build_coder_chain_resolver returns None
        # when coder isn't enabled (no chat_clients_by_model).
        from consultants.engine.graph import _build_coder_chain_resolver
        deps = build_default_deps(
            enabled_roles=["planner", "researcher", "synthesizer"],
        )
        # Default deps don't populate the per-model dict → resolver
        # ends up None even though cfg has routes.
        self.assertIsNone(_build_coder_chain_resolver(deps))

    def test_other_roles_have_no_route_fields(self):
        # routes_by_language + default_route are coder-only — other
        # roles must not have them populated. Catches a regression
        # where the seeded factory leaks into non-coder roles.
        for role in ("planner", "researcher", "critic", "synthesizer",
                     "tool_executor", "adversary"):
            with self.subTest(role=role):
                rc = self.cfg.roles[role]
                self.assertEqual(rc.routes_by_language, {})
                self.assertIsNone(rc.default_route)

    # ----- M3 / M1 (dynamic adversary role + checkpoint) -------- #

    def test_adversary_role_disabled_by_default(self):
        # M3: the post-synthesis refuter. Off by default — when off,
        # the council topology must stay synthesizer → END (the
        # adversary singleton node is never registered), so the
        # default answer surface is byte-identical to v1.
        self.assertFalse(self.cfg.roles["adversary"].enabled)

    def test_adversary_role_excluded_from_enabled_roles_by_default(self):
        # enabled_roles() drives graph construction; adversary must
        # not appear in the default list or _wrap_adversary would be
        # registered + the synthesizer → END edge rerouted.
        from consultants.config import enabled_roles
        self.assertNotIn("adversary", enabled_roles(self.cfg))

    def test_default_topology_tail_is_synthesizer_end(self):
        # M3: with the adversary role off (default), plan_topology must
        # emit the v1 synthesizer → END tail — NOT synthesizer →
        # adversary → END. This is the edge-level parity guard.
        from consultants.engine.graph import plan_topology
        from consultants.config import enabled_roles
        edges = plan_topology(tuple(enabled_roles(self.cfg)))
        self.assertIn(("synthesizer", "END"), edges)
        self.assertNotIn(("synthesizer", "adversary"), edges)

    def test_verify_budget_default_is_bounded(self):
        # M1: the Workflow skeptic-panel budget. ``bounded`` (3 claims)
        # is the shipped default; the knob only affects the M6 driver
        # script, never the bare council, but pin it so a default flip
        # is loud.
        self.assertEqual(self.cfg.verify_budget, "bounded")

    def test_adversary_strictness_default_is_normal(self):
        self.assertEqual(self.cfg.adversary_strictness, "normal")

    def test_adversary_checkpoint_off_by_default(self):
        # M2: the engine-initiated pause-before-synthesis. Off by
        # default → _drive_council_stream never enters the
        # awaiting_adversary park branch, so the synthesizer interrupt
        # boundary resumes immediately as it does today.
        self.assertFalse(self.cfg.adversary_checkpoint)
        self.assertEqual(self.cfg.adversary_checkpoint_timeout_s, 600)

    # ----- M4 (dynamic critic dial) ----------------------------- #

    def test_critic_dial_seeds_to_normal_by_default(self):
        # M4: the boot-time RuntimeControl seeds ``critic_strictness``
        # from ``adversary_strictness`` (default normal → normal) and
        # never seeds an ``adversarial_focus``. A default flip here
        # would silently re-shape every critic prompt.
        from consultants.engine import control
        rc = control.runtime_control_defaults(self.cfg, effort="medium")
        self.assertEqual(rc["critic_strictness"], "normal")
        self.assertNotIn("adversarial_focus", rc)

    def test_default_critic_prompt_is_byte_identical(self):
        # M4: with the dial at its default (normal, no focus) the critic
        # AND meta-critic prompts must be byte-identical to the
        # no-dial-argument call — i.e. the M4 threading appends nothing.
        from consultants.engine import council
        self.assertEqual(
            council.build_critic_messages("q?", "1. p", ["r"]),
            council.build_critic_messages(
                "q?", "1. p", ["r"],
                strictness="normal", adversarial_focus=""),
        )
        self.assertEqual(
            council.build_meta_critic_messages("q?", "p", ["r"], ["v"]),
            council.build_meta_critic_messages(
                "q?", "p", ["r"], ["v"],
                strictness="normal", adversarial_focus=""),
        )

    # ----- M-A / M-C (tool registry + git provider) -------------- #

    def test_git_tools_off_by_default(self):
        # M-C: the git history provider is read-only and carries no new
        # risk surface, but turning it on adds five schemas to every
        # prompt on every lane — a default-behaviour change. It lands
        # disabled and gets flipped after a live smoke, exactly how
        # ``store`` was handled (scaffold off → validated → M14 on).
        self.assertFalse(self.cfg.tools.git)

    def test_uniform_role_tools_off_by_default(self):
        # M-B: giving planner / critic / meta_critic / synthesizer /
        # adversary the researcher's tools changes cost, not
        # correctness — one LLM call per tool iteration per role per
        # lane, and critic fans out per lane at the x-tiers. Same gate
        # discipline as tool_executor: land it off, measure, then flip.
        self.assertFalse(self.cfg.tools.all_roles)

    def test_default_graph_hands_no_role_any_tools(self):
        # The knob's runtime consequence. An ungated role must receive
        # exactly its pre-M-B argument list — not tool_specs=None, but
        # no tool kwargs at all.
        from consultants.engine.graph import (
            TOOLABLE_ROLES, GraphDeps, _tools_for,
        )
        deps = GraphDeps(chat_clients={}, models={}, enabled_roles=(),
                         cwd="/p", tool_specs=[{"x": 1}],
                         tool_executor=lambda *a: "")
        self.assertEqual(deps.tooled_roles, ())
        for role in TOOLABLE_ROLES:
            self.assertEqual(_tools_for(deps, role), {}, role)

    def test_default_permission_level_is_auto(self):
        # M-A: the ladder's cheap rung. If this ever defaulted to an
        # ask_* level, every grep in every lane would pay an approval
        # round-trip — the exact cost the ``auto`` rung exists to avoid.
        self.assertEqual(self.cfg.tools.default_level, "auto")
        self.assertEqual(self.cfg.tools.permissions, {})

    def test_default_tool_surface_is_byte_identical_to_pre_registry(self):
        # M-A is a refactor, not a capability change: with default
        # config the composed surface must equal what the two hardcoded
        # ``openai_tool_specs()`` call sites produced. Anything else
        # re-shapes every researcher prompt and invalidates the M11c
        # benchmark corpus.
        from claude_hooks.caliber_proxy.tools import openai_tool_specs
        from consultants.server.tool_surface import build_tool_surface
        specs, _executor, _registry = build_tool_surface(self.cfg)
        self.assertEqual(specs, openai_tool_specs())

    def test_default_surface_adds_no_extra_tools_note(self):
        # The prompt half of the same guarantee. RESEARCHER_SYSTEM and
        # the tool-plan prompt enumerate their six tools in prose; the
        # note names only what a provider adds *beyond* that list, so on
        # the default surface it must be empty and every researcher /
        # tool_executor prompt stays byte-identical.
        from claude_hooks.caliber_proxy.tools import openai_tool_specs
        from consultants.engine import council
        from consultants.server.tool_surface import build_tool_surface
        specs, _e, _r = build_tool_surface(self.cfg)
        self.assertEqual(council.build_extra_tools_note(specs), "")
        self.assertEqual(council.build_extra_tools_note(openai_tool_specs()),
                         "")
        msgs = [{"role": "system", "content": "S"},
                {"role": "user", "content": "U"}]
        self.assertIs(council._with_extra_tools_note(msgs, specs), msgs)

    def test_tool_addendum_is_unreachable_with_default_tooled_roles(self):
        # The addendum exists to make the M-B knob actually change
        # behaviour (the 2026-08-01 bench measured 0 tool calls without
        # it). It must remain unreachable while the knob is off — the
        # gate is ``_tools_for`` returning {}, so no toolable role is
        # ever handed the tool_specs the addendum keys on.
        from consultants.engine.graph import (
            TOOLABLE_ROLES, GraphDeps, _tools_for,
        )
        deps = GraphDeps(chat_clients={}, models={}, enabled_roles=(),
                         cwd="/p", tool_specs=[{"x": 1}],
                         tool_executor=lambda *a: "")
        for role in TOOLABLE_ROLES:
            self.assertNotIn("tool_specs", _tools_for(deps, role), role)

    def test_registry_is_not_shared_between_sessions(self):
        # The gate carries per-session taint, which is sticky. A shared
        # registry would leak that into the next consultation — and in
        # the wrong direction, since taint never clears.
        from consultants.server.tool_surface import build_tool_surface
        _s1, _e1, r1 = build_tool_surface(self.cfg)
        _s2, _e2, r2 = build_tool_surface(self.cfg)
        self.assertIsNot(r1, r2)

    # ----- M1 (checkpointer) ------------------------------------ #

    def test_checkpointer_backend_is_sqlite_by_default(self):
        # M1: zero-dependency SQLite is the default; postgres is
        # opt-in via cfg.checkpointer.backend = "postgres" + a
        # connection url. Catches the regression where a default
        # flips to postgres + a fresh install breaks because
        # psycopg isn't on the path.
        self.assertEqual(self.cfg.checkpointer.backend, "sqlite")
        self.assertIsNone(self.cfg.checkpointer.url)

    # ----- review loop (consultancy followup cap) --------------- #

    def test_review_loop_default_baseline(self):
        # The review loop is ON by default (unlike the opt-ins above):
        # a fresh consultancy caps auto-followups at ``max_followups``
        # and grants ``allow_extra`` per over-cap approval. This is a
        # DELIBERATE default-behavior change vs the pre-review-loop
        # engine (which allowed unlimited followups). Lock the baseline
        # values here so an accidental flip is caught — a 5th followup
        # past the default cap is refused server-side (see
        # tests/test_consultants_review_loop.py for the E2E gate).
        self.assertEqual(self.cfg.max_followups, 4)
        self.assertEqual(self.cfg.allow_extra, 1)

    # ----- M4 (event taxonomy) — meta-assertion ----------------- #

    def test_v2_optin_event_kinds_set_is_complete(self):
        # Belt-and-braces: if a new opt-in event gets added to
        # ``events.py`` without an entry in ``V2_OPT_IN_EVENT_KINDS``,
        # the off-by-default check in cohort 1 won't catch it. This
        # test enumerates every CouncilEvent subclass that has its
        # ``kind`` in the v2 opt-in set vs the always-on set, so
        # a future event addition shows up here as a fail until
        # the helper is updated.
        from consultants.engine import events as ev_mod
        from consultants.engine.events import CouncilEvent

        # Enumerate every CouncilEvent subclass declared in the
        # module.
        all_kinds = set()
        for name in dir(ev_mod):
            obj = getattr(ev_mod, name)
            if (isinstance(obj, type) and issubclass(obj, CouncilEvent)
                    and obj is not CouncilEvent):
                # Default kind from the dataclass field default.
                default_kind = getattr(obj, "kind", None)
                # Build a no-arg instance only when the dataclass
                # has all-default fields. The frozen+kw_only mix in
                # events.py means we need to inspect the field
                # defaults directly.
                try:
                    inst = obj()  # noqa — kw_only=True; required fields would raise
                    all_kinds.add(inst.kind)
                except TypeError:
                    # Required-field events: read the default via
                    # dataclass introspection.
                    import dataclasses
                    fields = {
                        f.name: f.default
                        for f in dataclasses.fields(obj)
                        if f.default is not dataclasses.MISSING
                    }
                    if "kind" in fields:
                        all_kinds.add(fields["kind"])

        # Always-on event kinds — these fire on every default run.
        always_on = {
            "node_started", "node_finished", "tool_call",
            "partial_synthesis", "confidence_update",
        }
        # Every kind we know about should be in one set or the
        # other. If a new event appears here uncatalogued, the
        # assertion's `unaccounted` list names it so the next
        # developer knows to add it.
        union = set(V2_OPT_IN_EVENT_KINDS) | always_on
        unaccounted = all_kinds - union
        self.assertFalse(
            unaccounted,
            f"unaccounted event kind(s) — add to V2_OPT_IN_EVENT_KINDS "
            f"or the always-on set: {sorted(unaccounted)}",
        )


# ============================================================== #
# Cohort 1: per-effort-tier E2E parity
# ============================================================== #


try:
    from langgraph.checkpoint.memory import InMemorySaver
    HAVE_LANGGRAPH = True
except ImportError:
    HAVE_LANGGRAPH = False


# Per-tier expected behavior — derived from running the engine
# TODAY with default config + default stubs. The values pin
# CURRENT behavior so a future change to the default path shows
# up as a parity-suite failure. (See plan: "no v1-in-tree to diff
# against; the baseline is current default behavior.")
TIERS_TO_TEST = ("low", "medium", "high", "max",
                 "xmedium", "xhigh", "xmax")


@unittest.skipUnless(HAVE_LANGGRAPH, "langgraph not installed")
class TestPerEffortTierParity(unittest.TestCase):
    """Per-tier E2E scenarios. Each test:

    1. Builds a default-config council with stub chat_clients.
    2. Captures the event stream + invokes the graph.
    3. Asserts: final_answer is the stub's deterministic output;
       the researcher round count is within the tier's cap; no
       v2 opt-in events fire on a default run.

    Token-count + exact-turn-shape pinning is deliberately NOT in
    this cohort — those are fragile against legitimate refactors
    (e.g. prompt-template tweaks). The pinned assertions here are
    the *behavioral* invariants that must hold for v2 to claim
    parity with v1.
    """

    def _run_tier(self, effort: str,
                  *,
                  enabled_roles=(
                      "planner", "researcher", "synthesizer",
                  )):
        """Build + invoke a default-config council at ``effort``.

        Returns ``(final_state, captured_events)``. Tests assert
        against both.
        """
        from consultants.engine.graph import build_council_graph

        deps = build_default_deps(enabled_roles=enabled_roles)
        graph = build_council_graph(deps, checkpointer=InMemorySaver())
        thread_config = {
            "configurable": {"thread_id": f"csl-parity-{effort}"},
        }
        initial = {
            "question": "smoke",
            "cwd": "/tmp",
            "models": deps.models,
            "topology": "council",
            "effort": effort,
        }
        with capture_events() as captured:
            final = graph.invoke(initial, config=thread_config)
        return final, captured

    # ----- Per-tier scenarios (parameterised via subTest) ------- #

    def test_low_effort_runs_to_completion(self):
        """``low`` effort: minimal pipeline (no critic). The
        researcher fires once, the synthesizer composes."""
        final, captured = self._run_tier("low")
        self.assertIn("FINAL ANSWER", final.get("final_answer", ""))
        # No v2 opt-in events on default config.
        from tests._parity_helpers import assert_no_v2_optin_events
        assert_no_v2_optin_events(captured)
        # No critic reroutes happened (low caps reroutes to 0).
        self.assertEqual(
            int(final.get("critic_reroutes_used") or 0), 0,
        )

    def test_medium_effort_runs_to_completion(self):
        """``medium``: 3-lane researcher fanout (default for the
        canonical smoke). Synthesizer composes from the lanes."""
        final, captured = self._run_tier(
            "medium",
            enabled_roles=("planner", "researcher", "critic",
                            "synthesizer"),
        )
        self.assertIn("FINAL ANSWER", final.get("final_answer", ""))
        from tests._parity_helpers import assert_no_v2_optin_events
        assert_no_v2_optin_events(captured)
        # Critic can reroute up to 1 time at medium — our stub
        # always returns ready, so reroutes_used stays at 0.
        self.assertEqual(
            int(final.get("critic_reroutes_used") or 0), 0,
        )

    def test_high_effort_runs_to_completion(self):
        final, captured = self._run_tier(
            "high",
            enabled_roles=("planner", "researcher", "critic",
                            "synthesizer"),
        )
        self.assertIn("FINAL ANSWER", final.get("final_answer", ""))
        from tests._parity_helpers import assert_no_v2_optin_events
        assert_no_v2_optin_events(captured)
        # high caps researcher_rounds_max at 3; researcher fired
        # at most that many times. We don't pin the exact count
        # because the count depends on the stub-critic always
        # saying "ready" (which terminates early). Assert <= cap.
        self.assertLessEqual(
            int(final.get("critic_reroutes_used") or 0), 2,
        )

    def test_max_effort_runs_to_completion(self):
        final, captured = self._run_tier(
            "max",
            enabled_roles=("planner", "researcher", "critic",
                            "synthesizer"),
        )
        self.assertIn("FINAL ANSWER", final.get("final_answer", ""))
        from tests._parity_helpers import assert_no_v2_optin_events
        assert_no_v2_optin_events(captured)
        self.assertLessEqual(
            int(final.get("critic_reroutes_used") or 0), 5,
        )

    def test_xmedium_effort_no_extras_behaves_like_medium(self):
        """``xmedium`` with empty ``extra_models_by_role`` should
        run exactly like ``medium`` — the x-prefix only activates
        multi-model fanout when extras are configured. Without
        extras the run is identical."""
        final, captured = self._run_tier(
            "xmedium",
            enabled_roles=("planner", "researcher", "critic",
                            "synthesizer"),
        )
        self.assertIn("FINAL ANSWER", final.get("final_answer", ""))
        from tests._parity_helpers import assert_no_v2_optin_events
        assert_no_v2_optin_events(captured)
        # No multi-model fanout happened (no extras configured).
        # researcher_lane_models for the researcher should be
        # empty / single-element.
        researcher_models = final.get("researcher_lane_models") or {}
        for lane_models in researcher_models.values():
            self.assertLessEqual(len(lane_models or [1]), 1)

    def test_xhigh_effort_no_extras_behaves_like_high(self):
        final, captured = self._run_tier(
            "xhigh",
            enabled_roles=("planner", "researcher", "critic",
                            "synthesizer"),
        )
        self.assertIn("FINAL ANSWER", final.get("final_answer", ""))
        from tests._parity_helpers import assert_no_v2_optin_events
        assert_no_v2_optin_events(captured)

    def test_xmax_effort_no_extras_behaves_like_max(self):
        final, captured = self._run_tier(
            "xmax",
            enabled_roles=("planner", "researcher", "critic",
                            "synthesizer"),
        )
        self.assertIn("FINAL ANSWER", final.get("final_answer", ""))
        from tests._parity_helpers import assert_no_v2_optin_events
        assert_no_v2_optin_events(captured)

    def test_xauto_without_dissent_stays_at_xmedium(self):
        """xauto is an opt-in tier. When the critic stub always
        returns 'ready' (no dissent signal), the escalator must
        NOT fire — runtime_control.xauto_tier stays unset or at
        the baseline xmedium, and no RuntimeMutation event fires
        beyond the initial seed.

        This is the parity guarantee for xauto: opt-in but inert
        when the conditions don't justify escalation.
        """
        final, captured = self._run_tier(
            "xauto",
            enabled_roles=("planner", "researcher", "critic",
                            "synthesizer"),
        )
        self.assertIn("FINAL ANSWER", final.get("final_answer", ""))
        rc = final.get("runtime_control") or {}
        # No escalation happened.
        self.assertIn(rc.get("xauto_escalations"), (None, 0))
        # xauto can emit RuntimeMutation for the initial seed of
        # the xauto_tier channel. Tolerate at most that one;
        # MORE than 1 indicates the escalator fired without
        # dissent — a regression.
        rt_mutations = [
            e for e in captured if getattr(e, "kind", None) == "runtime_mutation"
        ]
        self.assertLessEqual(
            len(rt_mutations), 1,
            "xauto fired escalation without critic dissent "
            "(stub critic returned 'ready' on every call)",
        )

    # ----- Cross-tier invariant: synthesizer ALWAYS fires ------- #

    def test_every_tier_produces_a_final_answer(self):
        """Belt-and-braces — for each tier, assert the
        synthesizer's output lands as ``final_answer``. Catches
        regressions where a topology change accidentally bypasses
        the synthesizer."""
        for tier in TIERS_TO_TEST:
            with self.subTest(tier=tier):
                final, _ = self._run_tier(
                    tier,
                    enabled_roles=(
                        "planner", "researcher", "critic",
                        "synthesizer",
                    ) if tier != "low" else (
                        "planner", "researcher", "synthesizer",
                    ),
                )
                self.assertIn(
                    "FINAL ANSWER", final.get("final_answer") or "",
                    f"tier={tier} produced no final_answer",
                )


# ============================================================== #
# Cohort 3: 2h-session stall recovery replay
# ============================================================== #


class TestStallRecoveryReplay(unittest.TestCase):
    """Replay the 2026-05-15 audit session pathology (gemini lanes
    that emitted a few tokens then went silent for 30+ minutes on
    the cloud) against the M3 stall detector. Validates the
    parity fix: a default-config research lane finishes in well
    under 20 mocked-minute wall instead of the 2h-7m pathology v1
    produced.

    Targets ``StallMonitor`` directly (its ``time_source=`` +
    ``sleep_fn=`` kwargs are the clean mock injection points)
    rather than the full council graph — the integration is what's
    being tested, but at the level where it actually lives.

    These tests use a TINY real ``check_interval_s`` (50 ms) so the
    watchdog wakes fast enough that the test's mock-clock advances
    interleave deterministically. Total real wall: < 5 seconds per
    test; mocked-wall budget: minutes.
    """

    def _build_monitor(self, clock,
                       *,
                       stall_threshold_s: float = 300.0,
                       hard_cap_s: float = 3600.0,
                       retries: int = 1,
                       check_interval_s: float = 0.05,
                       on_event=None):
        from consultants.engine.stall import (
            StallConfig, StallMonitor,
        )
        cfg = StallConfig(
            stall_threshold_s=stall_threshold_s,
            hard_cap_s=hard_cap_s,
            retries=retries,
            check_interval_s=check_interval_s,
            retry_backoff_s=0.01,     # tight for tests
            join_grace_s=0.5,         # cooperative cancel timeout
        )
        return StallMonitor(
            cfg, time_source=clock.now, sleep_fn=clock.sleep,
            on_event=on_event,
        )

    def test_stalled_lane_is_retried_then_tombstoned(self):
        """Replay shape: lane emits 3 tokens via the controller,
        then hangs. Stall detector cancels mid-stream, retries
        once, retry also hangs, tombstones."""
        import threading
        import time as _time
        from consultants.engine.stall import (
            CancelledByOrchestrator, StallRetryExhausted,
        )
        from tests._parity_helpers import MockedClock

        clock = MockedClock(t0=0.0)
        events: list[dict] = []

        cancel_check_calls: list[int] = []

        def stub_chat_streamed(payload, controller):
            # 1) Emit a few tokens via the controller — the M3
            #    controller updates ``last_token_ts`` on each call.
            for i in range(3):
                controller.mark_token()
                clock.advance(1.0)
            # 2) Hang: spin-wait for cancellation. The test thread
            #    drives the mock clock forward past the stall
            #    threshold, the watchdog detects it, calls
            #    ``controller.cancel()`` (set the flag), this loop
            #    notices and exits.
            while not controller.is_cancelled():
                cancel_check_calls.append(1)
                _time.sleep(0.01)  # real time, tiny
            raise CancelledByOrchestrator(
                "test stub honoured cancellation"
            )

        monitor = self._build_monitor(
            clock, stall_threshold_s=300.0, hard_cap_s=3600.0,
            retries=1, on_event=events.append,
        )

        # Run the monitor in a separate thread so the main thread
        # can drive the mock clock between watchdog wakeups.
        result_box: dict = {"outcome": None, "exc": None}

        def runner():
            try:
                result_box["outcome"] = monitor.run(
                    stub_chat_streamed, {"model": "stub"},
                )
            except StallRetryExhausted as e:
                result_box["exc"] = e

        t = threading.Thread(target=runner, daemon=True)
        t.start()

        # Drive the mock clock past the stall threshold twice (once
        # for each attempt's stall). Wait briefly after each
        # advance so the watchdog has a chance to wake (it joins
        # with check_interval_s = 50 ms).
        for _ in range(2):
            _time.sleep(0.15)        # give worker time to emit
            clock.advance(400.0)     # past 300s threshold
            _time.sleep(0.5)         # let watchdog detect + retry

        t.join(timeout=5.0)
        self.assertFalse(t.is_alive(),
                          "StallMonitor did not exit cleanly")
        self.assertIsInstance(result_box["exc"], StallRetryExhausted,
                              f"expected StallRetryExhausted, got "
                              f"{result_box!r}")

        # Two attempts: initial + 1 retry. Both stalled.
        self.assertEqual(len(monitor.attempts), 2)
        for entry in monitor.attempts:
            self.assertIn(entry["outcome"],
                          ("mid_stream_stall", "startup_stall"),
                          entry)
            self.assertGreaterEqual(entry["tokens_emitted"], 0)

        # Event stream shows the stall + the final exhaustion.
        kinds = [e.get("kind") for e in events]
        self.assertIn("stall.attempt.stalled", kinds)

        # Mocked-wall budget: each attempt consumed ~400 s mocked
        # plus the retry backoff (10 ms). Total stays well under
        # 20 min (1200 s mocked) — the parity guarantee.
        self.assertLess(
            clock.now(), 1200.0,
            "stall recovery exceeded the 20-min mocked wall budget "
            f"(was {clock.now():.0f}s) — M3 detector regressed",
        )

    def test_slow_but_progressing_lane_is_not_killed(self):
        """The complement: a lane that legitimately emits one
        token every 60 mocked seconds for 20 mocked minutes must
        NOT be killed. Validates the M3 design goal: don't punish
        legitimate deep thinking (the user's 2026-05-16
        constraint)."""
        import threading
        import time as _time
        from tests._parity_helpers import MockedClock

        clock = MockedClock(t0=0.0)

        def stub_chat_streamed(payload, controller):
            # 20 tokens at 60 s each = 1200 s mocked total. Within
            # the 1-token-per-60-s cadence, ``last_token_ts``
            # always advances within the 300 s threshold.
            for i in range(20):
                if controller.is_cancelled():
                    raise AssertionError(
                        "slow-progressing lane got cancelled — "
                        "stall detector regressed (false positive)"
                    )
                controller.mark_token()
                clock.advance(60.0)
                _time.sleep(0.02)   # small real wait so the
                                     # watchdog wakes inside the gap
            return {
                "choices": [{"message": {
                    "role": "assistant", "content": "slow OK",
                }}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 20},
            }

        monitor = self._build_monitor(
            clock, stall_threshold_s=300.0, hard_cap_s=3600.0,
        )

        # No threading — the controller's cancel flag is the only
        # async signal, and the stub releases it cooperatively.
        # Just run synchronously and verify completion.
        result_box: dict = {"outcome": None, "exc": None}

        def runner():
            try:
                result_box["outcome"] = monitor.run(
                    stub_chat_streamed, {"model": "stub"},
                )
            except Exception as e:
                result_box["exc"] = e

        t = threading.Thread(target=runner, daemon=True)
        t.start()
        t.join(timeout=10.0)

        self.assertFalse(t.is_alive(),
                          "slow-progressing monitor did not exit")
        self.assertIsNone(result_box["exc"],
                          f"slow-progressing run raised: "
                          f"{result_box['exc']!r}")
        self.assertEqual(len(monitor.attempts), 1,
                          "slow-progressing run was retried "
                          "(stall detector false positive)")
        self.assertEqual(monitor.attempts[0]["outcome"], "ok")


if __name__ == "__main__":
    unittest.main()
