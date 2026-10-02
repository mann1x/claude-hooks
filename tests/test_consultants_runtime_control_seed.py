"""The runners seed ``runtime_control``.

Until 2026-09-30 nothing did, and every consumer reads an absent record
as "not configured": the researcher's stall monitor never wrapped a call,
the deadline in ``route_after_critic`` never fired, and POST /control
mutated caps no node had been started with. Each of those looked
configured and did nothing.
"""
from __future__ import annotations

import inspect
import unittest

from consultants import config as cc
from consultants.server import runner


class SeedTests(unittest.TestCase):

    def _cfg(self, effort):
        cfg = cc.ConsultantsConfig()
        cfg.effort = effort
        return cfg

    def test_deadline_and_caps_are_present(self):
        rc = runner._seed_runtime_control(
            self._cfg("high"), ["planner", "researcher", "critic",
                                "synthesizer"])
        self.assertGreater(rc["deadline_ts"], rc["soft_target_ts"])
        self.assertEqual((rc["max_rounds"], rc["max_reroutes"]), (3, 2))

    def test_enabled_roles_are_the_runs_not_the_configs(self):
        """The runner drops the critic below high effort; the config-
        derived default would put it back for any reader of the live
        record."""
        self.assertIn("critic", cc.enabled_roles(self._cfg("medium")))
        rc = runner._seed_runtime_control(
            self._cfg("medium"), ("planner", "researcher", "synthesizer"))
        self.assertEqual(rc["enabled_roles"],
                         ["planner", "researcher", "synthesizer"])

    def test_stall_thresholds_are_left_to_the_model(self):
        rc = runner._seed_runtime_control(self._cfg("medium"), ["researcher"])
        self.assertNotIn("stall_threshold_s", rc)
        self.assertNotIn("per_lane_hard_s", rc)

    def test_fanout_extras_extend_the_deadline(self):
        base = runner._seed_runtime_control(self._cfg("xhigh"), ["researcher"])
        wide = runner._seed_runtime_control(self._cfg("xhigh"), ["researcher"],
                                            n_fanout_extras=2)
        span = lambda rc: rc["deadline_ts"] - rc["soft_target_ts"]  # noqa: E731
        self.assertGreater(span(wide), span(base))


class WiringTests(unittest.TestCase):

    def test_both_runners_seed_before_the_graph_starts(self):
        src = inspect.getsource(runner)
        for fn in ("make_runner", "make_follow_up_runner"):
            body = inspect.getsource(getattr(runner, fn))
            self.assertIn('initial["runtime_control"] = _seed_runtime_control(',
                          body, fn)
            self.assertLess(body.index('initial["runtime_control"]'),
                            body.index("_drive_council_stream("), fn)
        self.assertIn("def _seed_runtime_control", src)


if __name__ == "__main__":
    unittest.main()
