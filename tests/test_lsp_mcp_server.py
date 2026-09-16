"""The MCP server that replaces cclsp.

Two things are being pinned here. The first is **surface conformance**:
every configured client calls these twelve names with these parameters,
so a drift is a broken client, not a refactor. The second is the set of
behaviours cclsp got wrong, each of which produced a plausible-looking
success.
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from claude_hooks.lsp_engine.engine import NavResponse  # noqa: E402
from claude_hooks.lsp_engine.protocol import (  # noqa: E402
    Location, Position, Range, Symbol, TextEdit, WorkspaceEdit,
)
from claude_hooks.lsp_mcp import server as S  # noqa: E402
from claude_hooks.lsp_mcp import tools as T  # noqa: E402


def rng(l1=0, c1=0, l2=0, c2=1):
    return Range(Position(l1, c1), Position(l2, c2))


class SurfaceConformanceTests(unittest.TestCase):
    """cclsp's contract, inherited verbatim.

    Recorded from the live cclsp MCP on 2026-09-16. A change here breaks
    every client and every model that has learned this surface.
    """

    EXPECTED = {
        "find_definition": {"file_path", "symbol_name"},
        "find_references": {"file_path", "symbol_name"},
        "find_implementation": {"file_path"},
        "get_hover": {"file_path"},
        "get_diagnostics": {"file_path"},
        "find_workspace_symbols": {"query"},
        "prepare_call_hierarchy": {"file_path"},
        "get_incoming_calls": {"file_path"},
        "get_outgoing_calls": {"file_path"},
        "rename_symbol": {"file_path", "symbol_name", "new_name"},
        "rename_symbol_strict": {"file_path", "line", "character", "new_name"},
        "restart_server": set(),
    }

    def setUp(self):
        self.catalog = {t["name"]: t for t in T.tool_catalog()}

    def test_exactly_the_twelve_cclsp_tools(self):
        self.assertEqual(set(self.catalog), set(self.EXPECTED))

    def test_required_parameters_match(self):
        for name, required in self.EXPECTED.items():
            with self.subTest(tool=name):
                self.assertEqual(
                    set(self.catalog[name]["inputSchema"]["required"]),
                    required)

    def test_every_tool_has_a_description_and_schema(self):
        for name, tool in self.catalog.items():
            with self.subTest(tool=name):
                self.assertTrue(tool["description"].strip())
                self.assertEqual(tool["inputSchema"]["type"], "object")

    def test_position_tools_also_accept_a_symbol_name(self):
        """Our addition, strictly additive: existing positional calls are
        unaffected, and a caller who knows only the name need not guess
        coordinates that would silently return an empty result."""
        for name in ("find_implementation", "get_hover",
                     "prepare_call_hierarchy", "get_incoming_calls",
                     "get_outgoing_calls"):
            with self.subTest(tool=name):
                props = self.catalog[name]["inputSchema"]["properties"]
                self.assertIn("symbol_name", props)
                self.assertIn("line", props)
                self.assertNotIn("line",
                                 self.catalog[name]["inputSchema"]["required"])

    def test_tool_names_tuple_matches_catalog(self):
        self.assertEqual(set(T.TOOL_NAMES), set(self.catalog))


class PositionConversionTests(unittest.TestCase):
    """1-based in, 0-based out, in exactly one place."""

    def test_one_based_to_zero_based(self):
        self.assertEqual(T.to_lsp_position(1, 1), (0, 0))
        self.assertEqual(T.to_lsp_position(102, 9), (101, 8))

    def test_zero_is_rejected_not_clamped(self):
        """A caller passing 0 is either already 0-based — in which case
        every result is off by one — or has a bug. Clamping hides both."""
        for line, ch in ((0, 1), (1, 0), (0, 0), (-5, 2)):
            with self.subTest(line=line, character=ch):
                with self.assertRaises(T.ToolError) as cm:
                    T.to_lsp_position(line, ch)
                self.assertIn("1-indexed", str(cm.exception))

    def test_non_numeric_is_a_caller_error(self):
        with self.assertRaises(T.ToolError):
            T.to_lsp_position("x", 1)
        with self.assertRaises(T.ToolError):
            T.to_lsp_position(None, None)

    def test_numeric_strings_and_floats_are_accepted(self):
        self.assertEqual(T.to_lsp_position("3", 4.0), (2, 3))


class UriPathTests(unittest.TestCase):

    def test_plain_posix_uri(self):
        self.assertEqual(T.uri_to_path("file:///srv/a/b.py"), "/srv/a/b.py")

    def test_percent_encoding_is_decoded(self):
        """Servers publish `file:///c%3A/x`; a caller handed that string
        cannot open it."""
        self.assertEqual(T.uri_to_path("file:///a/my%20file.py"),
                         "/a/my file.py")

    def test_non_file_uri_passes_through(self):
        self.assertEqual(T.uri_to_path("untitled:Untitled-1"),
                         "untitled:Untitled-1")


class ProvenanceTests(unittest.TestCase):
    """Four ways to get an empty list; only one is about the code."""

    def test_trustworthy_empty_has_no_note(self):
        self.assertEqual(
            T.provenance_note(NavResponse(items=[], consulted=("pyright",))),
            "")

    def test_nothing_asked_says_not_analysed(self):
        note = T.provenance_note(NavResponse(items=[]))
        self.assertIn("NOT ANALYSED", note)
        self.assertIn("not a statement about the code", note)

    def test_failure_is_surfaced_with_the_server_name(self):
        note = T.provenance_note(
            NavResponse(items=[], failures=(("gopls", "exited"),)))
        self.assertIn("gopls", note)
        self.assertIn("did not answer", note)

    def test_indexing_says_retry_and_gives_an_eta(self):
        note = T.provenance_note(NavResponse(
            items=[], failures=(("clangd", "timeout"),),
            progress={"server": "clangd", "title": "Indexing",
                      "message": "12000 files", "percentage": 40,
                      "eta_seconds": 90}))
        self.assertIn("INCOMPLETE", note)
        self.assertIn("NOT an empty result", note)
        self.assertIn("Indexing", note)
        self.assertIn("40%", note)
        self.assertIn("Retry", note)

    def test_retry_hint_is_capped(self):
        note = T.provenance_note(NavResponse(
            items=[], progress={"server": "c", "title": "x",
                                "eta_seconds": 100000}))
        self.assertIn("Retry the same call in 60s", note)

    def test_progress_without_an_eta_still_says_retry(self):
        note = T.provenance_note(NavResponse(
            items=[], progress={"server": "c", "title": "Loading"}))
        self.assertIn("Retry the same call in 30s", note)

    def test_not_running_servers_make_it_partial(self):
        note = T.provenance_note(
            NavResponse(items=[], consulted=("pyright",),
                        not_running=("gopls", "clangd")))
        self.assertIn("PARTIAL", note)
        self.assertIn("gopls", note)
        self.assertIn("start_all=true", note)


class RenderTests(unittest.TestCase):

    def test_locations_are_one_based_for_display(self):
        res = NavResponse(items=[Location("file:///p/a.py", rng(101, 8, 101, 16))],
                          consulted=("pyright",))
        out = T.render_locations(res, root=Path("/p"), title="Definitions")
        self.assertIn("a.py:102:9", out)

    def test_duplicate_locations_from_two_servers_are_collapsed(self):
        """Counting results would otherwise report two definitions."""
        loc = Location("file:///p/a.py", rng(1, 0, 1, 4))
        res = NavResponse(items=[loc, loc], consulted=("a", "b"))
        out = T.render_locations(res, root=Path("/p"), title="Definitions")
        self.assertIn("(1)", out)

    def test_empty_but_trustworthy_says_none_found(self):
        out = T.render_locations(NavResponse(items=[], consulted=("pyright",)),
                                 title="References")
        self.assertIn("none found", out)
        self.assertNotIn("NOT ANALYSED", out)

    def test_empty_and_untrustworthy_carries_the_warning(self):
        out = T.render_locations(NavResponse(items=[]), title="References")
        self.assertIn("NOT ANALYSED", out)

    def test_symbols_show_container_and_kind(self):
        res = NavResponse(items=[Symbol(
            name="did_open", kind=6, uri="file:///p/e.py",
            range=rng(10, 4, 20, 0), selection=rng(10, 8, 10, 16),
            container="Engine")], consulted=("pyright",))
        out = T.render_symbols(res, root=Path("/p"))
        self.assertIn("[method] Engine.did_open", out)
        self.assertIn("e.py:11", out)

    def test_hover_joins_multiple_servers_with_a_rule(self):
        res = NavResponse(items=["from html", "from ts"], consulted=("a", "b"))
        self.assertIn("---", T.render_hover(res))


class RenameRenderTests(unittest.TestCase):

    def edit(self, n=3):
        return WorkspaceEdit(edits=tuple(
            TextEdit(f"file:///p/f{i}.py", rng(i, 0, i, 3), "new")
            for i in range(n)))

    def test_preview_says_nothing_was_written(self):
        out = T.render_rename(self.edit(), root=Path("/p"), applied=False,
                              new_name="new")
        self.assertIn("Planned (not written)", out)
        self.assertIn("apply=true", out)

    def test_applied_says_so(self):
        out = T.render_rename(self.edit(), root=Path("/p"), applied=True,
                              new_name="new")
        self.assertIn("Applied", out)
        self.assertNotIn("apply=true", out)

    def test_file_operations_are_reported_as_incomplete(self):
        """A rename that also needed a file moved is half done if we
        ignore that half — and reporting success would be the lie."""
        edit = WorkspaceEdit(
            edits=(TextEdit("file:///p/A.java", rng(0, 6, 0, 7), "B"),),
            file_operations=("rename file:///p/A.java -> file:///p/B.java",))
        out = T.render_rename(edit, root=Path("/p"), applied=True,
                              new_name="B")
        self.assertIn("NOT APPLIED", out)
        self.assertIn("INCOMPLETE", out)

    def test_no_edits_explains_rather_than_claiming_success(self):
        out = T.render_rename(WorkspaceEdit(), root=None, applied=False,
                              new_name="x")
        self.assertIn("No rename edits", out)


class ApplyEditTests(unittest.TestCase):
    """Edits must be applied bottom-up, or they corrupt the file while
    reporting success."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.f = Path(self.tmp.name) / "a.py"

    def test_single_edit(self):
        self.f.write_text("aaa bbb\n", encoding="utf-8")
        S._apply_edit(WorkspaceEdit(edits=(
            TextEdit(self.f.as_uri(), rng(0, 4, 0, 7), "ccc"),)))
        self.assertEqual(self.f.read_text(encoding="utf-8"), "aaa ccc\n")

    def test_two_edits_on_one_line_apply_right_to_left(self):
        """Left-to-right, the first replacement shifts the second
        range and the edit lands in the wrong columns."""
        self.f.write_text("old and old\n", encoding="utf-8")
        S._apply_edit(WorkspaceEdit(edits=(
            TextEdit(self.f.as_uri(), rng(0, 0, 0, 3), "brandnew"),
            TextEdit(self.f.as_uri(), rng(0, 8, 0, 11), "brandnew"),
        )))
        self.assertEqual(self.f.read_text(encoding="utf-8"),
                         "brandnew and brandnew\n")

    def test_multi_line_edits_apply_bottom_up(self):
        self.f.write_text("x = old\ny = 1\nz = old\n", encoding="utf-8")
        S._apply_edit(WorkspaceEdit(edits=(
            TextEdit(self.f.as_uri(), rng(0, 4, 0, 7), "renamed"),
            TextEdit(self.f.as_uri(), rng(2, 4, 2, 7), "renamed"),
        )))
        self.assertEqual(self.f.read_text(encoding="utf-8"),
                         "x = renamed\ny = 1\nz = renamed\n")

    def test_edit_spanning_lines(self):
        self.f.write_text("a(\n  b\n)\n", encoding="utf-8")
        S._apply_edit(WorkspaceEdit(edits=(
            TextEdit(self.f.as_uri(), rng(0, 0, 2, 1), "c()"),)))
        self.assertEqual(self.f.read_text(encoding="utf-8"), "c()\n")

    def test_several_files(self):
        g = Path(self.tmp.name) / "b.py"
        self.f.write_text("old\n", encoding="utf-8")
        g.write_text("old\n", encoding="utf-8")
        n = S._apply_edit(WorkspaceEdit(edits=(
            TextEdit(self.f.as_uri(), rng(0, 0, 0, 3), "new"),
            TextEdit(g.as_uri(), rng(0, 0, 0, 3), "new"),
        )))
        self.assertEqual(n, 2)
        self.assertEqual(g.read_text(encoding="utf-8"), "new\n")

    def test_missing_file_is_a_tool_error_not_a_traceback(self):
        with self.assertRaises(T.ToolError):
            S._apply_edit(WorkspaceEdit(edits=(
                TextEdit((Path(self.tmp.name) / "gone.py").as_uri(),
                         rng(0, 0, 0, 1), "x"),)))

    def test_out_of_range_line_is_ignored_rather_than_appending(self):
        self.f.write_text("one\n", encoding="utf-8")
        S._apply_edit(WorkspaceEdit(edits=(
            TextEdit(self.f.as_uri(), rng(99, 0, 99, 1), "x"),)))
        self.assertEqual(self.f.read_text(encoding="utf-8"), "one\n")


class ApplyDefaultTests(unittest.TestCase):
    """Preview by default. This inverts cclsp, deliberately."""

    def test_default_is_preview(self):
        self.assertFalse(S._wants_apply({}))

    def test_apply_true_writes(self):
        self.assertTrue(S._wants_apply({"apply": True}))

    def test_dry_run_is_honoured_for_compatibility(self):
        self.assertFalse(S._wants_apply({"dry_run": True}))
        self.assertTrue(S._wants_apply({"dry_run": False}))

    def test_apply_wins_over_dry_run(self):
        self.assertTrue(S._wants_apply({"apply": True, "dry_run": True}))


class ProjectRootTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def test_finds_the_nearest_marker(self):
        (self.root / "cclsp.json").write_text("{}", encoding="utf-8")
        deep = self.root / "a" / "b"
        deep.mkdir(parents=True)
        (deep / "f.py").write_text("x", encoding="utf-8")
        self.assertEqual(S.find_project_root(deep / "f.py"), self.root)

    def test_no_marker_returns_none(self):
        """Guessing / would start a language server over the whole disk
        — the inferred-project problem that made TypeScript useless."""
        deep = self.root / "x"
        deep.mkdir()
        f = deep / "f.py"
        f.write_text("x", encoding="utf-8")
        found = S.find_project_root(f)
        self.assertIsNone(found, f"unexpectedly resolved to {found}")

    def test_directory_input_works(self):
        (self.root / ".git").mkdir()
        self.assertEqual(S.find_project_root(self.root), self.root)


class RegistryTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.cfg = self.root / "cclsp.json"
        self._write_config(["py"])
        self.file = self.root / "a.py"
        self.file.write_text("x = 1\n", encoding="utf-8")

    def _write_config(self, exts):
        self.cfg.write_text(json.dumps({"servers": [
            {"extensions": exts, "command": ["true"], "rootDir": "."}]}),
            encoding="utf-8")

    def test_engine_is_built_and_reused(self):
        reg = S.EngineRegistry()
        self.addCleanup(reg.shutdown_all)
        a = reg.for_path(self.file)
        b = reg.for_path(self.file)
        self.assertIs(a.engine, b.engine)

    def test_config_change_rebuilds_the_engine(self):
        """cclsp had to be killed to re-read its config."""
        reg = S.EngineRegistry()
        self.addCleanup(reg.shutdown_all)
        first = reg.for_path(self.file).engine
        time.sleep(0.01)
        self._write_config(["py", "pyi"])
        second = reg.for_path(self.file).engine
        self.assertIsNot(first, second)

    def test_missing_config_is_an_actionable_error(self):
        self.cfg.unlink()
        (self.root / ".git").mkdir()
        reg = S.EngineRegistry()
        self.addCleanup(reg.shutdown_all)
        with self.assertRaises(T.ToolError) as cm:
            reg.for_path(self.file)
        self.assertIn("sync_cclsp.py", str(cm.exception))

    def test_idle_engines_are_reaped(self):
        reg = S.EngineRegistry(idle_hours=0.0)   # clamps to 60s
        self.addCleanup(reg.shutdown_all)
        entry = reg.for_path(self.file)
        entry.last_used -= 120
        self.assertEqual(reg.reap_idle(), [str(self.root)])
        self.assertEqual(reg.running(), [])

    def test_fresh_engines_are_not_reaped(self):
        reg = S.EngineRegistry(idle_hours=0.0)
        self.addCleanup(reg.shutdown_all)
        reg.for_path(self.file)
        self.assertEqual(reg.reap_idle(), [])

    def test_two_projects_get_two_engines(self):
        """cclsp served one workspace; a file outside it got the same
        message as an unsupported extension."""
        other = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(lambda: __import__("shutil").rmtree(other,
                                                            ignore_errors=True))
        (other / "cclsp.json").write_text(json.dumps({"servers": [
            {"extensions": ["py"], "command": ["true"], "rootDir": "."}]}),
            encoding="utf-8")
        f2 = other / "b.py"
        f2.write_text("y = 2\n", encoding="utf-8")

        reg = S.EngineRegistry()
        self.addCleanup(reg.shutdown_all)
        self.assertIsNot(reg.for_path(self.file).engine,
                         reg.for_path(f2).engine)
        self.assertEqual(len(reg.running()), 2)


class JsonRpcTests(unittest.TestCase):

    def setUp(self):
        self.server = S.LspMcpServer()

    def test_initialize_reports_the_server_identity(self):
        r = self.server.handle({"jsonrpc": "2.0", "id": 1,
                                "method": "initialize", "params": {}})
        self.assertEqual(r["result"]["serverInfo"]["name"],
                         "claude-hooks-lsp")
        self.assertIn("tools", r["result"]["capabilities"])

    def test_initialized_notification_gets_no_reply(self):
        self.assertIsNone(self.server.handle(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_tools_list(self):
        r = self.server.handle({"jsonrpc": "2.0", "id": 2,
                                "method": "tools/list"})
        self.assertEqual(len(r["result"]["tools"]), 12)

    def test_unknown_method_errors(self):
        r = self.server.handle({"jsonrpc": "2.0", "id": 3,
                                "method": "nope/nope"})
        self.assertEqual(r["error"]["code"], -32601)

    def test_unknown_notification_is_silent(self):
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0",
                                              "method": "nope/nope"}))

    def test_unknown_tool_lists_the_real_ones(self):
        r = self.server.handle({"jsonrpc": "2.0", "id": 4,
                                "method": "tools/call",
                                "params": {"name": "get_hovr",
                                           "arguments": {}}})
        self.assertTrue(r["result"]["isError"])
        self.assertIn("get_hover", r["result"]["content"][0]["text"])

    def test_missing_file_path_is_a_caller_error_not_a_traceback(self):
        r = self.server.handle({"jsonrpc": "2.0", "id": 5,
                                "method": "tools/call",
                                "params": {"name": "get_hover",
                                           "arguments": {}}})
        self.assertTrue(r["result"]["isError"])
        text = r["result"]["content"][0]["text"]
        self.assertIn("file_path", text)
        self.assertNotIn("Traceback", text)

    def test_nonexistent_file_says_where_paths_resolve(self):
        r = self.server.handle({
            "jsonrpc": "2.0", "id": 6, "method": "tools/call",
            "params": {"name": "get_diagnostics",
                       "arguments": {"file_path": "/nope/missing.py"}}})
        self.assertIn("does not exist",
                      r["result"]["content"][0]["text"])


class SymbolKindValidationTests(unittest.TestCase):

    def test_known_kinds_pass(self):
        self.assertEqual(S.T_parse_kind("method"), 6)
        self.assertIsNone(S.T_parse_kind(None))
        self.assertIsNone(S.T_parse_kind(""))

    def test_unknown_kind_lists_the_valid_ones(self):
        """Silently ignoring it would filter nothing and quietly return
        the wrong symbol."""
        with self.assertRaises(T.ToolError) as cm:
            S.T_parse_kind("banana")
        self.assertIn("function", str(cm.exception))


class MergeTests(unittest.TestCase):

    def test_failures_from_every_response_are_kept(self):
        """find_definition on an overloaded name queries once per match;
        keeping only the last response's failures would report a partial
        answer as complete."""
        merged = S._merge([
            NavResponse(items=[1], consulted=("a",), failures=(("a", "x"),)),
            NavResponse(items=[2], consulted=("b",), failures=(("b", "y"),)),
        ])
        self.assertEqual(merged.items, [1, 2])
        self.assertEqual(len(merged.failures), 2)
        self.assertEqual(merged.consulted, ("a", "b"))
        self.assertFalse(merged.trustworthy)

    def test_consulted_is_deduped(self):
        merged = S._merge([NavResponse(items=[], consulted=("a",)),
                           NavResponse(items=[], consulted=("a",))])
        self.assertEqual(merged.consulted, ("a",))

    def test_first_progress_wins(self):
        merged = S._merge([
            NavResponse(items=[], progress={"title": "first"}),
            NavResponse(items=[], progress={"title": "second"}),
        ])
        self.assertEqual(merged.progress["title"], "first")


class NoSymbolMessageTests(unittest.TestCase):

    def test_clean_miss_explains_exact_matching(self):
        msg = S._no_symbol_message("foo", NavResponse(items=[],
                                                      consulted=("pyright",)))
        self.assertIn("exactly", msg)

    def test_server_problem_is_not_reported_as_a_missing_symbol(self):
        """Otherwise the caller goes looking for a name that exists."""
        msg = S._no_symbol_message(
            "foo", NavResponse(items=[], failures=(("pyright", "died"),)))
        self.assertIn("pyright", msg)


if __name__ == "__main__":
    unittest.main()
