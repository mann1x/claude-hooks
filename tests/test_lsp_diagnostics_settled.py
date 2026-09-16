"""An empty diagnostics list must never be rendered as good news.

The opencoti session validated our MCP server on a cosmocc/CUDA tree on
2026-09-16 and found the one remaining hole: ``get_diagnostics`` on a
large C++ TU answered *"No diagnostics for llama-kv-cache-kvarn.cpp."*
while driving the same clangd by hand published **24** diagnostics,
including a severity-1 error.

Two causes, both covered here:

1. the wait was a flat 2 s, and clangd builds an AST and runs
   clang-tidy before its first push; and
2. the engine prefers pull diagnostics precisely to avoid this
   ambiguity, but clangd answers ``diagnosticProvider: null`` even when
   the client declares ``textDocument.diagnostic``, so for C/C++ the
   timed push wait is the *normal* road, not a rare fallback.
"""
from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from claude_hooks.lsp_engine.lsp import (  # noqa: E402
    _DIAGNOSTICS_TIMEOUT_CEILING,
    _DIAGNOSTICS_COLD_FLOOR,
    _DIAGNOSTICS_TIMEOUT_DEFAULT,
    Diagnostic,
    DiagnosticsResult,
    LspClient,
)


def _client(command) -> LspClient:
    """An unstarted client — enough for the timeout policy, which is
    pure arithmetic over the binary name."""
    return LspClient(command=command, root_dir=REPO)


class TimeoutPolicyTests(unittest.TestCase):

    def test_clangd_gets_far_longer_than_the_old_flat_two_seconds(self):
        self.assertGreaterEqual(_client(["clangd"]).diagnostics_timeout(),
                                15.0)

    def test_fast_servers_keep_a_short_budget_once_measured(self):
        # Still not merely a preference: this wait runs after every
        # edit, so giving pyright clangd's standing budget would stall
        # the hook. What changed is that it applies from the SECOND
        # publish — the first one has no measurement behind it.
        for name in ("pyright-langserver", "gopls",
                     "typescript-language-server"):
            with self.subTest(server=name):
                c = _client([name])
                c._observed_publish_latency = 0.4
                self.assertEqual(c.diagnostics_timeout(),
                                 _DIAGNOSTICS_TIMEOUT_DEFAULT)

    def test_first_publish_gets_the_cold_floor(self):
        # The adaptive budget raises itself from observed latency, and
        # can only observe a request that finished. A server whose cold
        # publish exceeds the default would otherwise time out forever
        # and never learn. Measured on a real monorepo: cold 3.78 s
        # against a 5 s default, warm 0.00 s — the same thin margin
        # clangd had at 2.74 s under 2.00 s.
        for name in ("pyright-langserver", "typescript-language-server"):
            with self.subTest(server=name):
                self.assertEqual(_client([name]).diagnostics_timeout(),
                                 _DIAGNOSTICS_COLD_FLOOR)

    def test_the_cold_floor_costs_nothing_when_the_server_is_quick(self):
        # It bounds the wait, it does not schedule one: a server that
        # publishes in 0.2 s returns in 0.2 s under any budget. This
        # pins the intent, since the number looks alarming next to a
        # post-edit hook until you know that.
        c = _client(["typescript-language-server"])
        self.assertGreater(c.diagnostics_timeout(), 5.0)
        c._observed_publish_latency = 0.2
        self.assertEqual(c.diagnostics_timeout(),
                         _DIAGNOSTICS_TIMEOUT_DEFAULT)

    def test_an_operator_value_outranks_the_cold_floor(self):
        # Naming a value for this server in cclsp.json is a statement
        # about this project; a floor picked from somebody else's
        # monorepo must not override it. The first version of this
        # change did exactly that.
        c = LspClient(command=["typescript-language-server"], root_dir=REPO,
                      diagnostics_timeout=3.0)
        self.assertEqual(c.diagnostics_timeout(), 3.0)

    def test_an_explicit_caller_budget_still_wins(self):
        # The PostToolUse hook passes 2.0 deliberately; a table must not
        # override a caller that knows its own latency requirement.
        self.assertEqual(_client(["clangd"]).diagnostics_timeout(2.0), 2.0)

    def test_spec_override_beats_the_table(self):
        c = LspClient(command=["clangd"], root_dir=REPO,
                      diagnostics_timeout=3.0)
        self.assertEqual(c.diagnostics_timeout(), 3.0)

    def test_measurement_raises_the_floor(self):
        c = _client(["pyright-langserver"])
        c._observed_publish_latency = 0.5      # measured, and quick
        self.assertEqual(c.diagnostics_timeout(), 5.0)
        c._observed_publish_latency = 9.0      # measured, and slow
        self.assertEqual(c.diagnostics_timeout(), 18.0)

    def test_measurement_is_capped(self):
        c = _client(["clangd"])
        c._observed_publish_latency = 10_000.0
        self.assertEqual(c.diagnostics_timeout(),
                         _DIAGNOSTICS_TIMEOUT_CEILING)

    def test_full_path_still_resolves_the_server_name(self):
        # The real config pins an absolute path
        # (.toolchains/clangd_22.1.6/bin/clangd) — matching on the bare
        # command string would have missed it and silently used 5 s.
        c = _client(["/srv/dev/.toolchains/clangd_22.1.6/bin/clangd",
                     "--background-index"])
        self.assertEqual(c.server_name, "clangd")
        self.assertGreaterEqual(c.diagnostics_timeout(), 15.0)

    def test_windows_exe_suffix_resolves_too(self):
        self.assertEqual(_client([r"C:\tools\clangd.exe"]).server_name,
                         "clangd")


class ResultTests(unittest.TestCase):
    """``DiagnosticsResult`` must stay usable where a list was."""

    def test_empty_and_unsettled_are_different_objects(self):
        clean = DiagnosticsResult(items=[], settled=True)
        quiet = DiagnosticsResult(items=[], settled=False)
        self.assertEqual(len(clean), len(quiet))
        self.assertNotEqual(clean.settled, quiet.settled)

    def test_iterates_and_counts_like_a_list(self):
        d = Diagnostic(uri="file:///x", severity=1, line=0, character=0,
                       message="boom")
        res = DiagnosticsResult(items=[d], settled=True)
        self.assertEqual(len(res), 1)
        self.assertEqual([x.message for x in res], ["boom"])
        self.assertTrue(res)

    def test_empty_is_falsey_regardless_of_settled(self):
        self.assertFalse(DiagnosticsResult(items=[], settled=True))
        self.assertFalse(DiagnosticsResult(items=[], settled=False))


class WaitBehaviourTests(unittest.TestCase):
    """Drive ``get_diagnostics_result`` against a client whose event is
    never set — the exact shape of a server still thinking."""

    def setUp(self):
        self.c = _client(["clangd"])
        self.path = REPO / "README.md"

    def _key(self):
        from claude_hooks.lsp_engine.lsp import path_to_uri, uri_key
        return uri_key(path_to_uri(self.path))

    def test_timeout_reports_unsettled_not_clean(self):
        with mock.patch.object(type(self.c), "supports_pull_diagnostics",
                               property(lambda _s: False)):
            res = self.c.get_diagnostics_result(self.path, timeout=0.01)
        self.assertEqual(res.items, [])
        self.assertFalse(res.settled,
                         "a timeout must not be reported as a clean file")
        self.assertEqual(res.server, "clangd")
        self.assertEqual(res.timeout, 0.01)

    def test_a_publish_reports_settled(self):
        key = self._key()
        d = Diagnostic(uri="file:///x", severity=1, line=0, character=0,
                       message="real")
        with self.c._diag_lock:
            self.c._diagnostics[key] = [d]
            self.c._diagnostics_event.setdefault(
                key, threading.Event()).set()
        with mock.patch.object(type(self.c), "supports_pull_diagnostics",
                               property(lambda _s: False)):
            res = self.c.get_diagnostics_result(self.path, timeout=0.01)
        self.assertTrue(res.settled)
        self.assertEqual([x.message for x in res.items], ["real"])
        self.assertEqual(res.source, "cached")

    def test_an_empty_publish_is_settled_and_clean(self):
        # The case that must stay silent: the server answered, and the
        # answer was "nothing wrong". Conflating this with a timeout in
        # either direction defeats the point.
        key = self._key()
        with self.c._diag_lock:
            self.c._diagnostics[key] = []
            self.c._diagnostics_event.setdefault(
                key, threading.Event()).set()
        with mock.patch.object(type(self.c), "supports_pull_diagnostics",
                               property(lambda _s: False)):
            res = self.c.get_diagnostics_result(self.path, timeout=0.01)
        self.assertTrue(res.settled)
        self.assertEqual(res.items, [])

    def test_get_diagnostics_still_returns_a_plain_list(self):
        with mock.patch.object(type(self.c), "supports_pull_diagnostics",
                               property(lambda _s: False)):
            got = self.c.get_diagnostics(self.path, timeout=0.01)
        self.assertIsInstance(got, list)
        self.assertEqual(got, [])


class ReopenPreservesDiagnosticsTests(unittest.TestCase):
    """The actual root cause behind the opencoti report.

    ``get_diagnostics`` calls ``did_open`` first. The old unconditional
    ``did_open`` cleared the cached diagnostics and bumped the version
    — but LSP forbids opening a document twice and clangd ignores the
    duplicate, so no republish ever came. A second call therefore threw
    away a real answer and reported the resulting empty list as clean.
    By the time they asked, hover had already opened the file.
    """

    def setUp(self):
        self.c = _client(["clangd"])
        self.path = REPO / "README.md"
        self.sent: list[tuple[str, dict]] = []
        self.c._send_notification = lambda m, p: self.sent.append((m, p))

    def _key(self):
        from claude_hooks.lsp_engine.lsp import path_to_uri, uri_key
        return uri_key(path_to_uri(self.path))

    def _publish(self, message="real"):
        d = Diagnostic(uri="file:///x", severity=1, line=0, character=0,
                       message=message)
        with self.c._diag_lock:
            self.c._diagnostics[self._key()] = [d]
            self.c._diagnostics_event.setdefault(
                self._key(), threading.Event()).set()

    def test_reopening_identical_content_keeps_the_diagnostics(self):
        self.c.did_open(self.path, "same")
        self._publish()
        self.c.did_open(self.path, "same")
        with mock.patch.object(type(self.c), "supports_pull_diagnostics",
                               property(lambda _s: False)):
            res = self.c.get_diagnostics_result(self.path, timeout=0.01)
        self.assertTrue(res.settled,
                        "a repeat did_open discarded a real answer")
        self.assertEqual([d.message for d in res.items], ["real"])

    def test_reopening_identical_content_sends_nothing(self):
        self.c.did_open(self.path, "same")
        self.sent.clear()
        self.c.did_open(self.path, "same")
        self.assertEqual(self.sent, [],
                         "a duplicate didOpen is a protocol violation the "
                         "server is entitled to ignore")

    def test_changed_content_becomes_a_did_change(self):
        self.c.did_open(self.path, "before")
        self._publish()
        self.sent.clear()
        self.c.did_open(self.path, "after")
        self.assertEqual([m for m, _ in self.sent],
                         ["textDocument/didChange"])
        # And the stale answer must be dropped — the file really did
        # change, so the old diagnostics are no longer about it.
        with mock.patch.object(type(self.c), "supports_pull_diagnostics",
                               property(lambda _s: False)):
            res = self.c.get_diagnostics_result(self.path, timeout=0.01)
        self.assertFalse(res.settled)

    def test_did_change_updates_the_content_cache(self):
        # Otherwise the next did_open compares against a stale copy and
        # re-sends a change nobody made.
        self.c.did_open(self.path, "v1")
        self.c.did_change(self.path, "v2")
        self.sent.clear()
        self.c.did_open(self.path, "v2")
        self.assertEqual(self.sent, [])

    def test_close_forgets_the_content(self):
        self.c.did_open(self.path, "v1")
        self.c.did_close(self.path)
        self.sent.clear()
        self.c.did_open(self.path, "v1")
        self.assertEqual([m for m, _ in self.sent],
                         ["textDocument/didOpen"])

    def test_first_open_still_opens(self):
        self.c.did_open(self.path, "v1")
        self.assertEqual([m for m, _ in self.sent],
                         ["textDocument/didOpen"])


class McpRenderingTests(unittest.TestCase):
    """The tool layer is the caller that was getting this wrong."""

    def _render(self, res):
        from claude_hooks.lsp_mcp.server import LspMcpServer
        srv = LspMcpServer.__new__(LspMcpServer)
        entry = mock.MagicMock()
        entry.engine.did_open.return_value = True
        entry.engine.get_diagnostics_result.return_value = res
        srv.registry = mock.MagicMock()
        srv.registry.for_path.return_value = entry
        with mock.patch.object(LspMcpServer, "_file_path",
                               lambda _s, _a: REPO / "README.md"):
            return srv._tool_get_diagnostics({"file_path": "README.md"})

    def test_timeout_is_not_rendered_as_no_diagnostics(self):
        out = self._render(DiagnosticsResult(
            items=[], settled=False, timeout=15.0, server="clangd"))
        self.assertIn("NO ANSWER YET", out)
        self.assertIn("clangd", out)
        self.assertIn("not a statement about the code", out.lower())
        self.assertNotIn("No diagnostics for", out)

    def test_a_genuinely_clean_file_still_says_so(self):
        out = self._render(DiagnosticsResult(items=[], settled=True))
        self.assertIn("No diagnostics for", out)
        self.assertNotIn("NO ANSWER", out)

    def test_partial_merge_is_flagged(self):
        d = Diagnostic(uri="file:///x", severity=2, line=3, character=4,
                       message="unused")
        out = self._render(DiagnosticsResult(
            items=[d], settled=False, timeout=5.0,
            server="typescript-language-server"))
        self.assertIn("PARTIAL", out)
        self.assertIn("typescript-language-server", out)
        self.assertIn("unused", out)


class HookRenderingTests(unittest.TestCase):
    """PostToolUse renders a clean file as *silence*, so an unsettled
    result rendered the same way is invisible — the worst version."""

    def _block(self, **kw):
        from claude_hooks import lsp_integration as li
        return li.format_diagnostics_block(
            path=REPO / "claude_hooks" / "lsp_integration.py",
            diagnostics=[], stale=False, **kw)

    def test_unsettled_produces_a_block(self):
        out = self._block(settled=False, server="clangd", wait_budget=2.0)
        self.assertIsNotNone(out)
        self.assertIn("No answer yet", out)
        self.assertIn("clangd", out)

    def test_settled_and_clean_stays_silent(self):
        # Trustworthy silence must stay silent or the warning becomes
        # noise and stops being read.
        self.assertIsNone(self._block(settled=True))

    def test_default_is_settled_for_older_callers(self):
        self.assertIsNone(self._block())


class DaemonContractTests(unittest.TestCase):

    def test_client_defaults_settled_true_when_daemon_is_old(self):
        from claude_hooks.lsp_engine.client import LspEngineClient
        c = LspEngineClient.__new__(LspEngineClient)
        with mock.patch.object(LspEngineClient, "_call",
                               lambda *_a, **_k: {"diagnostics": [],
                                                  "stale": False}):
            diags, stale, meta = c.diagnostics_full("x.py")
        self.assertEqual(diags, [])
        self.assertFalse(stale)
        self.assertTrue(meta["settled"],
                        "a daemon too old to report settledness must not "
                        "make every file look unanalysed")

    def test_client_passes_settled_through(self):
        from claude_hooks.lsp_engine.client import LspEngineClient
        c = LspEngineClient.__new__(LspEngineClient)
        with mock.patch.object(LspEngineClient, "_call",
                               lambda *_a, **_k: {
                                   "diagnostics": [], "stale": False,
                                   "settled": False, "diag_server": "clangd",
                                   "diag_timeout_s": 15.0}):
            _d, _s, meta = c.diagnostics_full("x.cpp")
        self.assertFalse(meta["settled"])
        self.assertEqual(meta["server"], "clangd")
        self.assertEqual(meta["timeout"], 15.0)


class ConfigTests(unittest.TestCase):

    def _load(self, tmp: Path, entry: dict):
        import json
        from claude_hooks.lsp_engine.config import load_cclsp_config
        p = tmp / "cclsp.json"
        p.write_text(json.dumps({"servers": [entry]}), encoding="utf-8")
        return load_cclsp_config(p)

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_diagnostics_timeout_is_read(self):
        specs = self._load(self.tmp, {
            "extensions": ["cpp"], "command": ["clangd"],
            "diagnosticsTimeout": 25})
        self.assertEqual(specs[0].diagnostics_timeout, 25.0)

    def test_absent_means_none_not_zero(self):
        specs = self._load(self.tmp, {
            "extensions": ["cpp"], "command": ["clangd"]})
        self.assertIsNone(specs[0].diagnostics_timeout)

    def test_zero_is_refused(self):
        from claude_hooks.lsp_engine.config import CclspConfigError
        with self.assertRaises(CclspConfigError) as ctx:
            self._load(self.tmp, {"extensions": ["cpp"],
                                  "command": ["clangd"],
                                  "diagnosticsTimeout": 0})
        self.assertIn("positive", str(ctx.exception))

    def test_nonsense_is_refused(self):
        from claude_hooks.lsp_engine.config import CclspConfigError
        with self.assertRaises(CclspConfigError):
            self._load(self.tmp, {"extensions": ["cpp"],
                                  "command": ["clangd"],
                                  "diagnosticsTimeout": "soon"})


if __name__ == "__main__":
    unittest.main()
