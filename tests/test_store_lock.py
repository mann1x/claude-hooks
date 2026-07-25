"""Tests for the cross-process store gate (``claude_hooks.store_lock``).

The gate exists because ``detach_store`` turned a serialised store
workload into a concurrent one, which broke dedup (two children each
finish dedup-recall before either writes, so neither sees the other and
both store) and starved interactive recall of embedder capacity. See
the module docstring for the full incident.

Contract under test:

- a free gate is acquired (``held=True``)
- a held gate blocks a second acquirer until release
- on timeout the body **still runs**, with ``held=False`` — the failure
  policy is "a duplicate beats a dropped memory"
- an unusable lock location degrades to open rather than raising
- the lock is released on exit, including when the body raises

Every test points ``CLAUDE_HOOKS_STORE_LOCK_PATH`` at a tmpdir so a test
run never contends with a real store on the developer's box.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from claude_hooks import store_lock  # noqa: E402


@pytest.fixture
def lock_file(tmp_path, monkeypatch):
    p = tmp_path / "store.lock"
    monkeypatch.setenv(store_lock._ENV_PATH, str(p))
    return p


class TestStoreGateBasics:
    def test_acquires_when_free(self, lock_file):
        with store_lock.store_gate(timeout=5) as held:
            assert held is True

    def test_releases_on_exit(self, lock_file):
        with store_lock.store_gate(timeout=5) as held:
            assert held is True
        # A second acquisition must succeed immediately.
        start = time.monotonic()
        with store_lock.store_gate(timeout=5) as held:
            assert held is True
        assert time.monotonic() - start < 1.0

    def test_releases_even_when_body_raises(self, lock_file):
        with pytest.raises(RuntimeError):
            with store_lock.store_gate(timeout=5):
                raise RuntimeError("boom")
        with store_lock.store_gate(timeout=5) as held:
            assert held is True

    def test_lock_path_honours_env_override(self, lock_file):
        assert store_lock.lock_path() == lock_file


class TestStoreGateContention:
    def test_second_acquirer_times_out_but_body_still_runs(self, lock_file):
        """The load-bearing failure policy: never skip the store."""
        ran = []
        with store_lock.store_gate(timeout=5) as outer:
            assert outer is True
            # Separate open() => separate open-file-description, so this
            # genuinely contends even in-process.
            with store_lock.store_gate(timeout=0.5, poll=0.05) as inner:
                assert inner is False       # did not get the gate
                ran.append("body")          # ...but still executed
        assert ran == ["body"]

    def test_waiter_acquires_once_holder_releases(self, lock_file):
        """Cross-process: B queues behind A and proceeds after release."""
        script = textwrap.dedent(
            """
            import os, sys, time
            os.environ["CLAUDE_HOOKS_STORE_LOCK_PATH"] = sys.argv[1]
            sys.path.insert(0, sys.argv[2])
            from claude_hooks.store_lock import store_gate
            hold = float(sys.argv[4])
            t0 = time.monotonic()
            with store_gate(timeout=30, poll=0.05) as held:
                print(f"{sys.argv[3]} {held} {time.monotonic()-t0:.2f}", flush=True)
                time.sleep(hold)
            """
        )
        a = subprocess.Popen(
            [sys.executable, "-c", script, str(lock_file), str(REPO), "A", "1.5"],
            stdout=subprocess.PIPE, text=True,
        )
        time.sleep(0.4)
        b = subprocess.Popen(
            [sys.executable, "-c", script, str(lock_file), str(REPO), "B", "0.0"],
            stdout=subprocess.PIPE, text=True,
        )
        a_out = a.communicate(timeout=60)[0].split()
        b_out = b.communicate(timeout=60)[0].split()

        assert a_out[0] == "A" and a_out[1] == "True"
        assert b_out[0] == "B" and b_out[1] == "True"
        # B must have waited for A rather than running straight through.
        assert float(b_out[2]) > 0.5, f"B did not queue: waited {b_out[2]}s"


class TestStoreGateDegradation:
    def test_unusable_lock_path_degrades_to_open(self, monkeypatch):
        """A gate we cannot create must not block the store."""
        monkeypatch.setattr(store_lock, "lock_path", lambda: None)
        with store_lock.store_gate(timeout=1) as held:
            assert held is False

    def test_unopenable_path_degrades_to_open(self, tmp_path, monkeypatch):
        # A directory is not openable as a lock file.
        d = tmp_path / "adir"
        d.mkdir()
        monkeypatch.setenv(store_lock._ENV_PATH, str(d))
        with store_lock.store_gate(timeout=1) as held:
            assert held is False

    def test_timeout_env_override(self, lock_file, monkeypatch):
        monkeypatch.setenv(store_lock._ENV_TIMEOUT, "0.25")
        assert store_lock._timeout_default() == 0.25

    def test_bad_timeout_env_falls_back(self, lock_file, monkeypatch):
        monkeypatch.setenv(store_lock._ENV_TIMEOUT, "not-a-number")
        assert store_lock._timeout_default() == store_lock.DEFAULT_TIMEOUT_S


class TestStoreAsyncUsesGate:
    def test_dedup_and_store_runs_inside_the_gate(self, lock_file, monkeypatch):
        """Regression guard: the gate must wrap dedup+store, not just store.

        If it ever wraps only the write, two concurrent children can
        still both pass dedup and duplicate the memory — the exact bug
        this was built for.
        """
        from claude_hooks import store_async

        seen = {}

        class _P:
            name = "pgvector"

            def store(self, content, metadata=None):
                # While storing, the gate must be held: a fresh
                # acquisition attempt has to fail.
                with store_lock.store_gate(timeout=0.3, poll=0.05) as held:
                    seen["gate_held_during_store"] = (held is False)

        store_async._run_dedup_and_store(
            {"providers": {"pgvector": {}}}, "x" * 200, {}, [_P()],
        )
        assert seen.get("gate_held_during_store") is True
