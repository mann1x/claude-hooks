"""Engine-level navigation: routing, merging, and honest emptiness.

The engine's job is not to forward requests — it is to make an empty
result mean something. Four different situations produce no items, and
only one of them is a statement about the code; these tests pin that
the other three stay distinguishable.
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from claude_hooks.lsp_engine.config import LspServerSpec  # noqa: E402
from claude_hooks.lsp_engine.engine import Engine, NavResponse  # noqa: E402
from claude_hooks.lsp_engine.lsp import LspError  # noqa: E402
from claude_hooks.lsp_engine.protocol import (  # noqa: E402
    CallHierarchyCall, CallHierarchyItem, Location, Position, Range, Symbol,
    WorkspaceEdit,
)


def rng(l1=0, c1=0, l2=0, c2=1):
    return Range(Position(l1, c1), Position(l2, c2))


def sym(name, kind=12, container="", line=0):
    return Symbol(name=name, kind=kind, uri="file:///x.py",
                  range=rng(line, 0, line, 5), selection=rng(line, 4, line, 9),
                  container=container)


class FakeClient:
    """Stands in for one language server."""

    def __init__(self, name="fake", *, symbols=None, locations=None,
                 hover_text="", fail=None, progress=None, calls=None,
                 ws_symbols=None, edit=None, renameable=True):
        self.name = name
        self._symbols = symbols or []
        self._locations = locations or []
        self._hover = hover_text
        self._fail = fail
        self._progress = progress
        self._calls = calls or []
        self._ws = ws_symbols or []
        self._edit = edit
        self._renameable = renameable
        self.opened: list[str] = []
        self.stopped = False
        self.rename_calls: list[tuple] = []
        self.desynced = False
        self.alive = True

    @property
    def is_desynced(self):
        return self.desynced

    @property
    def is_alive(self):
        return self.alive and not self.desynced

    def _maybe_fail(self):
        if self._fail:
            raise LspError(self._fail)

    def did_open(self, path, content):
        self.opened.append(str(path))

    def did_change(self, path, content):
        pass

    def did_close(self, path):
        pass

    def document_symbols(self, path, **kw):
        self._maybe_fail()
        return list(self._symbols)

    def definition(self, path, line, ch, **kw):
        self._maybe_fail()
        return list(self._locations)

    implementation = definition

    def references(self, path, line, ch, *, include_declaration=True, **kw):
        self._maybe_fail()
        self.include_declaration = include_declaration
        return list(self._locations)

    def hover(self, path, line, ch, **kw):
        self._maybe_fail()
        return self._hover

    def prepare_call_hierarchy(self, path, line, ch, **kw):
        self._maybe_fail()
        return [CallHierarchyItem(name="run", kind=12, uri="file:///x.py",
                                  range=rng(), selection=rng())]

    def incoming_calls(self, item, **kw):
        self._maybe_fail()
        return list(self._calls)

    outgoing_calls = incoming_calls

    def workspace_symbols(self, query, **kw):
        self._maybe_fail()
        return list(self._ws)

    def prepare_rename(self, path, line, ch, **kw):
        return self._renameable

    def rename(self, path, line, ch, new_name, **kw):
        self._maybe_fail()
        self.rename_calls.append((line, ch, new_name))
        return self._edit or WorkspaceEdit()

    def progress_snapshot(self):
        return self._progress

    def stop(self, timeout=3.0):
        self.stopped = True


class EngineHarness(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.file = self.root / "x.py"
        self.file.write_text("def f():\n    pass\n", encoding="utf-8")
        self.addCleanup(self.tmp.cleanup)

    def engine(self, *clients_by_spec):
        specs = [s for s, _ in clients_by_spec]
        eng = Engine(self.root, specs)
        mapping = dict(clients_by_spec)
        eng._client_for = lambda spec: mapping[spec]   # type: ignore
        # _client_for is stubbed, so populate the registry the engine
        # uses to decide what is "running" for workspace queries.
        eng._clients = dict(mapping)
        return eng

    @staticmethod
    def spec(*exts, binary="pyright-langserver"):
        return LspServerSpec(extensions=tuple(exts), command=(binary, "--stdio"))


class EmptinessTests(EngineHarness):
    """The four ways to get no items, kept apart."""

    def test_genuinely_empty_is_trustworthy(self):
        eng = self.engine((self.spec("py"), FakeClient(locations=[])))
        res = eng.definition(self.file, 0, 4)
        self.assertEqual(res.items, [])
        self.assertTrue(res.trustworthy)
        self.assertEqual(res.consulted, ("pyright-langserver",))

    def test_no_server_claims_the_file_is_not_trustworthy(self):
        other = self.root / "notes.md"
        other.write_text("hi", encoding="utf-8")
        eng = self.engine((self.spec("py"), FakeClient()))
        res = eng.definition(other, 0, 0)
        self.assertEqual(res.items, [])
        self.assertEqual(res.consulted, ())
        self.assertFalse(res.trustworthy,
                         "nobody was asked; that is not 'nothing found'")

    def test_failing_server_records_the_failure(self):
        eng = self.engine((self.spec("py"), FakeClient(fail="boom")))
        res = eng.definition(self.file, 0, 4)
        self.assertEqual(res.items, [])
        self.assertFalse(res.trustworthy)
        self.assertEqual(res.failures[0][0], "pyright-langserver")
        self.assertIn("boom", res.failures[0][1])

    def test_timeout_while_indexing_attaches_progress(self):
        """'still indexing, 40%' and 'dead' are otherwise the same
        empty list."""
        client = FakeClient(fail="timeout waiting for response",
                            progress={"title": "Indexing", "percentage": 40,
                                      "eta_seconds": 90})
        eng = self.engine((self.spec("py"), client))
        res = eng.definition(self.file, 0, 4)
        self.assertIsNotNone(res.progress)
        self.assertEqual(res.progress["title"], "Indexing")
        self.assertEqual(res.progress["server"], "pyright-langserver")
        self.assertFalse(res.trustworthy)

    def test_bool_is_about_items_not_health(self):
        self.assertFalse(NavResponse(items=[], consulted=("a",)))
        self.assertTrue(NavResponse(items=[1]))


class MergeTests(EngineHarness):
    """A file claimed by two servers gets both halves of the answer."""

    def test_results_from_both_servers_are_merged(self):
        html = self.spec("html", binary="vscode-html-language-server")
        ts = self.spec("html", binary="typescript-language-server")
        eng = self.engine(
            (html, FakeClient(locations=[Location("file:///a", rng())])),
            (ts, FakeClient(locations=[Location("file:///b", rng())])),
        )
        page = self.root / "p.html"
        page.write_text("<html></html>", encoding="utf-8")
        res = eng.definition(page, 0, 1)
        self.assertEqual(len(res.items), 2)
        self.assertEqual(len(res.consulted), 2)

    def test_one_server_failing_still_yields_the_others_half(self):
        html = self.spec("html", binary="vscode-html-language-server")
        ts = self.spec("html", binary="typescript-language-server")
        eng = self.engine(
            (html, FakeClient(fail="dead")),
            (ts, FakeClient(locations=[Location("file:///b", rng())])),
        )
        page = self.root / "p.html"
        page.write_text("<html></html>", encoding="utf-8")
        res = eng.definition(page, 0, 1)
        self.assertEqual(len(res.items), 1, "the working half must survive")
        self.assertEqual(res.consulted, ("typescript-language-server",))
        self.assertEqual(len(res.failures), 1)
        self.assertFalse(res.trustworthy, "a partial answer is not a full one")


class SymbolResolutionTests(EngineHarness):

    def test_exact_name_match_only(self):
        """A prefix match would return did_open_all for did_open, and a
        caller renaming the result would never notice."""
        eng = self.engine((self.spec("py"), FakeClient(symbols=[
            sym("did_open"), sym("did_open_all"), sym("open")])))
        res = eng.find_symbols(self.file, "did_open")
        self.assertEqual([s.name for s in res.items], ["did_open"])

    def test_kind_filter(self):
        eng = self.engine((self.spec("py"), FakeClient(symbols=[
            sym("start", kind=12), sym("start", kind=6)])))
        self.assertEqual(len(eng.find_symbols(self.file, "start").items), 2)
        methods = eng.find_symbols(self.file, "start", kind=6)
        self.assertEqual(len(methods.items), 1)
        self.assertEqual(methods.items[0].kind_name, "method")

    def test_all_matches_returned_for_the_caller_to_disambiguate(self):
        """Two classes in one file can both have `start`; picking one is
        a coin flip dressed as an answer."""
        eng = self.engine((self.spec("py"), FakeClient(symbols=[
            sym("start", container="Engine"), sym("start", container="Client")])))
        res = eng.find_symbols(self.file, "start")
        self.assertEqual(sorted(s.container for s in res.items),
                         ["Client", "Engine"])

    def test_missing_symbol_is_empty_but_trustworthy(self):
        eng = self.engine((self.spec("py"), FakeClient(symbols=[sym("other")])))
        res = eng.find_symbols(self.file, "nope")
        self.assertEqual(res.items, [])
        self.assertTrue(res.trustworthy)


class OpenOnDemandTests(EngineHarness):
    """LSP has no 'look at this file'; a position query against an
    unopened document returns empty rather than erroring."""

    def test_file_is_opened_before_the_request(self):
        client = FakeClient(locations=[Location("file:///a", rng())])
        eng = self.engine((self.spec("py"), client))
        eng.definition(self.file, 0, 4)
        self.assertEqual(len(client.opened), 1)

    def test_already_open_file_is_not_reopened(self):
        client = FakeClient()
        eng = self.engine((self.spec("py"), client))
        eng.did_open(self.file, "def f():\n    pass\n")
        eng.definition(self.file, 0, 4)
        eng.hover(self.file, 0, 4)
        self.assertEqual(len(client.opened), 1)

    def test_unreadable_file_raises_rather_than_returning_empty(self):
        eng = self.engine((self.spec("py"), FakeClient()))
        with self.assertRaises(LspError):
            eng.definition(self.root / "missing.py", 0, 0)


class HoverTests(EngineHarness):

    def test_blank_hover_is_dropped(self):
        """A server that does not handle this file answers "" — keeping
        it renders as an empty hover card from a real server."""
        eng = self.engine(
            (self.spec("py"), FakeClient(hover_text="   ")),
        )
        self.assertEqual(eng.hover(self.file, 0, 4).items, [])

    def test_real_hover_is_kept(self):
        eng = self.engine((self.spec("py"), FakeClient(hover_text="def f()")))
        self.assertEqual(eng.hover(self.file, 0, 4).items, ["def f()"])


class ReferencesTests(EngineHarness):

    def test_include_declaration_is_forwarded(self):
        client = FakeClient(locations=[])
        eng = self.engine((self.spec("py"), client))
        eng.references(self.file, 0, 4, include_declaration=False)
        self.assertFalse(client.include_declaration)


class SeedWorkspaceTests(EngineHarness):
    """Without seeding, pyright answers `find_references` from open
    documents only — so a class used in thirty files reports the two
    uses inside its own. Not an error, not empty: a *shorter list*,
    which is the one shape a caller cannot tell from the truth.

    Measured on this repo: 140 files in 0.1 s, references 2 -> 16.
    """

    def setUp(self):
        super().setUp()
        for name in ("a.py", "b.py", "c.py"):
            (self.root / name).write_text("x = 1\n", encoding="utf-8")
        (self.root / "notes.md").write_text("hi", encoding="utf-8")

    def test_references_opens_the_other_files_of_that_language(self):
        client = FakeClient()
        eng = self.engine((self.spec("py"), client))
        eng.references(self.file, 0, 4)
        opened = {Path(p).name for p in client.opened}
        self.assertEqual(opened, {"x.py", "a.py", "b.py", "c.py"})

    def test_other_languages_are_not_opened(self):
        client = FakeClient()
        eng = self.engine((self.spec("py"), client))
        eng.references(self.file, 0, 4)
        self.assertNotIn("notes.md", {Path(p).name for p in client.opened})

    def test_seeding_happens_once(self):
        client = FakeClient()
        eng = self.engine((self.spec("py"), client))
        eng.references(self.file, 0, 4)
        first = len(client.opened)
        eng.references(self.file, 0, 4)
        self.assertEqual(len(client.opened), first,
                         "second query must not re-walk the tree")

    def test_seed_can_be_declined(self):
        client = FakeClient()
        eng = self.engine((self.spec("py"), client))
        eng.references(self.file, 0, 4, seed=False)
        self.assertEqual({Path(p).name for p in client.opened}, {"x.py"})

    def test_excluded_directories_are_not_walked(self):
        """node_modules and friends hold code nobody is asking about,
        and on a real tree they dominate the file count."""
        for bad in ("node_modules", ".venv", "backup_models", "vendor"):
            d = self.root / bad
            d.mkdir()
            (d / "junk.py").write_text("x = 1\n", encoding="utf-8")
        client = FakeClient()
        eng = self.engine((self.spec("py"), client))
        eng.references(self.file, 0, 4)
        self.assertNotIn("junk.py", {Path(p).name for p in client.opened})

    def test_hitting_the_cap_is_reported_not_hidden(self):
        """A search over the first N files of a bigger repo is a partial
        answer and must not read as a complete one."""
        client = FakeClient()
        eng = self.engine((self.spec("py"), client))
        truncated = eng.seed_workspace(self.file, max_files=2)
        self.assertEqual(truncated, 2)
        res = eng.references(self.file, 0, 4)
        self.assertEqual(res.scan_truncated_at, 2)
        self.assertFalse(res.trustworthy)

    def test_complete_scan_is_trustworthy(self):
        eng = self.engine((self.spec("py"), FakeClient()))
        res = eng.references(self.file, 0, 4)
        self.assertEqual(res.scan_truncated_at, 0)
        self.assertTrue(res.trustworthy)

    def test_unreadable_file_does_not_abort_the_seed(self):
        client = FakeClient()
        eng = self.engine((self.spec("py"), client))
        original = eng.did_open

        def flaky(path, content):
            if Path(path).name == "b.py":
                raise LspError("nope")
            return original(path, content)

        eng.did_open = flaky   # type: ignore
        eng.seed_workspace(self.file)
        self.assertIn("c.py", {Path(p).name for p in client.opened})

    def test_restart_clears_the_seed_so_it_runs_again(self):
        """A restarted server has forgotten every open document; leaving
        the marker would make the next query answer from an empty
        workspace."""
        client = FakeClient()
        spec = self.spec("py")
        eng = self.engine((spec, client))
        eng.references(self.file, 0, 4)
        eng.restart(["py"])
        eng._clients = {spec: client}
        client.opened.clear()
        eng.references(self.file, 0, 4)
        self.assertGreater(len(client.opened), 1, "seed must run again")


class RespawnTests(EngineHarness):
    """A desynced or dead server must be replaced, not reused.

    Reusing one is how cclsp turned a single bad frame into a server
    that timed out forever — surviving restarts of everything except
    itself.
    """

    def engine_with_factory(self, spec, clients):
        """Engine whose _client_for hands out `clients` in order."""
        eng = Engine(self.root, [spec])
        made = []

        def factory(s):
            with eng._lock:
                cur = eng._clients.get(s)
                if cur is not None:
                    reason = eng._retire_reason(s, cur)
                    if reason is None:
                        return cur
                    eng._clients.pop(s, None)
                    eng._forget_client(s)
                    cur.stop()
                nxt = clients[len(made)]
                made.append(nxt)
                eng._clients[s] = nxt
                eng._started_at[s] = time.monotonic()
                return nxt

        eng._client_for = factory   # type: ignore
        return eng, made

    def test_desynced_client_is_replaced(self):
        first, second = FakeClient("a"), FakeClient("b")
        spec = self.spec("py")
        eng, made = self.engine_with_factory(spec, [first, second])
        eng.did_open(self.file, "x")
        self.assertEqual(len(made), 1)

        first.desynced = True
        eng.definition(self.file, 0, 0)
        self.assertEqual(len(made), 2, "must have spawned a replacement")
        self.assertTrue(first.stopped)

    def test_dead_process_is_replaced(self):
        first, second = FakeClient("a"), FakeClient("b")
        spec = self.spec("py")
        eng, made = self.engine_with_factory(spec, [first, second])
        eng.did_open(self.file, "x")
        first.alive = False
        eng.definition(self.file, 0, 0)
        self.assertEqual(len(made), 2)

    def test_healthy_client_is_reused(self):
        first, second = FakeClient("a"), FakeClient("b")
        spec = self.spec("py")
        eng, made = self.engine_with_factory(spec, [first, second])
        eng.did_open(self.file, "x")
        eng.definition(self.file, 0, 0)
        eng.hover(self.file, 0, 0)
        self.assertEqual(len(made), 1)

    def test_replacement_clears_routing_so_the_file_reopens(self):
        """A new process has never seen the document; a stale route would
        send it a did_change for a file it never opened."""
        first, second = FakeClient("a"), FakeClient("b")
        spec = self.spec("py")
        eng, _ = self.engine_with_factory(spec, [first, second])
        eng.did_open(self.file, "x")
        first.desynced = True
        eng.definition(self.file, 0, 0)
        self.assertGreaterEqual(len(second.opened), 1,
                                "replacement must be given the file")

    def test_retire_reason_names_the_cause(self):
        spec = self.spec("py")
        eng = self.engine((spec, FakeClient()))
        c = FakeClient()
        self.assertIsNone(eng._retire_reason(spec, c))
        c.desynced = True
        self.assertIn("desync", eng._retire_reason(spec, c))
        c.desynced, c.alive = False, False
        self.assertIn("exited", eng._retire_reason(spec, c))


class RestartIntervalTests(EngineHarness):
    """cclsp reads `restartInterval` and ships 5 minutes for pylsp.
    We read the same key, so honouring it is the difference between a
    documented field and a decorative one."""

    def test_zero_means_never(self):
        spec = LspServerSpec(extensions=("py",), command=("x",))
        eng = self.engine((spec, FakeClient()))
        eng._started_at[spec] = time.monotonic() - 100000
        self.assertIsNone(eng._retire_reason(spec, FakeClient()))

    def test_elapsed_interval_retires_the_client(self):
        spec = LspServerSpec(extensions=("py",), command=("x",),
                             restart_interval_minutes=5.0)
        eng = self.engine((spec, FakeClient()))
        eng._started_at[spec] = time.monotonic() - 301
        reason = eng._retire_reason(spec, FakeClient())
        self.assertIsNotNone(reason)
        self.assertIn("restartInterval", reason)

    def test_within_the_interval_is_reused(self):
        spec = LspServerSpec(extensions=("py",), command=("x",),
                             restart_interval_minutes=5.0)
        eng = self.engine((spec, FakeClient()))
        eng._started_at[spec] = time.monotonic() - 10
        self.assertIsNone(eng._retire_reason(spec, FakeClient()))

    def test_unknown_start_time_does_not_retire(self):
        spec = LspServerSpec(extensions=("py",), command=("x",),
                             restart_interval_minutes=1.0)
        eng = self.engine((spec, FakeClient()))
        self.assertIsNone(eng._retire_reason(spec, FakeClient()))


class SubstringFallbackTests(EngineHarness):
    """cclsp matched `name === q || name.includes(q)`. Keeping the
    capability without the silence: exact wins, substring only fills a
    gap that would otherwise be 'not found'."""

    def test_exact_match_wins_over_a_substring_candidate(self):
        eng = self.engine((self.spec("py"), FakeClient(symbols=[
            sym("open"), sym("did_open"), sym("open_all")])))
        res = eng.find_symbols(self.file, "open")
        self.assertEqual([s.name for s in res.items], ["open"],
                         "an exact name must never be shadowed")

    def test_substring_fills_the_gap_when_nothing_matches_exactly(self):
        eng = self.engine((self.spec("py"), FakeClient(symbols=[
            sym("did_open"), sym("did_close")])))
        res = eng.find_symbols(self.file, "open")
        self.assertEqual([s.name for s in res.items], ["did_open"])

    def test_substring_can_be_declined(self):
        eng = self.engine((self.spec("py"), FakeClient(symbols=[
            sym("did_open")])))
        self.assertEqual(
            eng.find_symbols(self.file, "open", substring=False).items, [])

    def test_kind_filter_applies_before_the_fallback(self):
        eng = self.engine((self.spec("py"), FakeClient(symbols=[
            sym("did_open", kind=12), sym("did_open", kind=6)])))
        res = eng.find_symbols(self.file, "open", kind=6)
        self.assertEqual(len(res.items), 1)
        self.assertEqual(res.items[0].kind_name, "method")

    def test_no_match_at_all_is_still_empty(self):
        eng = self.engine((self.spec("py"), FakeClient(symbols=[sym("zzz")])))
        res = eng.find_symbols(self.file, "qqq")
        self.assertEqual(res.items, [])
        self.assertTrue(res.trustworthy)


class CallHierarchyTests(EngineHarness):

    def test_prepare_and_query_happen_against_the_same_server(self):
        """The item must be the one that server produced; splitting the
        pair across servers yields an empty list, not an error."""
        call = CallHierarchyCall(
            item=CallHierarchyItem(name="caller", kind=12, uri="file:///c.py",
                                   range=rng(), selection=rng()))
        eng = self.engine((self.spec("py"), FakeClient(calls=[call])))
        res = eng.calls(self.file, 0, 4, direction="incoming")
        self.assertEqual(res.items[0].item.name, "caller")


class RenameTests(EngineHarness):

    def test_rename_returns_a_plan_and_writes_nothing(self):
        edit = WorkspaceEdit(edits=())
        eng = self.engine((self.spec("py"), FakeClient(edit=edit)))
        before = self.file.read_text(encoding="utf-8")
        res = eng.rename(self.file, 0, 4, "g")
        self.assertEqual(len(res.items), 1)
        self.assertEqual(self.file.read_text(encoding="utf-8"), before,
                         "the engine must not write")

    def test_server_refusing_the_rename_yields_no_plan(self):
        client = FakeClient(renameable=False)
        eng = self.engine((self.spec("py"), client))
        res = eng.rename(self.file, 0, 4, "g")
        self.assertEqual(res.items, [])
        self.assertEqual(client.rename_calls, [],
                         "must not ask for an edit it was told is invalid")


class WorkspaceSymbolTests(EngineHarness):

    def test_only_running_servers_are_asked_by_default(self):
        """Starting nine servers for one query is the preload mistake
        that stopped cclsp loading at all."""
        running = self.spec("py")
        idle = self.spec("go", binary="gopls")
        eng = Engine(self.root, [running, idle])
        client = FakeClient(ws_symbols=[sym("Engine", kind=5)])
        eng._client_for = lambda spec: client    # type: ignore
        eng._clients = {running: client}

        res = eng.workspace_symbols("Engine")
        self.assertEqual(len(res.items), 1)
        self.assertEqual(res.not_running, ("gopls",))
        self.assertFalse(res.trustworthy,
                         "an answer from some servers is not an answer")

    def test_no_servers_running_is_not_a_statement_about_the_workspace(self):
        eng = Engine(self.root, [self.spec("py")])
        res = eng.workspace_symbols("Anything")
        self.assertEqual(res.items, [])
        self.assertEqual(res.consulted, ())
        self.assertEqual(res.not_running, ("pyright-langserver",))
        self.assertFalse(res.trustworthy)

    def test_start_all_consults_every_configured_server(self):
        a, b = self.spec("py"), self.spec("go", binary="gopls")
        eng = self.engine((a, FakeClient(ws_symbols=[sym("X")])),
                          (b, FakeClient(ws_symbols=[sym("Y")])))
        res = eng.workspace_symbols("X", start_all=True)
        self.assertEqual(len(res.consulted), 2)
        self.assertEqual(res.not_running, ())
        self.assertTrue(res.trustworthy)


class RestartTests(EngineHarness):

    def test_restart_all_stops_every_client(self):
        c1, c2 = FakeClient(), FakeClient()
        a, b = self.spec("py"), self.spec("go", binary="gopls")
        eng = self.engine((a, c1), (b, c2))
        stopped = eng.restart()
        self.assertEqual(sorted(stopped), ["gopls", "pyright-langserver"])
        self.assertTrue(c1.stopped and c2.stopped)

    def test_restart_by_extension_leaves_the_others_alone(self):
        c1, c2 = FakeClient(), FakeClient()
        a, b = self.spec("py"), self.spec("go", binary="gopls")
        eng = self.engine((a, c1), (b, c2))
        self.assertEqual(eng.restart(["go"]), ["gopls"])
        self.assertTrue(c2.stopped)
        self.assertFalse(c1.stopped)

    def test_extension_accepts_a_leading_dot(self):
        c1 = FakeClient()
        a = self.spec("py")
        eng = self.engine((a, c1))
        self.assertEqual(eng.restart([".py"]), ["pyright-langserver"])

    def test_routing_is_dropped_so_the_next_change_reopens(self):
        """A stale route sends did_change to a client that no longer
        exists, which fails as 'did_change before did_open'."""
        client = FakeClient()
        a = self.spec("py")
        eng = self.engine((a, client))
        eng.did_open(self.file, "x")
        eng.restart(["py"])
        self.assertFalse(eng.did_change(self.file, "y"))

    def test_restarting_nothing_reports_nothing(self):
        eng = self.engine((self.spec("py"), FakeClient()))
        self.assertEqual(eng.restart(["rs"]), [])


if __name__ == "__main__":
    unittest.main()
