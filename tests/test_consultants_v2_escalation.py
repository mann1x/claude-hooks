"""Tests for :mod:`consultants.engine.escalation` — the xauto
adaptive-effort decision engine.

Pure-Python; no LangGraph required. Covers:

- ``is_xauto_run`` short-circuits non-xauto efforts.
- ``current_tier`` reads runtime_control + defaults to xmedium.
- Forward-only transitions: xmedium → xhigh → xmax → ceiling.
- Each signal triggers escalation as documented (critic_dissent /
  gap_named / low_confidence / time_pressure).
- Pre-condition guards: min round, ceiling, time-pressure
  suppression.
- Topology delta computation only includes changed fields.
- Event payload shape contract.
- Config integration: ``xauto`` is in EFFORT_BUDGETS,
  ``extras_active("xauto") == True``, ``base_effort("xauto") ==
  "medium"``.
"""

from __future__ import annotations

import time
import unittest

import consultants.config as cc
from consultants.engine.escalation import (
    ALLOWED_TRANSITIONS,
    EscalationDecision,
    TIER_TOPOLOGIES,
    apply_escalation,
    current_tier,
    is_xauto_run,
    next_escalation,
    runtime_mutation_event_data,
)


# ============================================================== #
# Config integration
# ============================================================== #

class TestConfigSurface(unittest.TestCase):
    """``xauto`` joins the tier registry without breaking helpers."""

    def test_xauto_in_effort_budgets(self):
        self.assertIn("xauto", cc.EFFORT_BUDGETS)
        # xauto budget matches xmax — worst-case escalation needs
        # follow-up budget compatible with the final tier.
        self.assertEqual(
            cc.EFFORT_BUDGETS["xauto"],
            cc.EFFORT_BUDGETS["xmax"],
        )

    def test_extras_active_for_xauto(self):
        # xauto is an x-tier by definition (starts at xmedium).
        self.assertTrue(cc.extras_active("xauto"))

    def test_base_effort_for_xauto(self):
        # xauto resolves to the medium base — that's the starting
        # caps the escalator grows from.
        self.assertEqual(cc.base_effort("xauto"), "medium")

    def test_non_xauto_unaffected(self):
        # Regression guard: existing tiers keep their semantics.
        self.assertEqual(cc.base_effort("xhigh"), "high")
        self.assertEqual(cc.base_effort("medium"), "medium")
        self.assertTrue(cc.extras_active("xhigh"))
        self.assertFalse(cc.extras_active("medium"))


# ============================================================== #
# is_xauto_run + current_tier
# ============================================================== #

class TestIsXautoRun(unittest.TestCase):

    def test_true_on_xauto(self):
        self.assertTrue(is_xauto_run({"effort": "xauto"}))

    def test_false_on_other_efforts(self):
        for e in ("medium", "high", "xmedium", "xhigh", "xmax", ""):
            self.assertFalse(is_xauto_run({"effort": e}))

    def test_false_when_effort_missing(self):
        self.assertFalse(is_xauto_run({}))


class TestCurrentTier(unittest.TestCase):

    def test_defaults_to_xmedium(self):
        self.assertEqual(current_tier({}), "xmedium")
        self.assertEqual(
            current_tier({"runtime_control": {}}), "xmedium",
        )

    def test_reads_runtime_control(self):
        for t in ("xmedium", "xhigh", "xmax", "ceiling"):
            state = {"runtime_control": {"xauto_tier": t}}
            self.assertEqual(current_tier(state), t)

    def test_unknown_tier_falls_back_to_xmedium(self):
        # Defensive: if a corrupted checkpoint surfaces an unknown
        # value, we don't crash — fall back to xmedium and log.
        state = {"runtime_control": {"xauto_tier": "bogus"}}
        self.assertEqual(current_tier(state), "xmedium")


# ============================================================== #
# Forward transitions only
# ============================================================== #

class TestAllowedTransitions(unittest.TestCase):

    def test_forward_only(self):
        self.assertEqual(ALLOWED_TRANSITIONS["xmedium"], "xhigh")
        self.assertEqual(ALLOWED_TRANSITIONS["xhigh"], "xmax")
        self.assertEqual(ALLOWED_TRANSITIONS["xmax"], "ceiling")

    def test_no_backward_edge(self):
        # The escalator never reverses — once you've grown a
        # consultation, you can't shrink it.
        self.assertNotIn("ceiling", ALLOWED_TRANSITIONS)


# ============================================================== #
# next_escalation — signal-driven transitions
# ============================================================== #

class TestNextEscalationGuards(unittest.TestCase):

    def test_no_escalation_on_non_xauto(self):
        # Critic dissent on xmedium should NOT escalate — only
        # xauto runs adapt.
        state = {
            "effort": "xmedium",
            "research_rounds_used": 1,
            "critic_decision": "needs_more_research",
        }
        self.assertIsNone(next_escalation(state))

    def test_no_escalation_before_first_round(self):
        # Premature: at least one round must complete before we
        # judge the council needs more.
        state = {
            "effort": "xauto",
            "research_rounds_used": 0,
            "critic_decision": "needs_more_research",
        }
        self.assertIsNone(next_escalation(state))

    def test_no_escalation_at_ceiling(self):
        state = {
            "effort": "xauto",
            "runtime_control": {"xauto_tier": "ceiling"},
            "research_rounds_used": 3,
            "critic_decision": "needs_more_research",
        }
        self.assertIsNone(next_escalation(state))

    def test_no_escalation_at_xmax_to_ceiling(self):
        # From xmax, the next "transition" is to ceiling — which
        # has the same topology, so no escalation needed.
        state = {
            "effort": "xauto",
            "runtime_control": {"xauto_tier": "xmax"},
            "research_rounds_used": 5,
            "critic_decision": "needs_more_research",
        }
        self.assertIsNone(next_escalation(state))


class TestNextEscalationSignals(unittest.TestCase):

    def _xauto_state(self, **kw):
        base = {
            "effort": "xauto",
            "research_rounds_used": 1,
            "runtime_control": {"xauto_tier": "xmedium"},
        }
        base.update(kw)
        return base

    def test_critic_dissent_triggers(self):
        state = self._xauto_state(
            critic_decision="needs_more_research",
        )
        d = next_escalation(state)
        self.assertIsNotNone(d)
        self.assertEqual(d.from_tier, "xmedium")
        self.assertEqual(d.to_tier, "xhigh")
        self.assertEqual(d.signal, "critic_dissent")
        self.assertIn("needs_more_research", d.reason)

    def test_critic_ready_no_dissent(self):
        # ready verdict → no critic_dissent signal.
        state = self._xauto_state(critic_decision="ready")
        # No confidence emitted, no other signals — escalation
        # should return None.
        self.assertIsNone(next_escalation(state))

    def test_gap_named_triggers(self):
        # critic says needs_more_research AND names a concrete gap.
        # The text contains both phrases the heuristic recognizes;
        # in production the critic prompt emits this format
        # deliberately at xauto.
        state = self._xauto_state(
            critic_decision="needs_more_research",
            critique="DECISION: needs_more_research\n"
                     "gap_named: no source for the rate limiter "
                     "behavior under burst load.",
        )
        d = next_escalation(state)
        self.assertIsNotNone(d)
        # gap_named or critic_dissent both fire here — gap_named
        # has higher priority in the signal order.
        self.assertEqual(d.signal, "gap_named")

    def test_low_confidence_triggers(self):
        # xmedium target is 0.60; below that fires.
        state = self._xauto_state(
            critic_decision="ready",
            confidence=[0.45],
        )
        d = next_escalation(state)
        self.assertIsNotNone(d)
        self.assertEqual(d.signal, "low_confidence")
        self.assertIn("0.45", d.reason)

    def test_high_confidence_no_escalation(self):
        # Above tier target → no escalation.
        state = self._xauto_state(
            critic_decision="ready",
            confidence=[0.80],
        )
        self.assertIsNone(next_escalation(state))

    def test_uses_latest_confidence_score(self):
        # ``latest_confidence`` reads [-1]; a later high score
        # MUST suppress the escalation even if earlier scores were
        # low. Mirrors the M5 interrupt-policy semantics.
        state = self._xauto_state(
            critic_decision="ready",
            confidence=[0.30, 0.85],
        )
        self.assertIsNone(next_escalation(state))

    def test_xmedium_to_xhigh_topology_delta(self):
        state = self._xauto_state(
            critic_decision="needs_more_research",
        )
        d = next_escalation(state)
        delta = d.runtime_control_delta
        self.assertEqual(delta["xauto_tier"], "xhigh")
        # xmedium=(1,1) → xhigh=(3,2): both fields change.
        self.assertEqual(delta["max_rounds"], 3)
        self.assertEqual(delta["max_reroutes"], 2)
        # confidence_target tightens.
        self.assertAlmostEqual(delta["confidence_target"], 0.70)
        # multi_critic doesn't change at this step.
        self.assertNotIn("multi_critic", delta)

    def test_xhigh_to_xmax_includes_multi_critic_flag(self):
        # xhigh.multi_critic=False → xmax.multi_critic=True.
        state = {
            "effort": "xauto",
            "research_rounds_used": 2,
            "runtime_control": {"xauto_tier": "xhigh"},
            "critic_decision": "needs_more_research",
        }
        d = next_escalation(state)
        self.assertEqual(d.to_tier, "xmax")
        self.assertIn("multi_critic", d.runtime_control_delta)
        self.assertIs(d.runtime_control_delta["multi_critic"], True)


# ============================================================== #
# Time-pressure semantics
# ============================================================== #

class TestTimePressure(unittest.TestCase):
    """≥ 70% of soft target elapsed SUPPRESSES the normal signals
    (escalation cost exceeds remaining budget) but is ITSELF the
    signal at xmedium when no critic is enabled."""

    def _ts_state(self, *, elapsed_fraction: float,
                   soft_budget_s: float = 100.0,
                   deadline_offset_s: float = 200.0,
                   **kw):
        """Build a state with a known soft-target elapsed fraction.

        ``elapsed_fraction=0.95`` means 95% of the (started_ts,
        soft_target_ts) budget has elapsed — well past the 70%
        pressure threshold. ``soft_budget_s`` and
        ``deadline_offset_s`` are absolute durations the test
        anchors against, computed back to wall-clock times.
        """
        now = time.time()
        # started_ts is in the past by elapsed_fraction * soft_budget.
        elapsed = elapsed_fraction * soft_budget_s
        started_ts = now - elapsed
        soft_target_ts = started_ts + soft_budget_s
        deadline_ts = started_ts + deadline_offset_s
        base = {
            "effort": "xauto",
            "research_rounds_used": 1,
            "runtime_control": {
                "xauto_tier": "xmedium",
                "started_ts": started_ts,
                "soft_target_ts": soft_target_ts,
                "deadline_ts": deadline_ts,
            },
        }
        base["runtime_control"].update(
            kw.pop("runtime_control_extra", {})
        )
        base.update(kw)
        return base

    def test_suppresses_critic_dissent_under_pressure(self):
        # 95% elapsed = past the 70% pressure threshold.
        # critic_dissent normally triggers, but pressure
        # suppresses it.
        state = self._ts_state(
            elapsed_fraction=0.95,
            critic_decision="needs_more_research",
        )
        d = next_escalation(state)
        self.assertIsNone(d)

    def test_time_pressure_promotes_to_xhigh_when_critic_off(self):
        # The rare case: time pressure mounting AND no critic
        # enabled in the active topology → upgrade to xhigh so a
        # critic perspective exists before the deadline. The "no
        # critic" condition is signalled by:
        # - critic_decision absent (no critic ran)
        # - "critic" not in enabled_roles
        state = self._ts_state(
            elapsed_fraction=0.80,
            runtime_control_extra={
                "enabled_roles": ["planner", "researcher",
                                   "synthesizer"],
            },
            # No critic_decision — critic didn't run.
        )
        d = next_escalation(state)
        self.assertIsNotNone(d)
        self.assertEqual(d.signal, "time_pressure")
        self.assertEqual(d.to_tier, "xhigh")

    def test_time_pressure_does_not_fire_when_critic_ran(self):
        # If critic_decision is present (even "ready"), the critic
        # is implicitly enabled — no carve-out needed.
        state = self._ts_state(
            elapsed_fraction=0.80,
            critic_decision="ready",
        )
        # critic_decision="ready" + no other signal → no
        # escalation (and time_pressure carve-out is gated off).
        d = next_escalation(state)
        self.assertIsNone(d)

    def test_below_pressure_threshold_no_suppression(self):
        # Only 50% elapsed → pressure threshold not yet reached.
        # critic_dissent fires normally.
        state = self._ts_state(
            elapsed_fraction=0.50,
            critic_decision="needs_more_research",
        )
        d = next_escalation(state)
        self.assertIsNotNone(d)
        self.assertEqual(d.signal, "critic_dissent")

    def test_no_soft_target_skips_time_pressure(self):
        # When no soft_target_ts is set, _is_time_pressure returns
        # False and the normal signals fire.
        state = {
            "effort": "xauto",
            "research_rounds_used": 1,
            "runtime_control": {"xauto_tier": "xmedium"},
            "critic_decision": "needs_more_research",
        }
        d = next_escalation(state)
        self.assertIsNotNone(d)
        self.assertEqual(d.signal, "critic_dissent")


# ============================================================== #
# Signal priority order
# ============================================================== #

class TestSignalPriority(unittest.TestCase):
    """When multiple signals are active, the priority order is:
    critic_dissent < gap_named < low_confidence < time_pressure.
    First match in code wins."""

    def test_gap_named_beats_low_confidence(self):
        # Both critic-dissent AND named-gap markers present, AND
        # low confidence. gap_named beats both.
        state = {
            "effort": "xauto",
            "research_rounds_used": 1,
            "runtime_control": {"xauto_tier": "xmedium"},
            "critic_decision": "needs_more_research",
            "critique": "DECISION: needs_more_research\n"
                        "gap_named: missing the auth middleware.",
            "confidence": [0.20],
        }
        d = next_escalation(state)
        self.assertEqual(d.signal, "gap_named")

    def test_critic_dissent_beats_low_confidence(self):
        # No gap_named marker, but critic dissent AND low
        # confidence. Dissent fires first.
        state = {
            "effort": "xauto",
            "research_rounds_used": 1,
            "runtime_control": {"xauto_tier": "xmedium"},
            "critic_decision": "needs_more_research",
            "critique": "DECISION: needs_more_research",
            "confidence": [0.20],
        }
        d = next_escalation(state)
        self.assertEqual(d.signal, "critic_dissent")


# ============================================================== #
# apply_escalation + RuntimeMutation event
# ============================================================== #

class TestApplyEscalation(unittest.TestCase):

    def test_returns_state_delta_with_runtime_control(self):
        d = EscalationDecision(
            from_tier="xmedium", to_tier="xhigh",
            signal="critic_dissent",
            reason="critic dissent at round 1",
            runtime_control_delta={
                "xauto_tier": "xhigh",
                "max_rounds": 3,
            },
        )
        state_delta = apply_escalation({}, d)
        rc = state_delta["runtime_control"]
        self.assertEqual(rc["xauto_tier"], "xhigh")
        self.assertEqual(rc["max_rounds"], 3)
        # xauto_escalations incremented from default 0.
        self.assertEqual(rc["xauto_escalations"], 1)

    def test_xauto_escalations_increments_from_prior(self):
        d = EscalationDecision(
            from_tier="xhigh", to_tier="xmax",
            signal="gap_named", reason="...",
            runtime_control_delta={"xauto_tier": "xmax"},
        )
        state = {"runtime_control": {"xauto_escalations": 1}}
        out = apply_escalation(state, d)
        self.assertEqual(
            out["runtime_control"]["xauto_escalations"], 2,
        )


class TestRuntimeMutationEvent(unittest.TestCase):

    def test_event_data_has_changes_and_reason(self):
        d = EscalationDecision(
            from_tier="xmedium", to_tier="xhigh",
            signal="critic_dissent",
            reason="critic returned needs_more_research after round 1",
            runtime_control_delta={
                "xauto_tier": "xhigh", "max_rounds": 3,
            },
        )
        ev = runtime_mutation_event_data(d)
        self.assertEqual(ev["changes"]["xauto_tier"], "xhigh")
        self.assertEqual(ev["changes"]["max_rounds"], 3)
        # Reason string concatenates tier + signal + raw reason.
        self.assertIn("xmedium → xhigh", ev["reason"])
        self.assertIn("critic_dissent", ev["reason"])
        self.assertIn("needs_more_research", ev["reason"])


# ============================================================== #
# TIER_TOPOLOGIES sanity
# ============================================================== #

class TestTierTopologies(unittest.TestCase):
    """Static checks on the tier table — escalation math anchors
    on these so any change must hold the invariants."""

    def test_monotonic_growth(self):
        # max_rounds non-decreasing across forward transitions.
        tiers = ["xmedium", "xhigh", "xmax"]
        for a, b in zip(tiers[:-1], tiers[1:]):
            self.assertLessEqual(
                TIER_TOPOLOGIES[a].max_rounds,
                TIER_TOPOLOGIES[b].max_rounds,
                f"max_rounds should not decrease {a} -> {b}",
            )
            self.assertLessEqual(
                TIER_TOPOLOGIES[a].max_reroutes,
                TIER_TOPOLOGIES[b].max_reroutes,
                f"max_reroutes should not decrease {a} -> {b}",
            )

    def test_confidence_target_tightens(self):
        # As we escalate, the confidence threshold tightens — a
        # harder topology demands higher confidence to claim done.
        self.assertLess(
            TIER_TOPOLOGIES["xmedium"].confidence_target,
            TIER_TOPOLOGIES["xhigh"].confidence_target,
        )
        self.assertLess(
            TIER_TOPOLOGIES["xhigh"].confidence_target,
            TIER_TOPOLOGIES["xmax"].confidence_target,
        )

    def test_only_xmax_uses_multi_critic(self):
        # Multi-critic is the xmax-only differentiator (Phase 10).
        self.assertFalse(TIER_TOPOLOGIES["xmedium"].multi_critic)
        self.assertFalse(TIER_TOPOLOGIES["xhigh"].multi_critic)
        self.assertTrue(TIER_TOPOLOGIES["xmax"].multi_critic)

    def test_ceiling_matches_xmax(self):
        # Ceiling sentinel — same topology, distinct only as a
        # stop state for the state machine.
        c = TIER_TOPOLOGIES["ceiling"]
        m = TIER_TOPOLOGIES["xmax"]
        self.assertEqual(c.max_rounds, m.max_rounds)
        self.assertEqual(c.max_reroutes, m.max_reroutes)
        self.assertEqual(c.multi_critic, m.multi_critic)


if __name__ == "__main__":
    unittest.main()
