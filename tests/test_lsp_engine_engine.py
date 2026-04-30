"""Tests for the multi-LSP routing layer (``Engine``).

Uses the fake LSP server from ``tests/lsp_engine_fake_server.py`` so
the tests don't depend on any real language server being installed.
Two server specs (different extensions) are configured against the
same fake binary to verify per-language LSP isolation.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from claude_hooks.lsp_engine.config import LspServerSpec
from claude_hooks.lsp_engine.engine import Engine
from claude_hooks.lsp_engine.lsp import LspError

_FAKE_SERVER = Path(__file__).parent / "lsp_engine_fake_server.py"


def _fake_spec(extensions: tuple[str, ...]) -> LspServerSpec:
    return LspServerSpec(
        extensions=extensions,
        command=(sys.executable, str(_FAKE_SERVER)),
        root_dir=".",
    )


@unittest.skipIf(os.name == "nt", "Windows subprocess parity is Phase 4")
class TestEngineRouting(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.engine = Engine(
            project_root=self.root,
            servers=[_fake_spec(("py",)), _fake_spec(("rs",))],
            startup_timeout=5.0,
            request_timeout=2.0,
        )
        self.addCleanup(self.engine.shutdown)

    def test_no_clients_until_first_open(self) -> None:
        self.assertEqual(self.engine.active_servers(), [])
        self.assertEqual(self.engine.open_files(), [])

    def test_did_open_starts_only_matching_lsp(self) -> None:
        opened = self.engine.did_open(self.root / "x.py", "x = 1\n")
        self.assertTrue(opened)
        active = self.engine.active_servers()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].extensions, ("py",))

    def test_did_open_unknown_extension_returns_false(self) -> None:
        opened = self.engine.did_open(self.root / "README.md", "hi")
        self.assertFalse(opened)
        self.assertEqual(self.engine.active_servers(), [])

    def test_two_languages_get_separate_lsps(self) -> None:
        self.engine.did_open(self.root / "a.py", "x = 1")
        self.engine.did_open(self.root / "b.rs", "fn main() {}")
        active = self.engine.active_servers()
        self.assertEqual(len(active), 2)
        self.assertEqual(
            {spec.extensions for spec in active},
            {("py",), ("rs",)},
        )

    def test_diagnostics_round_trip(self) -> None:
        path = self.root / "demo.py"
        self.engine.did_open(path, "abc")
        diags = self.engine.get_diagnostics(path, timeout=2.0)
        self.assertEqual(len(diags), 1)
        self.assertEqual(diags[0].message, "len=3")

    def test_did_change_forwards_to_correct_lsp(self) -> None:
        path = self.root / "demo.py"
        self.engine.did_open(path, "first")
        forwarded = self.engine.did_change(path, "much longer second")
        self.assertTrue(forwarded)
        diags = self.engine.get_diagnostics(path, timeout=2.0)
        self.assertEqual(diags[0].message, "len=18")

    def test_did_change_without_open_returns_false(self) -> None:
        forwarded = self.engine.did_change(self.root / "ghost.py", "x")
        self.assertFalse(forwarded)

    def test_did_close_clears_routing(self) -> None:
        path = self.root / "demo.py"
        self.engine.did_open(path, "abc")
        self.assertEqual(len(self.engine.open_files()), 1)
        closed = self.engine.did_close(path)
        self.assertTrue(closed)
        self.assertEqual(self.engine.open_files(), [])
        # Subsequent did_change is rejected (returns False) — no
        # routing entry, no LSP forward.
        self.assertFalse(self.engine.did_change(path, "x"))

    def test_get_diagnostics_for_unopened_file_returns_empty(self) -> None:
        diags = self.engine.get_diagnostics(self.root / "ghost.py", timeout=0.1)
        self.assertEqual(diags, [])

    def test_shutdown_stops_all_clients(self) -> None:
        self.engine.did_open(self.root / "a.py", "x = 1")
        self.engine.did_open(self.root / "b.rs", "fn main() {}")
        self.assertEqual(len(self.engine.active_servers()), 2)
        self.engine.shutdown()
        self.assertEqual(self.engine.active_servers(), [])
        # Subsequent ops on a stopped engine raise LspError.
        with self.assertRaises(LspError):
            self.engine.did_open(self.root / "c.py", "x")


@unittest.skipIf(os.name == "nt", "Windows subprocess parity is Phase 4")
class TestEngineFailureModes(unittest.TestCase):
    def test_missing_binary_raises_clean_lsperror_on_first_open(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = Engine(
                project_root=root,
                servers=[
                    LspServerSpec(
                        extensions=("py",),
                        command=("/no/such/binary-for-engine-test",),
                    )
                ],
            )
            try:
                with self.assertRaises(LspError):
                    engine.did_open(root / "x.py", "x = 1")
                # Failed start did NOT cache a broken client; a retry
                # would re-attempt instead of returning the cached one.
                self.assertEqual(engine.active_servers(), [])
            finally:
                engine.shutdown()


if __name__ == "__main__":
    unittest.main()
