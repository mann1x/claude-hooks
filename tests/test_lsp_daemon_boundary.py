"""The daemon is keyed at the repository; its engines are not.

Two roots, two questions. A language server has to be rooted narrowly
or it answers nothing useful — tsserver at a 6.1 GB monorepo measured 0
references in 81.7 s. A *daemon* rooted that narrowly is a process per
package, and on the cline checkout that is 30 of them for one repo, each
with a socket, a lock file, a sweeper thread and a fleet.

The failure mode this pins is not the count itself but the way the two
roots silently disagree. ``Daemon.__init__`` normalises its own root,
and ``load_daemon_config`` did not: the daemon then reported the right
``cclsp_config`` in status while holding zero servers, because the
config had been read from ``…/messages.ts/cclsp.json``. Measured on
cline before the fix, and the strict half looked like the bug.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from claude_hooks.lsp_engine.daemon import (  # noqa: E402
    Daemon,
    daemon_root_for,
    project_dir,
    socket_path_for,
)
from claude_hooks.lsp_engine.engine import NavResponse  # noqa: E402
from claude_hooks.lsp_engine.protocol import (  # noqa: E402
    Location,
    Position,
    Range,
    Symbol,
)
from claude_hooks.lsp_engine.pool import EnginePool  # noqa: E402


class _FakeEngine:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.opened: list[str] = []
        self.closed: list[str] = []
        self.restarted = 0

    def did_open(self, path, content) -> bool:
        self.opened.append(str(path))
        return True

    def did_change(self, path, content) -> bool:
        return True

    def did_close(self, path) -> bool:
        self.closed.append(str(path))
        return True

    def _range(self):
        return Range(start=Position(line=0, character=0),
                     end=Position(line=0, character=1))

    def references(self, path, line, character):
        # A real Location so the wire codec is exercised too: a marker
        # object would let an encoding regression through.
        return NavResponse(
            items=[Location(uri=self.root.as_uri(), range=self._range())],
            consulted=("fake-ls",))

    def workspace_symbols(self, query, start_all=False):
        return NavResponse(
            items=[Symbol(name=self.root.name, kind=12,
                          uri=self.root.as_uri(), range=self._range(),
                          selection=self._range())],
            consulted=("fake-ls",))

    def restart(self, exts=None) -> list[str]:
        self.restarted += 1
        return [f"fake-ls@{self.root.name}"]

    def open_files(self) -> list[str]:
        return list(self.opened)

    def active_servers(self):
        return []

    def support_report(self):
        return []

    def shutdown(self, *, timeout: float = 3.0) -> None:
        pass


class _Locks:
    def did_change(self, session, path, content):
        return True, []

    def query(self, session, path, timeout_ms=0):
        return True, []

    def held_uris(self):
        return []


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name).resolve() / "repo"
        (self.repo / ".git").mkdir(parents=True)
        self.a = self.repo / "packages" / "a"
        self.b = self.repo / "packages" / "b"
        for pkg in (self.a, self.b):
            (pkg / "src").mkdir(parents=True)
            (pkg / "package.json").write_text("{}", encoding="utf-8")
            (pkg / "src" / "index.ts").write_text("x\n", encoding="utf-8")

    def daemon(self) -> Daemon:
        d = Daemon.__new__(Daemon)
        d._project_root = self.repo
        d._lock_manager = _Locks()
        d._compile = None
        d._served = {}
        d._sessions_lock = __import__("threading").Lock()
        d._attached_sessions = set()
        d._cclsp_config_path = self.repo / "cclsp.json"
        d._engine_config = type("C", (), {
            "session_locks": type("S", (), {"query_timeout_ms": 500})()})()
        self.built: dict[Path, _FakeEngine] = {}

        def factory(root: Path) -> _FakeEngine:
            eng = _FakeEngine(root)
            self.built[root] = eng
            return eng

        d._pool = EnginePool(self.repo, [], factory=factory)
        return d


class AddressKeyingTests(_Fixture):
    def test_every_path_in_the_repo_maps_to_one_daemon(self) -> None:
        # The identity the client and the daemon must agree on.
        for p in (self.repo, self.a, self.b / "src" / "index.ts"):
            self.assertEqual(daemon_root_for(p), self.repo)

    def test_one_socket_for_the_whole_repository(self) -> None:
        base = Path(self.tmp.name) / "state"
        first = socket_path_for(self.a / "src" / "index.ts", base=base)
        second = socket_path_for(self.b, base=base)
        self.assertEqual(str(first), str(second))

    def test_a_file_resolves_to_its_repository_not_itself(self) -> None:
        # The bug found live: a file path let through as a root, so the
        # daemon looked for ``index.ts/cclsp.json`` and found no servers
        # while reporting the repository's config path in status.
        f = self.a / "src" / "index.ts"
        self.assertEqual(daemon_root_for(f), self.repo)

    def test_a_path_that_does_not_exist_is_left_alone(self) -> None:
        # Walking up from a phantom path reaches the process's cwd and
        # lands on whatever repository the caller is sitting in.
        ghost = self.repo / "nope" / "gone.ts"
        self.assertEqual(daemon_root_for(ghost), ghost)

    def test_state_dir_is_shared_across_packages(self) -> None:
        base = Path(self.tmp.name) / "state"
        self.assertEqual(project_dir(self.a, base=base),
                         project_dir(self.b, base=base))


class RoutingTests(_Fixture):
    def test_an_edit_reaches_its_own_package(self) -> None:
        d = self.daemon()
        f = self.a / "src" / "index.ts"
        d._op_did_open(1, "sess", {"path": str(f), "content": "x\n"})
        self.assertIn(self.a, self.built)
        self.assertNotIn(self.b, self.built)
        self.assertEqual(self.built[self.a].opened, [str(f)])

    def test_navigation_asks_the_owning_package(self) -> None:
        d = self.daemon()
        f = self.b / "src" / "index.ts"
        resp = d._op_nav(1, {"method": "references",
                             "args": {"path": str(f), "line": 0,
                                      "character": 0}})
        self.assertTrue(resp["ok"])
        self.assertEqual([i["uri"] for i in resp["nav"]["items"]],
                         [self.b.as_uri()])

    def test_closing_a_file_starts_nothing(self) -> None:
        # Starting a fleet in order to say a file is closed would be an
        # expensive way to do nothing.
        d = self.daemon()
        resp = d._op_did_close(
            1, "sess", {"path": str(self.a / "src" / "index.ts")})
        self.assertTrue(resp["ok"])
        self.assertFalse(resp["closed"])
        self.assertEqual(self.built, {})

    def test_a_workspace_query_spans_the_live_packages(self) -> None:
        d = self.daemon()
        for pkg in (self.a, self.b):
            d._op_did_open(1, "sess", {"path": str(pkg / "src" / "index.ts"),
                                       "content": "x\n"})
        resp = d._op_nav(1, {"method": "workspace_symbols",
                             "args": {"query": "thing"}})
        self.assertTrue(resp["ok"])
        self.assertEqual(sorted(i["uri"] for i in resp["nav"]["items"]),
                         sorted([self.a.as_uri(), self.b.as_uri()]))

    def test_a_workspace_query_with_nothing_running_still_answers(self) -> None:
        d = self.daemon()
        resp = d._op_nav(1, {"method": "workspace_symbols",
                             "args": {"query": "thing"}})
        self.assertTrue(resp["ok"])
        self.assertEqual([i["uri"] for i in resp["nav"]["items"]],
                         [self.repo.as_uri()])

    def test_status_names_every_live_engine(self) -> None:
        d = self.daemon()
        for pkg in (self.a, self.b):
            d._op_did_open(1, "sess", {"path": str(pkg / "src" / "index.ts"),
                                       "content": "x\n"})
        st = d._op_status(1)
        self.assertEqual(st["project"], str(self.repo))
        self.assertEqual(sorted(e["root"] for e in st["engines"]),
                         sorted([str(self.a), str(self.b)]))


class MergeProvenanceTests(unittest.TestCase):
    """Merging must not launder a partial answer into a clean one."""

    def test_a_failure_in_one_package_survives_the_merge(self) -> None:
        from claude_hooks.lsp_engine.engine import merge_nav
        good = NavResponse(items=[1], consulted=("tsserver",))
        bad = NavResponse(items=[], failures=(("gopls", "timeout"),))
        merged = merge_nav([good, bad])
        self.assertEqual(merged.items, [1])
        self.assertEqual(merged.failures, (("gopls", "timeout"),))
        self.assertFalse(merged.trustworthy)

    def test_all_clean_stays_trustworthy(self) -> None:
        from claude_hooks.lsp_engine.engine import merge_nav
        merged = merge_nav([
            NavResponse(items=[1], consulted=("tsserver",)),
            NavResponse(items=[2], consulted=("pyright-langserver",)),
        ])
        self.assertEqual(merged.items, [1, 2])
        self.assertTrue(merged.trustworthy)
        self.assertEqual(merged.consulted,
                         ("tsserver", "pyright-langserver"))

    def test_a_truncated_scan_is_carried_by_the_widest(self) -> None:
        from claude_hooks.lsp_engine.engine import merge_nav
        merged = merge_nav([
            NavResponse(items=[], consulted=("a",), scan_truncated_at=100),
            NavResponse(items=[], consulted=("b",), scan_truncated_at=500),
        ])
        self.assertEqual(merged.scan_truncated_at, 500)

    def test_a_single_part_is_returned_unchanged(self) -> None:
        from claude_hooks.lsp_engine.engine import merge_nav
        one = NavResponse(items=[1], consulted=("tsserver",))
        self.assertIs(merge_nav([one]), one)


class ReloadTests(_Fixture):
    """The lifecycle gap: a running server holds the config it started with."""

    def test_reload_stops_every_engine(self) -> None:
        d = self.daemon()
        for pkg in (self.a, self.b):
            d._op_did_open(1, "sess", {"path": str(pkg / "src" / "index.ts"),
                                       "content": "x\n"})
        resp = d._op_reload(1, {"config": False})
        self.assertTrue(resp["ok"])
        self.assertEqual(sorted(resp["stopped"]),
                         sorted([str(self.a), str(self.b)]))
        self.assertEqual(d._pool.live_roots(), [])

    def test_engines_rebuild_after_a_reload(self) -> None:
        d = self.daemon()
        f = self.a / "src" / "index.ts"
        d._op_did_open(1, "sess", {"path": str(f), "content": "x\n"})
        first = self.built[self.a]
        d._op_reload(1, {"config": False})
        d._op_did_open(1, "sess", {"path": str(f), "content": "x\n"})
        self.assertIsNot(self.built[self.a], first)

    def test_reload_re_reads_the_config(self) -> None:
        (self.repo / "cclsp.json").write_text(
            '{"servers": [{"extensions": ["ts"], "command": ["fake-ls"]}]}',
            encoding="utf-8")
        d = self.daemon()
        resp = d._op_reload(1, {})
        self.assertTrue(resp["ok"])
        self.assertTrue(resp["reloaded_config"])
        self.assertEqual(resp["cclsp_config"], str(self.repo / "cclsp.json"))

    def test_a_broken_config_is_reported_not_swallowed(self) -> None:
        # Adopting nothing and saying "reloaded" would leave a daemon
        # that answers every question with silence and claims health.
        (self.repo / "cclsp.json").write_text("{not json", encoding="utf-8")
        d = self.daemon()
        resp = d._op_reload(1, {})
        self.assertFalse(resp["ok"])
        self.assertIn("config reload failed", resp["error"])

    def test_reload_is_reachable_over_the_wire(self) -> None:
        d = self.daemon()
        resp = d._dispatch_request({"id": 7, "op": "reload",
                                    "session": "s", "config": False})
        self.assertTrue(resp["ok"])

    def test_restart_covers_every_live_package(self) -> None:
        d = self.daemon()
        for pkg in (self.a, self.b):
            d._op_did_open(1, "sess", {"path": str(pkg / "src" / "index.ts"),
                                       "content": "x\n"})
        resp = d._op_restart(1, {"extensions": ["ts"]})
        self.assertTrue(resp["ok"])
        self.assertEqual(sorted(resp["restarted"]),
                         ["fake-ls@a", "fake-ls@b"])


class NoBoundaryAtAllTests(unittest.TestCase):
    """A path with nothing above it still keys a daemon on a directory.

    Reached on a real tree, not invented: ``backup_models/manic-harness``
    is a symlink onto another disk, so resolving a file under it escapes
    the declared root and there is no repository, sentinel or package
    marker anywhere above what is left. ``boundary_root_for`` correctly
    returns None — and the fallback then handed back the *file*, so 179
    files each keyed a daemon on themselves and looked for
    ``<file>.py/cclsp.json``.

    That is the same failure the existence guard in ``daemon_root_for``
    was added for, arriving through a different door, which is why the
    fallback is pinned here rather than left to the guard.
    """

    def test_a_file_with_no_boundary_keys_on_its_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # No .git, no sentinel, no package marker anywhere inside.
            leaf = Path(tmp).resolve() / "nested" / "deep"
            leaf.mkdir(parents=True)
            src = leaf / "script.py"
            src.write_text("x = 1\n", encoding="utf-8")

            from claude_hooks.lsp_engine.config import boundary_root_for
            if boundary_root_for(src) is not None:
                self.skipTest("temp dir sits inside a repository")

            root = daemon_root_for(src)
            self.assertTrue(Path(root).is_dir(),
                            f"daemon keyed on a file: {root}")
            self.assertEqual(Path(root), leaf)

    def test_a_directory_with_no_boundary_keys_on_itself(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            leaf = Path(tmp).resolve() / "nested"
            leaf.mkdir()
            from claude_hooks.lsp_engine.config import boundary_root_for
            if boundary_root_for(leaf) is not None:
                self.skipTest("temp dir sits inside a repository")
            self.assertEqual(Path(daemon_root_for(leaf)), leaf)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
