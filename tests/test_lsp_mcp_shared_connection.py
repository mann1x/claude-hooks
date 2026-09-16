"""Two packages in one repository are one daemon connection.

Introduced by moving the daemon out to the repository boundary. The MCP
registry keys its entries on the *narrow* root, because that is what a
result's scope warning and its relative paths are about — but every one
of those roots now resolves to the same socket, and every client in this
process attaches with the same session id, ``lsp-mcp-<pid>``.

That makes a second connection actively harmful rather than merely
wasteful. The daemon holds attached sessions in a set and releases a
session's file locks on detach, so the first package reaped would drop
the locks of every other package in the repository, and the daemon has
no way to tell that from the session ending. One connection, refcounted,
with the *last* holder detaching.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from claude_hooks.lsp_mcp import server as S  # noqa: E402


class _FakeClient:
    """Counts the lifecycle calls the daemon would refcount."""

    def __init__(self) -> None:
        self.detached = 0
        self.closed = 0

    def status(self):
        return {"cclsp_config": None}

    def take_stale_notice(self):
        return None

    def detach(self) -> None:
        self.detached += 1

    def close(self) -> None:
        self.closed += 1


class SharedConnectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name).resolve() / "repo"
        (self.repo / ".git").mkdir(parents=True)
        (self.repo / "cclsp.json").write_text(
            '{"servers": [{"extensions": ["ts"], "command": ["fake-ls"]}]}',
            encoding="utf-8")
        self.a = self.repo / "packages" / "a"
        self.b = self.repo / "packages" / "b"
        for pkg in (self.a, self.b):
            (pkg / "src").mkdir(parents=True)
            (pkg / "package.json").write_text("{}", encoding="utf-8")
            (pkg / "src" / "index.ts").write_text("x\n", encoding="utf-8")

        self.clients: list[_FakeClient] = []

        def fake_connect(**kw):
            c = _FakeClient()
            self.clients.append(c)
            return c

        self._orig = S.connect_or_spawn
        S.connect_or_spawn = fake_connect
        self.addCleanup(lambda: setattr(S, "connect_or_spawn", self._orig))
        self.registry = S.EngineRegistry()

    def _entry(self, pkg: Path):
        return self.registry.for_path(pkg / "src" / "index.ts")

    def test_two_packages_open_one_connection(self) -> None:
        a, b = self._entry(self.a), self._entry(self.b)
        self.assertEqual(len(self.clients), 1)
        self.assertIs(a.engine, b.engine)

    def test_each_entry_keeps_its_own_narrow_root(self) -> None:
        # The connection is shared; the scope a result is reported
        # against is not. Collapsing them would make a package-bounded
        # search claim it covered the repository.
        a, b = self._entry(self.a), self._entry(self.b)
        self.assertEqual(a.root, self.a)
        self.assertEqual(b.root, self.b)

    def test_reaping_one_package_does_not_detach_the_session(self) -> None:
        # The bug: one detach releases the whole session's locks, and
        # the daemon cannot tell that from the session ending.
        self._entry(self.a)
        self._entry(self.b)
        self.registry._drop(self.a)
        self.assertEqual(self.clients[0].detached, 0)

    def test_the_last_holder_detaches(self) -> None:
        self._entry(self.a)
        self._entry(self.b)
        self.registry._drop(self.a)
        self.registry._drop(self.b)
        self.assertEqual(self.clients[0].detached, 1)
        self.assertEqual(self.clients[0].closed, 1)

    def test_a_package_reopened_after_the_last_drop_reconnects(self) -> None:
        # Handing back a detached client would fail every later call
        # with a closed socket rather than reconnecting.
        self._entry(self.a)
        self.registry._drop(self.a)
        again = self._entry(self.a)
        self.assertEqual(len(self.clients), 2)
        self.assertFalse(again.engine.closed)

    def test_shutdown_all_detaches_exactly_once(self) -> None:
        self._entry(self.a)
        self._entry(self.b)
        self.registry.shutdown_all()
        self.assertEqual(self.clients[0].detached, 1)

    def test_separate_repositories_get_separate_connections(self) -> None:
        other = Path(self.tmp.name).resolve() / "other"
        (other / ".git").mkdir(parents=True)
        (other / "cclsp.json").write_text(
            '{"servers": [{"extensions": ["ts"], "command": ["fake-ls"]}]}',
            encoding="utf-8")
        (other / "src").mkdir()
        (other / "src" / "index.ts").write_text("x\n", encoding="utf-8")
        self._entry(self.a)
        self.registry.for_path(other / "src" / "index.ts")
        self.assertEqual(len(self.clients), 2)

    def test_a_stale_notice_is_not_consumed_by_one_package(self) -> None:
        # take_notices is once-only. Asking the shared connection once
        # per entry would give the notice to the first package and
        # nothing to the rest, reading as "only that one is stale".
        a = self._entry(self.a)
        self._entry(self.b)
        a.engine.add_notice("DAEMON-STALE")
        self.assertEqual(self.registry.drain_stale_notices(),
                         ["DAEMON-STALE"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
