"""End-to-end tests for ``claude_hooks.lsp_engine.lsp.LspClient``.

Spawns ``tests/lsp_engine_fake_server.py`` as the LSP child and
verifies the wire protocol works: ``initialize`` handshake completes,
``didOpen`` and ``didChange`` round-trip into ``publishDiagnostics``,
``shutdown`` + ``exit`` reap cleanly.

Skipped on Windows in Phase 0 — ``stdin``/``stdout`` framing is the
same, but the subprocess + signal-handling tests need a separate pass
to validate. Phase 4 (Windows parity) will revisit.
"""

from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from claude_hooks.lsp_engine.lsp import (
    Diagnostic,
    LspClient,
    LspError,
    language_id_for,
    path_to_uri,
)

_FAKE_SERVER = Path(__file__).parent / "lsp_engine_fake_server.py"


@unittest.skipIf(os.name == "nt", "Windows subprocess parity is Phase 4")
class TestLspClientLifecycle(unittest.TestCase):
    """The fake server should come up, handshake, accept docs, and
    shut down without leaving a zombie."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.client = LspClient(
            command=[sys.executable, str(_FAKE_SERVER)],
            root_dir=self.root,
            startup_timeout=5.0,
            request_timeout=2.0,
        )

    def tearDown(self) -> None:
        self.client.stop(timeout=2.0)

    def test_start_completes_initialize_handshake(self) -> None:
        self.client.start()
        # If we got here without raising, the initialize handshake
        # ran and the reader thread is up.
        self.assertIsNotNone(self.client._proc)
        self.assertTrue(self.client._reader_thread.is_alive())

    def test_did_open_publishes_diagnostics(self) -> None:
        self.client.start()
        target = self.root / "demo.py"
        target.write_text("x = 1\n", encoding="utf-8")
        self.client.did_open(target, "x = 1\n")
        diags = self.client.get_diagnostics(target, timeout=2.0)
        self.assertEqual(len(diags), 1)
        # Fake server encodes len(content) in the message.
        self.assertEqual(diags[0].message, "len=6")
        self.assertEqual(diags[0].source, "fake-lsp")
        self.assertEqual(diags[0].severity, 1)

    def test_did_change_replaces_diagnostics(self) -> None:
        self.client.start()
        target = self.root / "demo.py"
        self.client.did_open(target, "abc")
        first = self.client.get_diagnostics(target, timeout=2.0)
        self.assertEqual(first[0].message, "len=3")

        self.client.did_change(target, "longer content here")
        # Each did_change resets the diagnostics-ready event, so this
        # waits for the *new* publish, not the old cached one.
        second = self.client.get_diagnostics(target, timeout=2.0)
        self.assertEqual(second[0].message, "len=19")

    def test_did_change_before_did_open_raises(self) -> None:
        self.client.start()
        target = self.root / "demo.py"
        with self.assertRaises(LspError):
            self.client.did_change(target, "anything")

    def test_did_close_clears_open_state(self) -> None:
        self.client.start()
        target = self.root / "demo.py"
        self.client.did_open(target, "abc")
        self.client.did_close(target)
        # After close, did_change should fail with the same error as
        # if did_open had never been called.
        with self.assertRaises(LspError):
            self.client.did_change(target, "abc")

    def test_stop_reaps_child(self) -> None:
        self.client.start()
        proc = self.client._proc
        self.assertIsNotNone(proc)
        self.client.stop(timeout=2.0)
        # Polling proc.poll() returns the exit code once the process
        # has been reaped.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        self.assertIsNotNone(proc.poll(), "fake LSP did not exit")

    def test_context_manager_starts_and_stops(self) -> None:
        with LspClient(
            command=[sys.executable, str(_FAKE_SERVER)],
            root_dir=self.root,
            startup_timeout=5.0,
        ) as c:
            target = self.root / "demo.py"
            c.did_open(target, "hi")
            diags = c.get_diagnostics(target, timeout=2.0)
            self.assertEqual(diags[0].message, "len=2")


class TestLspClientStartupErrors(unittest.TestCase):
    def test_missing_binary_raises_clean_lsperror(self) -> None:
        client = LspClient(
            command=["/no/such/binary-for-lsp-test"],
            root_dir=Path("/tmp"),
        )
        with self.assertRaises(LspError) as ctx:
            client.start()
        self.assertIn("not found", str(ctx.exception))

    def test_double_start_raises(self) -> None:
        if os.name == "nt":  # pragma: no cover
            self.skipTest("Windows subprocess parity is Phase 4")
        client = LspClient(
            command=[sys.executable, str(_FAKE_SERVER)],
            root_dir=Path("/tmp"),
        )
        try:
            client.start()
            with self.assertRaises(LspError):
                client.start()
        finally:
            client.stop(timeout=2.0)


class TestLanguageIdMapping(unittest.TestCase):
    def test_known_extensions(self) -> None:
        self.assertEqual(language_id_for("a.py"), "python")
        self.assertEqual(language_id_for("a.go"), "go")
        self.assertEqual(language_id_for("a.rs"), "rust")
        self.assertEqual(language_id_for("a.cpp"), "cpp")
        self.assertEqual(language_id_for("a.cs"), "csharp")

    def test_uppercase_extension(self) -> None:
        self.assertEqual(language_id_for("A.PY"), "python")

    def test_unknown_extension_falls_back_to_plaintext(self) -> None:
        self.assertEqual(language_id_for("a.weird"), "plaintext")
        self.assertEqual(language_id_for("noextension"), "plaintext")


class TestPathToUri(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "Windows path-to-URI tested in Phase 4")
    def test_absolute_path_to_file_uri(self) -> None:
        uri = path_to_uri("/tmp/foo bar.py")
        self.assertTrue(uri.startswith("file://"))
        self.assertIn("foo%20bar.py", uri)

    def test_relative_path_is_resolved(self) -> None:
        # Whatever cwd we're in, the URI should be absolute.
        uri = path_to_uri("relative.py")
        self.assertTrue(uri.startswith("file://"))


class TestDiagnosticDataclass(unittest.TestCase):
    """Smoke check the dataclass field layout — these are part of the
    public surface (``LspClient.get_diagnostics`` returns them)."""

    def test_construct_with_required_fields(self) -> None:
        d = Diagnostic(
            uri="file:///tmp/x.py",
            severity=1,
            line=0,
            character=0,
            message="boom",
        )
        self.assertEqual(d.code, None)
        self.assertEqual(d.source, None)


if __name__ == "__main__":
    unittest.main()
