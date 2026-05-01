"""Phase 4 dispatch + address-helper tests.

Real Windows pipe runtime can't be exercised from a Linux test
host (``multiprocessing.connection.Listener(family="AF_PIPE")`` is
Windows-only), but the dispatch layer is testable here:

- The address helpers (``is_windows_address``, ``windows_pipe_name_for``,
  ``socket_path_for``) are pure functions and check out on either
  platform.
- ``IpcServer`` / ``IpcClient`` selection happens in the constructor
  before any backend code runs; we patch ``os.name`` and verify the
  right impl is created.

True end-to-end coverage of the Windows backend lands when a
Windows host runs the suite (CI matrix item, not part of the
default Linux pytest run).
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from claude_hooks.lsp_engine import ipc as ipc_mod
from claude_hooks.lsp_engine.daemon import socket_path_for
from claude_hooks.lsp_engine.ipc import (
    IpcClient,
    IpcServer,
    _UnixSocketClient,
    _UnixSocketServer,
    _WindowsPipeClient,
    _WindowsPipeServer,
    is_windows_address,
    windows_pipe_name_for,
)


class TestAddressHelpers(unittest.TestCase):
    def test_is_windows_address_recognizes_pipe_prefix(self) -> None:
        self.assertTrue(is_windows_address(r"\\.\pipe\foo"))
        self.assertTrue(
            is_windows_address(r"\\.\pipe\claude-hooks-lsp-engine-deadbeef")
        )

    def test_is_windows_address_false_for_unix_paths(self) -> None:
        self.assertFalse(is_windows_address("/tmp/daemon.sock"))
        self.assertFalse(is_windows_address(Path("/var/run/x.sock")))
        self.assertFalse(is_windows_address("daemon.sock"))

    def test_windows_pipe_name_for_is_stable(self) -> None:
        with TemporaryDirectory() as tmp:
            p = Path(tmp) / "project"
            p.mkdir()
            name1 = windows_pipe_name_for(p)
            name2 = windows_pipe_name_for(p)
            self.assertEqual(name1, name2)
            self.assertTrue(name1.startswith(r"\\.\pipe\claude-hooks-lsp-engine-"))

    def test_windows_pipe_name_for_differs_per_project(self) -> None:
        with TemporaryDirectory() as tmp:
            a = Path(tmp) / "alpha"
            b = Path(tmp) / "beta"
            a.mkdir()
            b.mkdir()
            self.assertNotEqual(
                windows_pipe_name_for(a),
                windows_pipe_name_for(b),
            )

    def test_socket_path_for_returns_pipe_on_windows(self) -> None:
        # Patching os.name globally on POSIX breaks pathlib (it tries to
        # instantiate WindowsPath which is unsupported here), so this
        # test patches just the daemon module's os reference. The same
        # patch on a real Windows host has no such side-effect.
        from claude_hooks.lsp_engine import daemon as daemon_mod
        with TemporaryDirectory() as tmp:
            with patch.object(daemon_mod.os, "name", "nt"):
                addr = socket_path_for(tmp)
            self.assertIsInstance(addr, str)
            self.assertTrue(addr.startswith("\\\\.\\pipe\\"))

    @unittest.skipIf(
        os.name == "nt",
        "POSIX-direction assertion: patching os.name='posix' on a Windows host "
        "trips pathlib's PosixPath guard. The Windows-direction test covers "
        "the inverse, and the Windows host never reaches this dispatch in real use.",
    )
    def test_socket_path_for_returns_path_on_posix(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch.object(os, "name", "posix"):
                addr = socket_path_for(tmp)
            self.assertIsInstance(addr, Path)
            self.assertTrue(str(addr).endswith("daemon.sock"))


class TestIpcServerDispatch(unittest.TestCase):
    """``IpcServer`` should pick its backend based on ``os.name``."""

    @unittest.skipIf(
        os.name == "nt",
        "POSIX-direction assertion: patching os.name='posix' on a Windows host "
        "trips pathlib's PosixPath guard. test_nt_platform_picks_pipe_backend "
        "covers the inverse, and address-based detection is platform-agnostic.",
    )
    def test_posix_picks_unix_socket_backend(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch.object(os, "name", "posix"):
                with patch.object(ipc_mod, "is_windows_address", return_value=False):
                    server = IpcServer(
                        Path(tmp) / "x.sock",
                        handler=lambda req: {"id": req.get("id"), "ok": True},
                    )
            self.assertIsInstance(server._impl, _UnixSocketServer)

    def test_windows_address_picks_pipe_backend_even_on_posix(self) -> None:
        """Address-based detection: a `\\\\.\\pipe\\` literal forces the
        Windows backend regardless of platform — useful for testing
        the dispatch on a Linux host without patching os.name globally.
        """
        server = IpcServer(
            r"\\.\pipe\test-pipe",
            handler=lambda req: {"id": req.get("id"), "ok": True},
        )
        self.assertIsInstance(server._impl, _WindowsPipeServer)
        # We do NOT call .start() — Listener(family="AF_PIPE") would
        # fail on Linux. The point is the dispatch.

    def test_nt_platform_picks_pipe_backend(self) -> None:
        with patch.object(os, "name", "nt"):
            server = IpcServer(
                r"\\.\pipe\test-pipe",
                handler=lambda req: {"id": req.get("id"), "ok": True},
            )
        self.assertIsInstance(server._impl, _WindowsPipeServer)


class TestIpcClientDispatch(unittest.TestCase):
    @unittest.skipIf(
        os.name == "nt",
        "POSIX-direction assertion: patching os.name='posix' on a Windows host "
        "trips pathlib's PosixPath guard. test_nt_platform_picks_pipe_client "
        "covers the inverse, and address-based detection is platform-agnostic.",
    )
    def test_posix_picks_unix_socket_client(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch.object(os, "name", "posix"):
                with patch.object(ipc_mod, "is_windows_address", return_value=False):
                    c = IpcClient(Path(tmp) / "x.sock")
            self.assertIsInstance(c._impl, _UnixSocketClient)

    def test_windows_address_picks_pipe_client_even_on_posix(self) -> None:
        c = IpcClient(r"\\.\pipe\test-pipe")
        self.assertIsInstance(c._impl, _WindowsPipeClient)

    def test_nt_platform_picks_pipe_client(self) -> None:
        with patch.object(os, "name", "nt"):
            c = IpcClient(r"\\.\pipe\test-pipe")
        self.assertIsInstance(c._impl, _WindowsPipeClient)


class TestWindowsBackendErrorOnPosixIsClean(unittest.TestCase):
    """If a user tries to actually run the Windows backend on a
    POSIX host (e.g. by passing a pipe address), they should get a
    clear failure from ``multiprocessing.connection`` rather than a
    cryptic AttributeError or hang.
    """

    def test_windows_pipe_listener_fails_loudly_on_posix(self) -> None:
        if os.name == "nt":  # pragma: no cover — POSIX-only invariant
            self.skipTest("Windows hosts can actually start the listener")
        server = IpcServer(
            r"\\.\pipe\test-pipe",
            handler=lambda req: {"id": req.get("id"), "ok": True},
        )
        # On POSIX, AF_PIPE is unsupported — Listener raises ValueError
        # ("family AF_PIPE is not recognized"). We expect a raise; the
        # *kind* of raise is implementation-defined across Python
        # versions, so just assert that something fails fast.
        with self.assertRaises(Exception):
            server.start()


if __name__ == "__main__":
    unittest.main()
