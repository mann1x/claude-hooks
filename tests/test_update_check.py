"""Unit tests for ``claude_hooks.update_check``.

Covers version comparison, scheduler windows, retry policy,
notification budget, and silent-on-timeout behaviour. The GitHub
HTTP call is injected via the ``fetch`` parameter — these tests
never touch the network.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from claude_hooks import update_check as uc  # noqa: E402


def _make_cfg(tmpdir, **overrides):
    """Build a config dict pointing the state file inside ``tmpdir``."""
    base = {
        "update_check": {
            "enabled": True,
            "interval_seconds": 86400,
            "retry_pause_seconds": 300,
            "max_retries": 5,
            "github_repo": "mann1x/claude-hooks",
            "timeout_seconds": 5,
            "max_notifications": 10,
            "state_path": str(Path(tmpdir) / "state.json"),
        }
    }
    base["update_check"].update(overrides)
    return base


# --------------------------------------------------------------------------- #
# Version comparison
# --------------------------------------------------------------------------- #
class VersionParseTests(unittest.TestCase):

    def test_parses_plain_semver(self):
        self.assertEqual(uc.parse_version("1.0.0"), (1, 0, 0))

    def test_parses_v_prefix(self):
        self.assertEqual(uc.parse_version("v1.2.3"), (1, 2, 3))

    def test_parses_with_prerelease_suffix(self):
        self.assertEqual(uc.parse_version("v1.0.0-rc.1"), (1, 0, 0))

    def test_returns_none_for_garbage(self):
        self.assertIsNone(uc.parse_version("not-a-version"))
        self.assertIsNone(uc.parse_version(""))
        self.assertIsNone(uc.parse_version(None))  # type: ignore[arg-type]

    def test_is_newer_strict_greater(self):
        self.assertTrue(uc.is_newer("1.0.1", "1.0.0"))
        self.assertTrue(uc.is_newer("v1.1.0", "1.0.99"))
        self.assertTrue(uc.is_newer("2.0.0", "1.99.99"))

    def test_is_newer_equal_or_lower_returns_false(self):
        self.assertFalse(uc.is_newer("1.0.0", "1.0.0"))
        self.assertFalse(uc.is_newer("1.0.0", "1.0.1"))
        self.assertFalse(uc.is_newer("0.9.0", "1.0.0"))

    def test_is_newer_handles_garbage(self):
        # Unparseable input must NEVER announce a phantom upgrade.
        self.assertFalse(uc.is_newer("garbage", "1.0.0"))
        self.assertFalse(uc.is_newer("1.0.0", "garbage"))


# --------------------------------------------------------------------------- #
# State load/save roundtrip
# --------------------------------------------------------------------------- #
class StateRoundtripTests(unittest.TestCase):

    def test_missing_file_returns_empty_dict(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = uc.get_cfg(_make_cfg(tmp))
            self.assertEqual(uc.load_state(cfg), {})

    def test_save_then_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = uc.get_cfg(_make_cfg(tmp))
            uc.save_state(cfg, {"latest_version": "v1.0.5", "retry_count": 2})
            loaded = uc.load_state(cfg)
            self.assertEqual(loaded["latest_version"], "v1.0.5")
            self.assertEqual(loaded["retry_count"], 2)

    def test_corrupt_state_yields_empty_dict(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = uc.get_cfg(_make_cfg(tmp))
            Path(cfg["state_path"]).write_text("{not json", encoding="utf-8")
            self.assertEqual(uc.load_state(cfg), {})


# --------------------------------------------------------------------------- #
# Scheduler — should_run_check decisions
# --------------------------------------------------------------------------- #
class SchedulerTests(unittest.TestCase):

    def test_disabled_never_runs(self):
        cfg = uc.get_cfg({"update_check": {"enabled": False}})
        self.assertFalse(uc.should_run_check({}, cfg, now=1_000_000))

    def test_fresh_state_runs_immediately(self):
        cfg = uc.get_cfg({"update_check": {"enabled": True}})
        self.assertTrue(uc.should_run_check({}, cfg, now=1_000_000))

    def test_within_interval_skips(self):
        cfg = uc.get_cfg({"update_check": {"enabled": True, "interval_seconds": 86400}})
        state = {"last_check_at": 999_950_000}
        # 50_000s elapsed, < 86400 → no run.
        self.assertFalse(uc.should_run_check(state, cfg, now=1_000_000_000))

    def test_after_interval_runs(self):
        cfg = uc.get_cfg({"update_check": {"enabled": True, "interval_seconds": 86400}})
        state = {"last_check_at": 999_900_000}
        # 100_000s elapsed > 86400 → run.
        self.assertTrue(uc.should_run_check(state, cfg, now=1_000_000_000))

    def test_retry_window_pending_skips(self):
        cfg = uc.get_cfg({"update_check": {"enabled": True}})
        state = {"next_retry_at": 1_000_000_500, "retry_count": 1}
        self.assertFalse(uc.should_run_check(state, cfg, now=1_000_000_000))

    def test_retry_window_due_runs(self):
        cfg = uc.get_cfg({"update_check": {"enabled": True}})
        state = {"next_retry_at": 1_000_000_000, "retry_count": 1}
        self.assertTrue(uc.should_run_check(state, cfg, now=1_000_000_001))


# --------------------------------------------------------------------------- #
# run_due_check end-to-end
# --------------------------------------------------------------------------- #
class RunDueCheckTests(unittest.TestCase):

    def test_disabled_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _make_cfg(tmp, enabled=False)
            calls = []
            def fake_fetch(repo, *, timeout):
                calls.append(repo)
                return "v9.9.9"
            self.assertIsNone(uc.run_due_check(cfg, fetch=fake_fetch))
            self.assertEqual(calls, [], "fetch must not be called when disabled")

    def test_success_records_latest_and_clears_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _make_cfg(tmp)
            with patch.object(uc, "CURRENT_VERSION", "1.0.0"):
                state = uc.run_due_check(
                    cfg, now=1000, fetch=lambda r, **k: "v1.0.5",
                )
            self.assertEqual(state["latest_version"], "v1.0.5")
            self.assertTrue(state["update_available"])
            self.assertEqual(state["retry_count"], 0)
            self.assertIsNone(state["next_retry_at"])
            self.assertEqual(state["last_check_at"], 1000)
            self.assertEqual(state["last_success_at"], 1000)

    def test_failure_schedules_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _make_cfg(tmp)
            state = uc.run_due_check(
                cfg, now=1000, fetch=lambda r, **k: None,
            )
            self.assertEqual(state["retry_count"], 1)
            self.assertEqual(state["next_retry_at"], 1000 + 300)

    def test_retry_increment_each_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _make_cfg(tmp)
            now = 1000
            for expected in range(1, 6):
                state = uc.run_due_check(
                    cfg, now=now, fetch=lambda r, **k: None,
                )
                self.assertEqual(state["retry_count"], expected)
                now = state["next_retry_at"]

    def test_retries_exhausted_defers_to_next_24h(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _make_cfg(tmp)
            now = 1000
            for _ in range(5):
                state = uc.run_due_check(cfg, now=now, fetch=lambda r, **k: None)
                now = state["next_retry_at"]
            # 6th attempt — exceeds max_retries=5; reset retry window.
            state = uc.run_due_check(cfg, now=now, fetch=lambda r, **k: None)
            self.assertEqual(state["retry_count"], 0)
            self.assertIsNone(state["next_retry_at"])
            # Next call must wait the full 24h interval.
            self.assertFalse(uc.should_run_check(
                state, uc.get_cfg(cfg), now=now + 3600,
            ))
            self.assertTrue(uc.should_run_check(
                state, uc.get_cfg(cfg), now=now + 86401,
            ))

    def test_no_update_when_already_at_latest(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _make_cfg(tmp)
            with patch.object(uc, "CURRENT_VERSION", "1.0.0"):
                state = uc.run_due_check(
                    cfg, now=1000, fetch=lambda r, **k: "v1.0.0",
                )
            self.assertFalse(state["update_available"])


# --------------------------------------------------------------------------- #
# Notification budget
# --------------------------------------------------------------------------- #
class NotificationBudgetTests(unittest.TestCase):

    def test_disabled_yields_no_notice(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _make_cfg(tmp, enabled=False)
            self.assertIsNone(uc.pending_notification(cfg))

    def test_no_state_yields_no_notice(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _make_cfg(tmp)
            self.assertIsNone(uc.pending_notification(cfg))

    def test_pops_up_until_budget_exhausted(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _make_cfg(tmp, max_notifications=10)
            with patch.object(uc, "CURRENT_VERSION", "1.0.0"):
                uc.run_due_check(
                    cfg, now=1000, fetch=lambda r, **k: "v1.0.5",
                )
                # First 10 calls return the message, then it's silenced.
                for i in range(10):
                    msg = uc.pending_notification(cfg)
                    self.assertIsNotNone(msg, f"iteration {i} silenced too early")
                    self.assertIn("1.0.5", msg)
                    uc.consume_notification(cfg)
                self.assertIsNone(
                    uc.pending_notification(cfg),
                    "11th call must be silenced",
                )

    def test_user_upgraded_silences_notice(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _make_cfg(tmp)
            # Stale state from before the upgrade.
            uc_cfg = uc.get_cfg(cfg)
            uc.save_state(uc_cfg, {
                "update_available": True,
                "latest_version": "v1.0.5",
                "notification_count": 0,
            })
            with patch.object(uc, "CURRENT_VERSION", "1.0.5"):
                # User now runs the version we said was "latest".
                self.assertIsNone(uc.pending_notification(cfg))

    def test_new_release_resets_counter(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _make_cfg(tmp, max_notifications=2)
            with patch.object(uc, "CURRENT_VERSION", "1.0.0"):
                # First release: exhaust budget.
                uc.run_due_check(cfg, now=1000, fetch=lambda r, **k: "v1.0.5")
                for _ in range(2):
                    uc.pending_notification(cfg)
                    uc.consume_notification(cfg)
                self.assertIsNone(uc.pending_notification(cfg))
                # New release lands → counter resets.
                uc.run_due_check(cfg, now=1_000_000, fetch=lambda r, **k: "v1.0.6")
                self.assertIsNotNone(uc.pending_notification(cfg))


# --------------------------------------------------------------------------- #
# Silent-on-timeout
# --------------------------------------------------------------------------- #
class SilentOnTimeoutTests(unittest.TestCase):

    def test_fetch_raising_treated_as_failure(self):
        # The real fetch_latest_tag swallows exceptions and returns None;
        # this confirms run_due_check still routes to the retry path even
        # if a custom fetcher misbehaves and raises.
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _make_cfg(tmp)

            def boom(repo, *, timeout):
                # This emulates a real urllib timeout ascending. The
                # production path catches this in fetch_latest_tag itself,
                # but we still want run_due_check robust if a custom fetch
                # leaks an exception. So wrap defensively here.
                raise TimeoutError("simulated timeout")

            try:
                state = uc.run_due_check(cfg, now=1000, fetch=boom)
            except TimeoutError:
                self.fail("run_due_check must not raise on fetch failure")
            # The retry was scheduled OR no state was written. Either is
            # acceptable as long as nothing propagated.
            self.assertTrue(state is None or "retry_count" in state)

    def test_real_fetch_returns_none_on_timeout(self):
        # Direct call into fetch_latest_tag with a 0-second timeout should
        # silently return None instead of raising.
        result = uc.fetch_latest_tag("nonexistent/repo", timeout=0.001)
        self.assertIsNone(result)


# --------------------------------------------------------------------------- #
# Smoke test against a live-looking GitHub response shape
# --------------------------------------------------------------------------- #
class FetchShapeTests(unittest.TestCase):

    def test_extracts_tag_from_releases_json(self):
        body = json.dumps({
            "tag_name": "v1.0.5",
            "name": "claude-hooks v1.0.5",
            "body": "release notes",
        }).encode("utf-8")

        class _Resp:
            def __enter__(self_inner): return self_inner
            def __exit__(self_inner, *a): return False
            def read(self_inner): return body

        with patch("urllib.request.urlopen", return_value=_Resp()):
            tag = uc.fetch_latest_tag("any/repo", timeout=5)
        self.assertEqual(tag, "v1.0.5")

    def test_returns_none_on_missing_tag_field(self):
        body = json.dumps({"name": "no tag here"}).encode("utf-8")

        class _Resp:
            def __enter__(self_inner): return self_inner
            def __exit__(self_inner, *a): return False
            def read(self_inner): return body

        with patch("urllib.request.urlopen", return_value=_Resp()):
            tag = uc.fetch_latest_tag("any/repo", timeout=5)
        self.assertIsNone(tag)


# --------------------------------------------------------------------------- #
# Stop-hook integration via _with_update_notice
# --------------------------------------------------------------------------- #
class StopHookNoticeTests(unittest.TestCase):

    def _seed_update_state(self, tmp, latest="v1.0.5"):
        cfg = _make_cfg(tmp)
        with patch.object(uc, "CURRENT_VERSION", "1.0.0"):
            uc.run_due_check(cfg, now=1000, fetch=lambda r, **k: latest)
        return cfg

    def test_attaches_to_none_result(self):
        from claude_hooks.hooks.stop import _with_update_notice
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._seed_update_state(tmp)
            with patch.object(uc, "CURRENT_VERSION", "1.0.0"):
                out = _with_update_notice(None, cfg)
            self.assertIsNotNone(out)
            self.assertIn("1.0.5", out["systemMessage"])
            self.assertIn("update available", out["systemMessage"])

    def test_appends_to_existing_systemmessage(self):
        from claude_hooks.hooks.stop import _with_update_notice
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._seed_update_state(tmp)
            with patch.object(uc, "CURRENT_VERSION", "1.0.0"):
                out = _with_update_notice(
                    {"systemMessage": "[claude-hooks] stored to qdrant"},
                    cfg,
                )
            self.assertIn("stored to qdrant", out["systemMessage"])
            self.assertIn("1.0.5", out["systemMessage"])
            self.assertIn("\n", out["systemMessage"])

    def test_no_op_when_no_update(self):
        from claude_hooks.hooks.stop import _with_update_notice
        cfg = _make_cfg(tempfile.gettempdir(), enabled=True)
        # No state file → no notice.
        cfg["update_check"]["state_path"] = str(
            Path(tempfile.gettempdir()) / "nonexistent-update-state.json"
        )
        Path(cfg["update_check"]["state_path"]).unlink(missing_ok=True)
        out = _with_update_notice(None, cfg)
        self.assertIsNone(out)

    def test_consumes_budget_each_call(self):
        from claude_hooks.hooks.stop import _with_update_notice
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._seed_update_state(tmp)
            cfg["update_check"]["max_notifications"] = 3
            with patch.object(uc, "CURRENT_VERSION", "1.0.0"):
                for _ in range(3):
                    out = _with_update_notice(None, cfg)
                    self.assertIsNotNone(out)
                # Fourth call → silenced.
                out = _with_update_notice(None, cfg)
            self.assertIsNone(out)


if __name__ == "__main__":
    unittest.main()
