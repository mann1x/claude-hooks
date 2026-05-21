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


class TestLspClientWindowlessSpawn(unittest.TestCase):
    """``LspClient.start`` must pass ``CREATE_NO_WINDOW`` on Windows so
    the LSP child (pyright / gopls / clangd / etc) doesn't flash a
    console window when the daemon — itself spawned with
    ``CREATE_NO_WINDOW | DETACHED_PROCESS`` — launches it.

    Regression for the v1.9.0 visible-window class of bugs: a
    detached parent on Windows yields children that allocate a
    fresh console by default, so this flag must be on every LSP
    server spawn even though the daemon is already windowless.

    We mock ``subprocess.Popen`` and assert the kwargs that were
    passed — runs on all platforms regardless of host OS.
    """

    def _make_client(self) -> LspClient:
        # Build BEFORE patching os.name — Path() resolves at construct
        # time and would crash on a Linux host with os.name="nt"
        # (pathlib picks WindowsPath whose flavour fails to instantiate).
        return LspClient(
            command=["fake-lsp"],
            root_dir=".",
            startup_timeout=0.01,  # initialize will time out, that's fine
            request_timeout=0.01,
        )

    def _capture_popen_kwargs(self, *, os_name: str | None = None) -> dict:
        from unittest.mock import patch
        import subprocess as _sub

        client = self._make_client()
        captured: dict = {}

        def _fake_popen(cmd, **kwargs):  # noqa: ARG001
            captured.update(kwargs)
            raise FileNotFoundError("we only need the kwargs")

        patches = [
            patch(
                "claude_hooks.lsp_engine.lsp.subprocess.Popen",
                side_effect=_fake_popen,
            ),
        ]
        if os_name is not None:
            patches.append(patch("claude_hooks.lsp_engine.lsp.os.name", os_name))
            # ``subprocess.CREATE_NO_WINDOW`` is a Windows-only
            # constant; ``getattr(..., 0)`` in the production path
            # would otherwise resolve to 0 when this test runs on
            # Linux with a patched ``os.name == "nt"``. Inject the
            # canonical value (``0x08000000``) so the assertion
            # exercises the real branch.
            if os_name == "nt" and not hasattr(_sub, "CREATE_NO_WINDOW"):
                patches.append(
                    patch.object(_sub, "CREATE_NO_WINDOW", 0x08000000, create=True),
                )
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        with self.assertRaises(LspError):
            client.start()
        return captured

    def test_pipes_are_set(self) -> None:
        """Sanity: stdin/stdout/stderr must remain pipes for JSON-RPC."""
        import subprocess

        kw = self._capture_popen_kwargs()
        self.assertEqual(kw.get("stdin"), subprocess.PIPE)
        self.assertEqual(kw.get("stdout"), subprocess.PIPE)
        self.assertEqual(kw.get("stderr"), subprocess.PIPE)

    def test_no_detached_process_flag_on_windows(self) -> None:
        """``DETACHED_PROCESS`` would sever stdio and break LSP — must
        NOT appear in creationflags even on Windows."""
        import subprocess

        kw = self._capture_popen_kwargs(os_name="nt")
        flags = kw.get("creationflags", 0)
        detached = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        self.assertEqual(
            flags & detached, 0,
            "DETACHED_PROCESS must not be set — would break LSP stdio",
        )

    def test_windows_sets_create_no_window(self) -> None:
        """On Windows the daemon is detached, so children inherit no
        console — without CREATE_NO_WINDOW one is allocated for them
        and pops on the user's desktop."""
        import subprocess

        kw = self._capture_popen_kwargs(os_name="nt")
        flags = kw.get("creationflags", 0)
        # CREATE_NO_WINDOW is 0x08000000; assert at least that bit is on.
        expected = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        self.assertTrue(
            flags & expected,
            f"creationflags=0x{flags:08x} missing CREATE_NO_WINDOW "
            f"(0x{expected:08x}) — LSP children will pop console windows",
        )

    def test_posix_does_not_set_creationflags(self) -> None:
        """``creationflags`` is a Windows-only kwarg; must not appear
        on POSIX so ``subprocess.Popen`` stays portable."""
        kw = self._capture_popen_kwargs(os_name="posix")
        self.assertNotIn("creationflags", kw)


if __name__ == "__main__":
    unittest.main()
