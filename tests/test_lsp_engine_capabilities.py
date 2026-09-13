"""Capability capture, stderr drain, pull diagnostics, multi-server routing.

All four exist because of one observation: bash-language-server without
shellcheck starts, completes its handshake, advertises a full capability
list, and then publishes an empty diagnostic list forever — which is
byte-identical to "this file is clean". The protocol gave it nowhere to
say otherwise, and the engine gave it nowhere to be heard.
"""
from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

from claude_hooks.lsp_engine.config import LspServerSpec, resolve_servers_for_path
from claude_hooks.lsp_engine.lsp import LspClient, Diagnostic


def _client(**kw) -> LspClient:
    return LspClient(command=["fake-lsp"], root_dir=".",
                     startup_timeout=0.01, request_timeout=0.01, **kw)


class TestCapabilityCapture(unittest.TestCase):
    def test_capabilities_start_empty(self) -> None:
        self.assertEqual(_client().server_capabilities, {})

    def test_supports_reads_the_advertised_set(self) -> None:
        c = _client()
        c._server_capabilities = {"hoverProvider": True,
                                  "renameProvider": {"prepareProvider": True},
                                  "codeLensProvider": False}
        self.assertTrue(c.supports("hoverProvider"))
        self.assertTrue(c.supports("renameProvider"))   # dict means present
        self.assertFalse(c.supports("codeLensProvider"))
        self.assertFalse(c.supports("nothingProvider"))

    def test_capabilities_are_a_copy(self) -> None:
        """A caller must not be able to edit the server's declaration."""
        c = _client()
        c._server_capabilities = {"hoverProvider": True}
        c.server_capabilities["hoverProvider"] = False
        self.assertTrue(c.supports("hoverProvider"))

    def test_pull_support_follows_diagnostic_provider(self) -> None:
        c = _client()
        self.assertFalse(c.supports_pull_diagnostics)
        c._server_capabilities = {"diagnosticProvider": {"interFileDependencies": True}}
        self.assertTrue(c.supports_pull_diagnostics)


class TestClientDeclaresPullSupport(unittest.TestCase):
    def test_initialize_declares_textdocument_diagnostic(self) -> None:
        """A server only advertises `diagnosticProvider` when the client
        declares support. Omitting it made every server look push-only."""
        import inspect
        from claude_hooks.lsp_engine import lsp as mod
        src = inspect.getsource(mod.LspClient.start)
        self.assertIn('"diagnostic"', src)
        self.assertIn('"publishDiagnostics"', src)


class TestStderrDrain(unittest.TestCase):
    """An undrained PIPE is a deadlock: ~64 KB on Linux, and these are
    single-threaded event loops."""

    def _drain(self, lines: list[bytes]) -> LspClient:
        c = _client()

        class FakeProc:
            stderr = iter(lines)

            class _S:
                def __init__(self, data): self._it = iter(data)
                def readline(self):
                    try:
                        return next(self._it)
                    except StopIteration:
                        return b""
        fake = FakeProc()
        fake.stderr = FakeProc._S(lines)
        c._proc = fake
        c._drain_stderr()
        return c

    def test_lines_are_kept_for_diagnosis(self) -> None:
        c = self._drain([b"starting\n", b"ready\n"])
        self.assertEqual(c.stderr_tail(), ["starting", "ready"])

    def test_blank_lines_are_dropped(self) -> None:
        c = self._drain([b"\n", b"real\n", b"  \n"])
        self.assertEqual(c.stderr_tail(), ["real"])

    def test_tail_is_bounded(self) -> None:
        """A breadcrumb, not a log file — the full stream goes to the
        engine log."""
        from claude_hooks.lsp_engine.lsp import _STDERR_TAIL_LINES
        c = self._drain([f"line{i}\n".encode() for i in range(_STDERR_TAIL_LINES * 3)])
        self.assertEqual(len(c.stderr_tail()), _STDERR_TAIL_LINES)

    def test_complaints_are_promoted_to_warning(self) -> None:
        """A degraded server explains itself here or nowhere."""
        with self.assertLogs("claude_hooks.lsp_engine.lsp", level="WARNING") as cm:
            self._drain([b"shellcheck not found on PATH\n"])
        self.assertTrue(any("shellcheck" in m for m in cm.output))

    def test_ordinary_chatter_is_not_a_warning(self) -> None:
        import logging
        logger = logging.getLogger("claude_hooks.lsp_engine.lsp")
        with patch.object(logger, "warning") as warn:
            self._drain([b"indexing 40%\n"])
        warn.assert_not_called()

    def test_closed_pipe_during_shutdown_is_not_an_error(self) -> None:
        c = _client()

        class Boom:
            class _S:
                def readline(self): raise ValueError("closed")
            stderr = _S()
        c._proc = Boom()
        c._drain_stderr()      # must not raise


class TestPullDiagnostics(unittest.TestCase):
    def test_pull_result_is_parsed_and_cached(self) -> None:
        c = _client()
        c._server_capabilities = {"diagnosticProvider": True}
        item = {"range": {"start": {"line": 2, "character": 4}},
                "severity": 1, "message": "boom", "source": "srv"}
        with patch.object(c, "_send_request",
                          return_value={"kind": "full", "items": [item]}):
            got = c.get_diagnostics("/tmp/x.py", timeout=0.1)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].message, "boom")
        self.assertEqual(got[0].line, 2)

    def test_failed_pull_returns_none_not_empty(self) -> None:
        """Returning [] would report a transport failure as a clean
        file — the exact conflation this whole area keeps producing."""
        from claude_hooks.lsp_engine.lsp import LspError, uri_key, path_to_uri
        c = _client()
        c._server_capabilities = {"diagnosticProvider": True}
        with patch.object(c, "_send_request", side_effect=LspError("nope")):
            out = c._pull_diagnostics("/tmp/x.py", uri_key(path_to_uri("/tmp/x.py")),
                                      timeout=0.01)
        self.assertIsNone(out)

    def test_push_path_is_used_when_pull_unsupported(self) -> None:
        c = _client()
        self.assertFalse(c.supports_pull_diagnostics)
        with patch.object(c, "_pull_diagnostics") as pull:
            c.get_diagnostics("/tmp/x.py", timeout=0.01)
        pull.assert_not_called()

    def test_both_paths_parse_identically(self) -> None:
        """Push and pull carry the same item shape; letting them drift
        would render a server's diagnostics differently depending on
        which way they arrived."""
        item = {"range": {"start": {"line": 1, "character": 2}},
                "severity": 2, "message": "m", "code": "C1", "source": "s"}
        parsed = LspClient._parse_diagnostics("file:///x", [item])
        self.assertEqual(len(parsed), 1)
        self.assertEqual((parsed[0].line, parsed[0].code, parsed[0].source),
                         (1, "C1", "s"))


class TestMultiServerRouting(unittest.TestCase):
    """One file is not one language: .html carries JS and CSS, .vue and
    .svelte carry all three, .md carries whatever its fences say."""

    def setUp(self) -> None:
        self.html = LspServerSpec(extensions=("html",), command=("html-ls",))
        self.ts = LspServerSpec(extensions=("html", "ts", "js"),
                                command=("ts-ls",))
        self.servers = [self.html, self.ts]

    def test_a_file_can_have_several_servers(self) -> None:
        got = resolve_servers_for_path("a.html", self.servers)
        self.assertEqual([s.command[0] for s in got], ["html-ls", "ts-ls"])

    def test_config_order_is_preserved(self) -> None:
        got = resolve_servers_for_path("a.html", [self.ts, self.html])
        self.assertEqual([s.command[0] for s in got], ["ts-ls", "html-ls"])

    def test_single_claimant_still_resolves_alone(self) -> None:
        got = resolve_servers_for_path("a.ts", self.servers)
        self.assertEqual([s.command[0] for s in got], ["ts-ls"])

    def test_unclaimed_file_yields_nothing(self) -> None:
        self.assertEqual(resolve_servers_for_path("README.md", self.servers), [])

    def test_legacy_single_resolver_still_returns_the_first(self) -> None:
        from claude_hooks.lsp_engine.config import resolve_server_for_path
        self.assertIs(resolve_server_for_path("a.html", self.servers), self.html)


if __name__ == "__main__":
    unittest.main()
