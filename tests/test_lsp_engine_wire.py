"""The daemon's navigation wire format must not lose anything.

The MCP server used to build its own Engine, so navigation never had to
cross a process boundary. Now it does, and a field dropped here does not
fail — it produces a navigation result that is subtly wrong, which is
the failure mode this engine exists to avoid. So every field round-trips
through json.dumps, provenance included.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from claude_hooks.lsp_engine import protocol as P  # noqa: E402
from claude_hooks.lsp_engine import wire as W  # noqa: E402
from claude_hooks.lsp_engine.engine import NavResponse  # noqa: E402


def _range(a=1, b=2, c=3, d=4) -> P.Range:
    return P.Range(start=P.Position(line=a, character=b),
                   end=P.Position(line=c, character=d))


def _round(value, to_json, from_json):
    return from_json(json.loads(json.dumps(to_json(value))))


class LeafRoundTripTests(unittest.TestCase):
    def test_position_and_range(self) -> None:
        r = _range()
        self.assertEqual(_round(r, W.range_to_json, W.range_from_json), r)

    def test_location(self) -> None:
        loc = P.Location(uri="file:///x.py", range=_range())
        self.assertEqual(
            _round(loc, W.location_to_json, W.location_from_json), loc)

    def test_symbol_keeps_selection_apart_from_range(self) -> None:
        # Requests are positioned on `selection`; collapsing the two
        # would ask about a class body instead of the class.
        s = P.Symbol(name="f", kind=12, uri="file:///x.py",
                     range=_range(1, 0, 9, 0), selection=_range(1, 4, 1, 5),
                     container="C", detail="def f()")
        got = _round(s, W.symbol_to_json, W.symbol_from_json)
        self.assertEqual(got, s)
        self.assertNotEqual(got.range, got.selection)

    def test_call_with_ranges(self) -> None:
        item = P.CallHierarchyItem(name="g", kind=12, uri="file:///y.py",
                                   range=_range(), selection=_range(),
                                   detail="d")
        c = P.CallHierarchyCall(item=item, ranges=(_range(5, 1, 5, 4),))
        self.assertEqual(_round(c, W.call_to_json, W.call_from_json), c)

    def test_workspace_edit_keeps_file_operations(self) -> None:
        # Dropping these would report a rename as complete while leaving
        # the workspace half-renamed.
        w = P.WorkspaceEdit(
            edits=(P.TextEdit(uri="file:///x.py", range=_range(),
                              new_text="new"),),
            file_operations=("rename x.py -> y.py",))
        got = _round(w, W.workspace_edit_to_json, W.workspace_edit_from_json)
        self.assertEqual(got, w)
        self.assertEqual(got.file_operations, ("rename x.py -> y.py",))


class NavResponseRoundTripTests(unittest.TestCase):
    def _round(self, res, kind):
        return W.nav_from_json(
            json.loads(json.dumps(W.nav_to_json(res, item_kind=kind))))

    def test_items_and_provenance_survive(self) -> None:
        res = NavResponse(
            items=[P.Location(uri="file:///x.py", range=_range())],
            consulted=("pyright-langserver",),
            failures=(("gopls", "timeout"),),
            progress={"title": "indexing", "percentage": 40},
            not_running=("clangd",),
            scan_truncated_at=500,
        )
        got = self._round(res, "location")
        self.assertEqual(got.items, res.items)
        self.assertEqual(got.consulted, res.consulted)
        self.assertEqual(got.failures, res.failures)
        self.assertEqual(got.progress, res.progress)
        self.assertEqual(got.not_running, res.not_running)
        self.assertEqual(got.scan_truncated_at, 500)

    def test_trustworthy_is_preserved_across_the_wire(self) -> None:
        # The whole point of the provenance fields. If a failure were
        # dropped, an empty result would arrive looking like a fact.
        failed = NavResponse(items=[], failures=(("tsserver", "timeout"),))
        self.assertFalse(self._round(failed, "location").trustworthy)
        clean = NavResponse(items=[], consulted=("tsserver",))
        self.assertTrue(self._round(clean, "location").trustworthy)

    def test_hover_text_items(self) -> None:
        res = NavResponse(items=["**markdown** hover"],
                          consulted=("tsserver",))
        self.assertEqual(self._round(res, "text").items,
                         ["**markdown** hover"])

    def test_empty_response(self) -> None:
        got = self._round(NavResponse(), "symbol")
        self.assertEqual(got.items, [])
        self.assertEqual(got.consulted, ())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
