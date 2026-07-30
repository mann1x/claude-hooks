"""Post-deploy verification checks (``scripts/verify_deploy.py``).

The store check earns its place by catching a class of failure that the
old manual "open a session and read the log" verification cannot: a
config that is *present and parseable* but points somewhere useless.

It also encodes the mistake that motivated it. On 2026-07-30 a host was
reported as having an unset store backend because the block was read
from ``config/claude-hooks.json`` — where it returns ``None`` on every
host — and then looked for under the per-project filename in the
user-global location. The store was fine the whole time. Two guards
below pin that down: the check must resolve config through the real
loader, and it must report *which* file it used.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

_spec = importlib.util.spec_from_file_location(
    "verify_deploy", REPO / "scripts" / "verify_deploy.py")
vd = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(vd)


def _store(**kw):
    base = dict(enabled=True, backend="pgvector",
                pgvector_dsn="postgresql://u:p@h:5432/db",
                pgvector_table="consultants_store",
                sqlite_vec_path="~/.claude/consultants-store.db",
                enable_at_efforts=("high", "max", "xhigh"))
    base.update(kw)
    return SimpleNamespace(**base)


def _results():
    r = vd.Results(quiet=True)
    return r


def _statuses(r):
    return {name: status for status, name, _ in r.rows}


def _detail(r, name):
    for _, n, d in r.rows:
        if n == name:
            return d
    return ""


class TestStoreBackendReachability(unittest.TestCase):
    def test_memory_backend_warns_about_volatility(self):
        r = _results()
        vd._check_store_backend(r, _store(backend="memory"), "memory")
        self.assertEqual(_statuses(r)["store backend reachable"], vd.WARN)
        self.assertIn("nothing persists", _detail(r, "store backend reachable"))

    def test_pgvector_without_dsn_fails(self):
        r = _results()
        vd._check_store_backend(r, _store(pgvector_dsn=None), "pgvector")
        self.assertEqual(_statuses(r)["store backend reachable"], vd.FAIL)

    def test_sqlite_without_path_fails(self):
        r = _results()
        vd._check_store_backend(r, _store(sqlite_vec_path=None), "sqlite_vec")
        self.assertEqual(_statuses(r)["store backend reachable"], vd.FAIL)

    def test_sqlite_missing_file_warns_not_fails(self):
        """The DB is created on first write, so absence is not yet an
        error — but it must still be surfaced."""
        r = _results()
        vd._check_store_backend(
            r, _store(backend="sqlite_vec",
                      sqlite_vec_path="/nonexistent/nope.db"), "sqlite_vec")
        self.assertEqual(_statuses(r)["store backend reachable"], vd.WARN)

    def test_sqlite_existing_file_passes(self):
        import sqlite3
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.db"
            con = sqlite3.connect(p)
            con.execute("CREATE TABLE t (a int)")
            con.commit()
            con.close()
            r = _results()
            vd._check_store_backend(
                r, _store(backend="sqlite_vec", sqlite_vec_path=str(p)),
                "sqlite_vec")
        self.assertEqual(_statuses(r)["store backend reachable"], vd.PASS)

    def test_unknown_backend_warns(self):
        r = _results()
        vd._check_store_backend(r, _store(backend="weaviate"), "weaviate")
        self.assertEqual(_statuses(r)["store backend reachable"], vd.WARN)


class TestStoreConfigChecks(unittest.TestCase):
    def _run_with(self, cfg, user_exists=True, proj_exists=False):
        """Drive check_store against a stubbed consultants.config."""
        import consultants.config as cc

        class _P:
            def __init__(self, exists, name):
                self._e = exists
                self._n = name

            def exists(self):
                return self._e

            def __str__(self):
                return self._n

        orig = (cc.load_config, cc.user_config_path, cc.project_config_path)
        cc.load_config = lambda *a, **k: cfg
        cc.user_config_path = lambda *a, **k: _P(user_exists, "USER.toml")
        cc.project_config_path = lambda *a, **k: _P(proj_exists, "PROJ.toml")
        try:
            r = _results()
            vd.check_store(r)
            return r
        finally:
            cc.load_config, cc.user_config_path, cc.project_config_path = orig

    def test_disabled_store_warns(self):
        r = self._run_with(SimpleNamespace(store=_store(enabled=False),
                                           effort="xhigh"))
        self.assertEqual(_statuses(r)["store.enabled"], vd.WARN)

    def test_enabled_without_backend_fails(self):
        r = self._run_with(SimpleNamespace(store=_store(backend=None),
                                           effort="xhigh"))
        self.assertEqual(_statuses(r)["store.backend"], vd.FAIL)

    def test_missing_store_section_fails(self):
        r = self._run_with(SimpleNamespace(store=None, effort="xhigh"))
        self.assertEqual(_statuses(r)["store block"], vd.FAIL)

    def test_effort_outside_gate_warns(self):
        """A perfectly configured store still does nothing when the
        host's default effort sits outside enable_at_efforts."""
        r = self._run_with(SimpleNamespace(store=_store(), effort="medium"))
        self.assertEqual(_statuses(r)["store effort gate"], vd.WARN)
        self.assertIn("not wired", _detail(r, "store effort gate"))

    def test_effort_inside_gate_passes(self):
        r = self._run_with(SimpleNamespace(store=_store(), effort="xhigh"))
        self.assertEqual(_statuses(r)["store effort gate"], vd.PASS)

    def test_reports_which_config_file_was_used(self):
        """The false alarm came from guessing the filename. The check
        must say which file it actually read."""
        r = self._run_with(SimpleNamespace(store=_store(), effort="xhigh"),
                           user_exists=True, proj_exists=True)
        detail = _detail(r, "store config file")
        self.assertIn("USER.toml", detail)
        self.assertIn("PROJ.toml", detail)

    def test_no_config_file_warns_that_defaults_are_in_play(self):
        r = self._run_with(SimpleNamespace(store=_store(), effort="xhigh"),
                           user_exists=False, proj_exists=False)
        self.assertEqual(_statuses(r)["store config file"], vd.WARN)
        self.assertIn("defaults", _detail(r, "store config file"))


class TestDoesNotReadClaudeHooksJson(unittest.TestCase):
    def test_store_check_never_consults_claude_hooks_json(self):
        """THE regression guard. ``claude-hooks.json`` has no ``store``
        key on any host, so reading it there yields a false negative
        that looks exactly like a real misconfiguration."""
        src = (REPO / "scripts" / "verify_deploy.py").read_text()
        store_src = src[src.index("def check_store"):src.index("def check_providers")]
        self.assertNotIn("claude-hooks.json", store_src)
        self.assertNotIn("claude_hooks.config", store_src)
        self.assertIn("consultants.config", store_src)


class TestResultsAccounting(unittest.TestCase):
    def test_exit_code_tracks_failures_only(self):
        r = _results()
        r.add(vd.PASS, "a")
        r.add(vd.WARN, "b")
        self.assertEqual(r.failed, 0)
        r.add(vd.FAIL, "c")
        self.assertEqual(r.failed, 1)


if __name__ == "__main__":
    unittest.main()
