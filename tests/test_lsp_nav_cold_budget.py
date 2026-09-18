"""Navigation: the cold-start budget, and not inventing empty results.

Two halves of one incident. On the cline monorepo a cold
``textDocument/references`` took **7.84 s** while the flat navigation
budget was **5 s**, so the first cross-file request always failed and
every later one succeeded (warm: 0.15 s, a 52x cliff). That timeout then
rendered as ``References: none found.`` with the warning pushed below
it — for a symbol with four real references.

The engine's own ``NavResponse`` docstring enumerates four ways ``items``
comes back empty and says collapsing them is the bug class it exists to
avoid. The renderer was collapsing two of them.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from claude_hooks.lsp_engine.engine import (  # noqa: E402
    NAV_COLD_TIMEOUT,
    Engine,
    NavResponse,
)
from claude_hooks.lsp_mcp import tools as T  # noqa: E402


# ── rendering ────────────────────────────────────────────────────────

class EmptyResultHonestyTests(unittest.TestCase):
    """``none found`` is a claim, and it needs to be earned."""

    def test_timeout_does_not_render_as_none_found(self) -> None:
        res = NavResponse(
            items=[],
            consulted=(),
            failures=(("typescript-language-server",
                       "timeout waiting for response to "
                       "'textDocument/references'"),),
        )
        out = T.render_locations(res, title="References")
        # The lead line is what a caller acts on.
        self.assertTrue(out.startswith("NO ANSWER"), out.splitlines()[0])
        self.assertNotIn("none found", out)
        self.assertIn("NOT an empty result", out)
        # The detail still follows.
        self.assertIn("typescript-language-server", out)

    def test_genuine_empty_still_says_none_found(self) -> None:
        # A server answered, and answered "nothing". That is a fact.
        res = NavResponse(items=[], consulted=("pyright-langserver",))
        out = T.render_locations(res, title="References")
        self.assertIn("References: none found.", out)
        self.assertNotIn("NO ANSWER", out)

    def test_still_indexing_is_not_empty(self) -> None:
        res = NavResponse(items=[], consulted=(),
                          progress={"title": "indexing", "percentage": 40})
        out = T.render_locations(res, title="References")
        self.assertIn("NO ANSWER YET", out)
        self.assertNotIn("none found", out)

    def test_no_server_claims_the_file(self) -> None:
        res = NavResponse(items=[], consulted=())
        out = T.render_locations(res, title="References")
        self.assertIn("NOT ANALYSED", out)
        self.assertNotIn("none found", out)

    def test_partial_scan_qualifies_the_claim(self) -> None:
        res = NavResponse(items=[], consulted=("tsserver",),
                          scan_truncated_at=500)
        out = T.render_locations(res, title="References")
        self.assertIn("in what was searched", out)
        self.assertIn("not ruled out", out)

    def test_results_present_are_untouched(self) -> None:
        # A partial answer with real items must still lead with them.
        import tempfile
        from claude_hooks.lsp_engine.protocol import Location, Position, Range
        # Built from the platform's own temp dir: "/tmp/a.ts" has no
        # drive letter, so it is not absolute on Windows and as_uri()
        # raises there.
        somewhere = Path(tempfile.gettempdir()).resolve() / "a.ts"
        loc = Location(uri=somewhere.as_uri(),
                       range=Range(start=Position(line=3, character=2),
                                   end=Position(line=3, character=8)))
        res = NavResponse(items=[loc], consulted=("tsserver",),
                          failures=(("gopls", "timeout"),))
        out = T.render_locations(res, title="References")
        self.assertTrue(out.startswith("References (1)"), out.splitlines()[0])
        self.assertIn("WARNING", out)

    def test_every_renderer_is_covered(self) -> None:
        failed = NavResponse(
            items=[], consulted=(),
            failures=(("typescript-language-server", "timeout"),))
        for render in (
            lambda r: T.render_locations(r, title="References"),
            lambda r: T.render_symbols(r, title="Symbols"),
            lambda r: T.render_hover(r),
            lambda r: T.render_calls(r, direction="incoming"),
        ):
            out = render(failed)
            self.assertIn("NO ANSWER", out)
            self.assertNotIn("none found", out)


# ── budget ───────────────────────────────────────────────────────────

class _Client:
    """Stands in for an LspClient; identity is all the engine uses."""


class ColdBudgetTests(unittest.TestCase):
    def engine(self, request_timeout: float = 5.0) -> Engine:
        return Engine(REPO, [], request_timeout=request_timeout)

    def test_first_request_gets_the_cold_allowance(self) -> None:
        eng = self.engine()
        self.assertEqual(eng._nav_timeout(_Client(), "references"),
                         NAV_COLD_TIMEOUT)

    def test_cold_allowance_exceeds_the_measured_cold_path(self) -> None:
        # 7.84 s measured on cline; a budget under that is the bug.
        self.assertGreater(NAV_COLD_TIMEOUT, 7.84)

    def test_warm_client_returns_to_the_small_budget(self) -> None:
        eng = self.engine()
        c = _Client()
        eng._nav_warm.add((c, "project"))
        self.assertEqual(eng._nav_timeout(c, "references"), 5.0)

    def test_warmth_is_per_client(self) -> None:
        eng = self.engine()
        warm, cold = _Client(), _Client()
        eng._nav_warm.add((warm, "project"))
        self.assertEqual(eng._nav_timeout(warm, "references"), 5.0)
        self.assertEqual(eng._nav_timeout(cold, "references"),
                         NAV_COLD_TIMEOUT)

    def test_a_large_configured_timeout_is_not_lowered(self) -> None:
        eng = self.engine(request_timeout=45.0)
        self.assertEqual(eng._nav_timeout(_Client(), "references"), 45.0)

    def test_success_marks_the_client_warm(self) -> None:
        eng = self.engine()
        client = _Client()
        eng._clients_for_test = client
        specs = [type("S", (), {"command": ["tsserver"]})()]
        eng._client_for = lambda spec: client  # type: ignore[assignment]
        res = eng._fan_out(specs, lambda c: [1, 2], what="references")
        self.assertEqual(len(res.items), 2)
        self.assertIn((client, "project"), eng._nav_warm)
        # And the next call is budgeted as warm.
        self.assertEqual(eng._nav_timeout(client, "references"), 5.0)

    def test_a_per_file_success_does_not_clear_the_allowance(self) -> None:
        """The trap this fix originally fell into.

        ``find_symbols`` issues ``documentSymbol`` first. It answers in
        well under a second because it only needs the one file parsed —
        and if that marks the client warm, the ``references`` call right
        behind it gets the small budget and times out exactly as before.
        The first attempt at this fix did that and measured as a no-op.
        """
        eng = self.engine()
        client = _Client()
        specs = [type("S", (), {"command": ["tsserver"]})()]
        eng._client_for = lambda spec: client  # type: ignore[assignment]
        eng._fan_out(specs, lambda c: [1], what="documentSymbol")
        self.assertNotIn((client, "project"), eng._nav_warm)
        # The cross-file request still gets the cold allowance.
        self.assertEqual(eng._nav_timeout(client, "references"),
                         NAV_COLD_TIMEOUT)

    def test_per_file_requests_keep_the_small_budget(self) -> None:
        eng = self.engine()
        c = _Client()
        self.assertEqual(eng._nav_timeout(c, "documentSymbol"), 5.0)
        self.assertEqual(eng._nav_timeout(c, "hover"), 5.0)

    def test_failure_leaves_the_client_cold(self) -> None:
        from claude_hooks.lsp_engine.lsp import LspError
        eng = self.engine()
        client = _Client()
        client.progress_snapshot = lambda: None  # type: ignore[attr-defined]
        specs = [type("S", (), {"command": ["tsserver"]})()]
        eng._client_for = lambda spec: client  # type: ignore[assignment]

        def boom(c):
            raise LspError("timeout")

        res = eng._fan_out(specs, boom, what="references")
        self.assertEqual(res.failures[0][0], "tsserver")
        self.assertNotIn((client, "project"), eng._nav_warm)
        # A server that never answered has not built anything; the next
        # attempt still needs the cold allowance.
        self.assertEqual(eng._nav_timeout(client, "references"),
                         NAV_COLD_TIMEOUT)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
