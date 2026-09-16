"""The MCP and the hooks must use one engine per project.

The MCP server used to build an ``Engine`` in-process while the
PostToolUse hook talked to the daemon's. Two engines per project meant
two fleets of language servers indexing the same tree, two warm-ups, and
two diagnostic caches free to disagree about the same file — on the
development host, 12 servers across three fleets holding 357 MB.

These pin the seam: the daemon can serve navigation, and the MCP's
engine object is a client of it rather than an owner of servers.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from claude_hooks.lsp_engine import wire  # noqa: E402
from claude_hooks.lsp_engine.daemon import Daemon  # noqa: E402
from claude_hooks.lsp_engine.engine import NavResponse  # noqa: E402
from claude_hooks.lsp_engine.protocol import (  # noqa: E402
    Location, Position, Range,
)
from claude_hooks.lsp_mcp.server import DaemonEngine  # noqa: E402


def _loc(line=1):
    return Location(uri="file:///x.py",
                    range=Range(start=Position(line=line, character=0),
                                end=Position(line=line, character=3)))


class _FakeEngine:
    def __init__(self, res=None):
        self.res = res if res is not None else NavResponse()
        self.calls: list[tuple] = []

    def references(self, path, line, character, **kw):
        self.calls.append(("references", path, line, character, kw))
        return self.res

    def restart(self, exts=None):
        self.calls.append(("restart", exts))
        return ["pyright-langserver"]


class DaemonNavOpTests(unittest.TestCase):
    """The daemon serves navigation, with provenance intact."""

    def daemon(self, engine):
        d = Daemon.__new__(Daemon)
        d._engine = engine
        return d

    def test_nav_op_returns_items_and_provenance(self) -> None:
        eng = _FakeEngine(NavResponse(
            items=[_loc(4)], consulted=("pyright-langserver",),
            failures=(("gopls", "timeout"),)))
        d = self.daemon(eng)
        resp = d._op_nav(1, {"method": "references",
                             "args": {"path": "/x.py", "line": 0,
                                      "character": 1}})
        self.assertTrue(resp["ok"])
        got = wire.nav_from_json(resp["nav"])
        self.assertEqual(got.items, [_loc(4)])
        # The failure has to survive, or an empty result on the other
        # side would read as a fact about the code.
        self.assertEqual(got.failures, (("gopls", "timeout"),))
        self.assertFalse(got.trustworthy)

    def test_unknown_method_is_refused(self) -> None:
        d = self.daemon(_FakeEngine())
        resp = d._op_nav(1, {"method": "shutdown", "args": {}})
        self.assertFalse(resp["ok"])
        self.assertIn("unknown nav method", resp["error"])

    def test_the_table_is_the_whitelist(self) -> None:
        # Anything not named here is unreachable over the socket, so a
        # malformed request cannot call arbitrary engine methods.
        for bad in ("did_close", "shutdown", "seed_workspace", "__init__"):
            with self.subTest(method=bad):
                self.assertNotIn(bad, Daemon._NAV_METHODS)

    def test_bad_arguments_report_rather_than_crash(self) -> None:
        d = self.daemon(_FakeEngine())
        resp = d._op_nav(1, {"method": "references", "args": {"nope": 1}})
        self.assertFalse(resp["ok"])
        self.assertIn("bad arguments", resp["error"])

    def test_restart_op(self) -> None:
        d = self.daemon(_FakeEngine())
        resp = d._op_restart(1, {"extensions": ["py"]})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["restarted"], ["pyright-langserver"])


class _FakeClient:
    def __init__(self):
        self.navs: list[tuple] = []
        self.detached = False
        self.closed = False

    def nav(self, method, **args):
        self.navs.append((method, args))
        return NavResponse(items=[_loc()], consulted=("pyright",))

    def detach(self):
        self.detached = True

    def close(self):
        self.closed = True


class DaemonEngineProxyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = _FakeClient()
        self.eng = DaemonEngine(Path("/proj"), self.client)

    def test_navigation_goes_over_the_socket(self) -> None:
        self.eng.references(Path("/proj/x.py"), 3, 4)
        method, args = self.client.navs[-1]
        self.assertEqual(method, "references")
        # Paths must be strings on the wire.
        self.assertIsInstance(args["path"], str)
        self.assertEqual(args["line"], 3)

    def test_the_proxy_mirrors_the_engine_surface(self) -> None:
        # The tool layer calls these by name and must not know it is
        # talking to a socket.
        for name in ("find_symbols", "definition", "implementation",
                     "references", "hover", "prepare_call_hierarchy",
                     "calls", "rename", "workspace_symbols", "did_open",
                     "get_diagnostics_result", "restart", "shutdown"):
            with self.subTest(method=name):
                self.assertTrue(callable(getattr(self.eng, name, None)))

    def test_shutdown_detaches_and_does_not_stop_the_daemon(self) -> None:
        # The daemon is shared with the PostToolUse hook and every other
        # session in the project; reaping our handle must not take their
        # language servers down.
        self.eng.shutdown()
        self.assertTrue(self.client.detached)
        self.assertTrue(self.client.closed)
        self.assertFalse(hasattr(self.client, "shutdown_called"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
