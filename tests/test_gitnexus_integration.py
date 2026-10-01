"""Tests for claude_hooks.gitnexus_integration — Tier 2."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from claude_hooks import claudemem_reindex as cr
from claude_hooks import gitnexus_integration as gn


@pytest.fixture(autouse=True)
def _no_real_gitnexus(monkeypatch):
    """Make sure detection never picks up a real gitnexus install on the
    host running the tests. Each test opts back in selectively."""
    monkeypatch.setattr(gn.shutil, "which", lambda _: None)
    monkeypatch.setattr(gn, "_global_registry", lambda: None)
    yield


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

class TestDetection:
    def test_unavailable_when_nothing_present(self):
        assert gn.is_available() is False
        assert gn.binary_path() is None

    def test_available_when_binary_on_path(self, monkeypatch, tmp_path):
        fake = tmp_path / "gitnexus"
        fake.write_text("#!/bin/sh\necho 1.2.3\n")
        fake.chmod(0o755)
        monkeypatch.setattr(gn.shutil, "which",
                            lambda name: str(fake) if name == "gitnexus" else None)
        assert gn.is_available() is True
        assert gn.binary_path() == str(fake)

    def test_available_when_global_registry_present(self, monkeypatch, tmp_path):
        reg = tmp_path / ".gitnexus" / "registry.json"
        reg.parent.mkdir()
        reg.write_text("{}")
        monkeypatch.setattr(gn, "_global_registry", lambda: reg)
        assert gn.is_available() is True

    def test_indexed_when_project_dir_exists(self, tmp_path):
        (tmp_path / ".gitnexus").mkdir()
        assert gn.is_indexed(tmp_path) is True

    def test_not_indexed_when_dir_missing(self, tmp_path):
        assert gn.is_indexed(tmp_path) is False


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

class TestStatus:
    def test_status_when_nothing_installed(self, tmp_path):
        s = gn.status(tmp_path)
        assert s["binary"] is None
        assert s["project_indexed"] is False
        assert s["version"] is None

    def test_status_when_indexed_and_installed(self, monkeypatch, tmp_path):
        fake = tmp_path / "gitnexus"
        fake.write_text("#!/bin/sh\necho gitnexus 1.0.0\n")
        fake.chmod(0o755)
        monkeypatch.setattr(gn.shutil, "which",
                            lambda name: str(fake) if name == "gitnexus" else None)
        # Don't exec the fake binary: a ``#!/bin/sh`` script with no
        # extension isn't directly runnable on Windows (CreateProcess
        # rejects it → version probes None). Mock subprocess.run so the
        # test exercises _probe_version's parsing portably; the which()
        # mock already supplies the path.
        monkeypatch.setattr(
            gn.subprocess, "run",
            lambda *a, **k: gn.subprocess.CompletedProcess(
                a[0] if a else [], 0, "gitnexus 1.0.0\n", ""),
        )
        (tmp_path / ".gitnexus").mkdir()
        s = gn.status(tmp_path)
        assert s["binary"] == str(fake)
        assert s["project_indexed"] is True
        assert s["version"] == "gitnexus 1.0.0"

    def test_version_probe_swallows_errors(self, monkeypatch):
        monkeypatch.setattr(gn.shutil, "which", lambda _: "/no/such/binary")

        def boom(*a, **kw):
            raise OSError("nope")
        monkeypatch.setattr(gn.subprocess, "run", boom)
        assert gn._probe_version() is None


# ---------------------------------------------------------------------------
# session_start_hint
# ---------------------------------------------------------------------------

class TestHint:
    def test_silent_when_not_present(self, tmp_path):
        assert gn.session_start_hint(tmp_path) is None

    def test_hint_when_indexed(self, tmp_path):
        (tmp_path / ".gitnexus").mkdir()
        h = gn.session_start_hint(tmp_path)
        assert h is not None
        assert "gitnexus" in h.lower()
        assert "mcp__gitnexus__" in h

    def test_hint_when_installed_but_unindexed(self, monkeypatch, tmp_path):
        monkeypatch.setattr(gn.shutil, "which",
                            lambda name: "/usr/bin/gitnexus" if name == "gitnexus" else None)
        h = gn.session_start_hint(tmp_path)
        assert h is not None
        assert "gitnexus init" in h.lower()


# ---------------------------------------------------------------------------
# reindex_if_dirty_async
# ---------------------------------------------------------------------------

class TestReindex:
    def test_no_op_when_unmodified(self, tmp_path):
        gn.reindex_if_dirty_async(cwd=str(tmp_path), turn_modified=False)
        # Nothing to assert beyond "no exception" — we mock spawn below.

    def test_no_op_when_no_binary(self, tmp_path):
        gn.reindex_if_dirty_async(cwd=str(tmp_path), turn_modified=True)
        # No binary = early return; verify nothing got spawned.

    def test_spawns_when_indexed_and_present(self, monkeypatch, tmp_path):
        # Stub the binary
        bin_path = "/usr/bin/gitnexus"
        monkeypatch.setattr(gn.shutil, "which",
                            lambda name: bin_path if name == "gitnexus" else None)
        # Mark project as indexed
        (tmp_path / ".gitnexus").mkdir()
        # Add a .git so _find_marker_root resolves
        (tmp_path / ".git").mkdir()

        spawned = []

        class _FakePopen:
            def __init__(self, args, **kwargs):
                spawned.append((args, kwargs))

        monkeypatch.setattr(gn.subprocess, "Popen", _FakePopen)

        gn.reindex_if_dirty_async(cwd=str(tmp_path), turn_modified=True)

        assert len(spawned) == 1
        args, _ = spawned[0]
        # Supervised: the analyze runs under a Python supervisor that
        # records its outcome and retries a failed rebuild.
        assert args[1:3] == ["-m", "claude_hooks.gitnexus_integration"]
        assert args[3:] == ["--supervise", str(tmp_path.resolve()), bin_path]

    def test_lock_blocks_rapid_respawn(self, monkeypatch, tmp_path):
        monkeypatch.setattr(gn.shutil, "which",
                            lambda name: "/usr/bin/gitnexus" if name == "gitnexus" else None)
        (tmp_path / ".gitnexus").mkdir()
        (tmp_path / ".git").mkdir()
        spawned = []
        monkeypatch.setattr(gn.subprocess, "Popen",
                            lambda *a, **kw: spawned.append(a))

        gn.reindex_if_dirty_async(cwd=str(tmp_path), turn_modified=True)
        gn.reindex_if_dirty_async(cwd=str(tmp_path), turn_modified=True)
        # First call spawned; second hit fresh lock and bailed
        assert len(spawned) == 1

    def test_live_pid_blocks_respawn_after_cooldown_expires(
        self, monkeypatch, tmp_path,
    ):
        """A rebuild that outlives the cooldown must not get a rival.

        Regression for 2026-09-25: gitnexus v1.6.12 turned the first
        analyze per repo into a multi-minute FULL rebuild, so the 60s
        age guard expired while the previous run was still writing
        ``.gitnexus/graph-csv``. The second run then deleted or collided
        with the first one's staging CSVs and both failed.
        """
        monkeypatch.setattr(gn.shutil, "which",
                            lambda name: "/usr/bin/gitnexus" if name == "gitnexus" else None)
        (tmp_path / ".gitnexus").mkdir()
        (tmp_path / ".git").mkdir()

        spawned = []

        class _FakePopen:
            # os.getpid() is guaranteed alive, so it stands in for a
            # still-running analyze.
            pid = os.getpid()

            def __init__(self, args, **kwargs):
                spawned.append(args)

        monkeypatch.setattr(gn.subprocess, "Popen", _FakePopen)

        gn.reindex_if_dirty_async(cwd=str(tmp_path), turn_modified=True)
        assert len(spawned) == 1

        # Cooldown fully expired — only the liveness guard can save us.
        gn.reindex_if_dirty_async(
            cwd=str(tmp_path), turn_modified=True, lock_min_age_seconds=0,
        )
        assert len(spawned) == 1, "live pid must block a second analyze"

    def test_dead_pid_allows_respawn_after_cooldown(self, monkeypatch, tmp_path):
        """The liveness guard must not wedge the lock forever: once the
        recorded pid is gone and the cooldown has passed, reindex runs."""
        monkeypatch.setattr(gn.shutil, "which",
                            lambda name: "/usr/bin/gitnexus" if name == "gitnexus" else None)
        (tmp_path / ".gitnexus").mkdir()
        (tmp_path / ".git").mkdir()
        # A high, unallocated pid stands in for a finished analyze.
        (tmp_path / gn._LOCK_FILENAME).write_text("4194303\n0", encoding="utf-8")

        spawned = []
        monkeypatch.setattr(gn.subprocess, "Popen",
                            lambda *a, **kw: spawned.append(a))

        gn.reindex_if_dirty_async(cwd=str(tmp_path), turn_modified=True)
        assert len(spawned) == 1

    @pytest.mark.skipif(os.name == "nt", reason="POSIX zombie semantics")
    def test_an_exited_child_of_this_process_does_not_hold_the_lock(self, tmp_path):
        """The daemon spawns analyze and never waits for it: once it exits
        it is a zombie, which kill(pid, 0) still reports as alive."""
        import subprocess as sp
        import time as _t
        pid = sp.Popen(["true"], stdin=sp.DEVNULL, stdout=sp.DEVNULL,
                       stderr=sp.DEVNULL, start_new_session=True).pid
        _t.sleep(0.5)  # exited, not waited for
        (tmp_path / gn._LOCK_FILENAME).write_text(f"{pid}\n0", encoding="utf-8")
        assert gn._acquire_lock(tmp_path, min_age_seconds=0)

    def test_a_pid_recorded_hours_ago_is_not_trusted(self, tmp_path):
        """A reused PID (reboot, wrap) must not hold the lock forever."""
        old = int(gn.time.time()) - cr._LOCK_PID_MAX_AGE_SECONDS - 60
        (tmp_path / gn._LOCK_FILENAME).write_text(f"{os.getpid()}\n{old}",
                                                  encoding="utf-8")
        assert gn._acquire_lock(tmp_path, min_age_seconds=60)

    def test_no_op_when_project_not_indexed(self, monkeypatch, tmp_path):
        monkeypatch.setattr(gn.shutil, "which",
                            lambda name: "/usr/bin/gitnexus" if name == "gitnexus" else None)
        (tmp_path / ".git").mkdir()  # is a project root, but no .gitnexus/
        spawned = []
        monkeypatch.setattr(gn.subprocess, "Popen",
                            lambda *a, **kw: spawned.append(a))
        gn.reindex_if_dirty_async(cwd=str(tmp_path), turn_modified=True)
        assert spawned == []

    def test_never_raises_on_garbage_cwd(self):
        # Exercises the outer try/except
        gn.reindex_if_dirty_async(cwd="", turn_modified=True)
        gn.reindex_if_dirty_async(cwd="/no/such/path/at/all", turn_modified=True)


# ---------------------------------------------------------------------------
# CLI: code_graph companions
# ---------------------------------------------------------------------------

class TestCompanionsCli:
    def test_companions_prints_status(self, tmp_path):
        repo_root = Path(__file__).resolve().parent.parent
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(repo_root), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
        # Need a git dir for project_root() to work
        subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
        out = subprocess.run(
            [sys.executable, "-m", "claude_hooks.code_graph",
             "companions", "--root", str(tmp_path)],
            capture_output=True, text=True, timeout=10,
            cwd=str(tmp_path), env=env,
        )
        assert out.returncode == 0, out.stderr
        import json
        report = json.loads(out.stdout)
        assert "code_graph" in report
        assert "gitnexus" in report


# ---------------------------------------------------------------------------
# Broken-index recovery (opencoti, 2026-10-01)
# ---------------------------------------------------------------------------

import os as _os
import time as _time


def _indexed_repo(tmp_path, *, db_mtime=None, recovery_mtime=None):
    (tmp_path / ".git").mkdir()
    idx = tmp_path / ".gitnexus"
    idx.mkdir()
    db = idx / "lbug"
    db.write_bytes(b"db")
    if db_mtime is not None:
        _os.utime(db, (db_mtime, db_mtime))
    if recovery_mtime is not None:
        rec = idx / "lbug.shadow.dirty-recovery"
        rec.write_bytes(b"r")
        _os.utime(rec, (recovery_mtime, recovery_mtime))
    return tmp_path


class TestIndexDirty:
    def test_a_recovery_file_newer_than_the_database_is_dirty(self, tmp_path):
        now = _time.time()
        root = _indexed_repo(tmp_path, db_mtime=now - 100,
                             recovery_mtime=now - 10)
        assert gn.index_dirty(root)

    def test_a_stale_recovery_file_is_not(self, tmp_path):
        """A later successful analyze rewrites lbug but leaves the old
        recovery file in place — opencoti had one from 09:49 beside an
        lbug rewritten at 12:26."""
        now = _time.time()
        root = _indexed_repo(tmp_path, db_mtime=now - 10,
                             recovery_mtime=now - 100)
        assert not gn.index_dirty(root)

    def test_no_recovery_file_is_clean(self, tmp_path):
        assert not gn.index_dirty(_indexed_repo(tmp_path))


class TestSupervise:
    def test_success_records_ok(self, tmp_path):
        root = _indexed_repo(tmp_path)
        st = gn.supervise_analyze("gn", root, run_fn=lambda b, r, o: 0,
                                  sleep_fn=lambda s: None)
        assert st["ok"] and st["attempts"] == 1
        assert gn.read_reindex_status(root)["ok"] is True
        assert not gn.index_broken(root)

    def test_a_failure_is_retried_then_recorded(self, tmp_path):
        root = _indexed_repo(tmp_path)
        rcs = iter([139, 139, 0])
        st = gn.supervise_analyze("gn", root,
                                  run_fn=lambda b, r, o: next(rcs),
                                  sleep_fn=lambda s: None)
        assert st["ok"] and st["attempts"] == 3

    def test_all_attempts_failing_marks_the_index_broken(self, tmp_path):
        root = _indexed_repo(tmp_path)

        def crash(b, r, out):
            out.write(b"Segmentation fault")
            return 139

        st = gn.supervise_analyze("gn", root, run_fn=crash,
                                  sleep_fn=lambda s: None)
        assert not st["ok"] and st["returncode"] == 139
        assert st["consecutive_failures"] == 3
        assert "Segmentation fault" in st["log_tail"]
        assert gn.index_broken(root)

    def test_exit_zero_with_a_dirty_database_is_a_failure(self, tmp_path):
        root = _indexed_repo(tmp_path)
        rec = root / ".gitnexus" / "lbug.shadow.dirty-recovery"

        def leaves_dirty(b, r, out):
            rec.write_bytes(b"r")
            future = _time.time() + 60
            _os.utime(rec, (future, future))
            return 0

        st = gn.supervise_analyze("gn", root, run_fn=leaves_dirty,
                                  attempts=1, sleep_fn=lambda s: None)
        assert not st["ok"] and st["dirty"]


class TestBrokenIndexRebuild:
    def _spawn_counter(self, monkeypatch):
        monkeypatch.setattr(gn.shutil, "which",
                            lambda n: "/usr/bin/gitnexus" if n == "gitnexus" else None)
        spawned = []
        monkeypatch.setattr(gn.subprocess, "Popen",
                            lambda *a, **kw: spawned.append(a))
        return spawned

    def test_a_dirty_index_is_rebuilt_without_an_edit(self, monkeypatch, tmp_path):
        spawned = self._spawn_counter(monkeypatch)
        now = _time.time()
        root = _indexed_repo(tmp_path, db_mtime=now - 100,
                             recovery_mtime=now - 10)
        gn.reindex_if_dirty_async(cwd=str(root), turn_modified=False)
        assert len(spawned) == 1

    def test_a_clean_index_is_left_alone_without_an_edit(self, monkeypatch, tmp_path):
        spawned = self._spawn_counter(monkeypatch)
        gn.reindex_if_dirty_async(cwd=str(_indexed_repo(tmp_path)),
                                  turn_modified=False)
        assert spawned == []

    def test_a_deterministic_failure_backs_off(self, monkeypatch, tmp_path):
        spawned = self._spawn_counter(monkeypatch)
        root = _indexed_repo(tmp_path)
        gn._write_reindex_status(root, {"ok": False, "returncode": 1,
                                        "finished_at": _time.time()})
        gn.reindex_if_dirty_async(cwd=str(root), turn_modified=True)
        assert spawned == []
        gn._write_reindex_status(root, {"ok": False, "returncode": 1,
                                        "finished_at": _time.time() - 3600})
        gn.reindex_if_dirty_async(cwd=str(root), turn_modified=True)
        assert len(spawned) == 1

    def test_the_hint_says_the_index_is_broken(self, tmp_path):
        root = _indexed_repo(tmp_path)
        gn._write_reindex_status(root, {"ok": False, "returncode": 139,
                                        "finished_at": _time.time()})
        hint = gn.session_start_hint(root)
        assert "broken" in hint and "rc=139" in hint
        assert "not run" in hint


class TestAnalyzeArgv:
    def test_a_background_rebuild_never_edits_docs_or_skills(self):
        """A bare analyze writes into AGENTS.md / CLAUDE.md and installs
        skills; the hook's rebuild must only refresh the index."""
        assert gn._analyze_argv("gn") == ["gn", "analyze", "--index-only"]

    def test_the_supervisor_runs_that_argv(self, monkeypatch, tmp_path):
        root = _indexed_repo(tmp_path)
        seen = []

        class _CP:
            returncode = 0

        monkeypatch.setattr(gn.subprocess, "run",
                            lambda argv, **kw: seen.append(argv) or _CP())
        assert gn.supervise_analyze("gn", root, sleep_fn=lambda s: None)["ok"]
        assert seen == [["gn", "analyze", "--index-only"]]
