"""Asking twice about unchanged content should cost once.

The MCP server and the PostToolUse hook share one engine, so the same
file gets asked about twice whenever the model inspects what it just
edited. Keyed on the content stamp, because that is what makes the
replay provably identical rather than merely probably identical.

The guard that matters: never replay an UNSETTLED result. A cold server
publishes nothing and then publishes everything for the same content
once it has indexed, so pinning the empty answer would turn a timing
artefact into a persistent wrong one — the failure this engine exists
to prevent.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from claude_hooks.lsp_engine.daemon import Daemon  # noqa: E402
from claude_hooks.lsp_engine.pool import EnginePool  # noqa: E402
from claude_hooks.lsp_engine.lsp import Diagnostic, DiagnosticsResult  # noqa: E402


class _Engine:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def get_diagnostics_result(self, path, timeout=None):
        self.calls += 1
        return self.result


class _Locks:
    def query(self, session, path, timeout_ms=0):
        return True, []


class DedupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.file = Path(self.tmp.name) / "x.py"
        self.file.write_text("x = 1\n", encoding="utf-8")
        self.addCleanup(self.tmp.cleanup)

    def daemon(self, *, settled=True, items=None):
        d = Daemon.__new__(Daemon)
        d._served = {}
        d._lock_manager = _Locks()
        d._compile = None
        d._engine_config = type("C", (), {
            "session_locks": type("S", (), {"query_timeout_ms": 500})()})()
        res = DiagnosticsResult(
            items=items if items is not None else [],
            settled=settled, server="pyright-langserver", timeout=8.0)
        # The daemon routes per file now, so the fake goes in as the
        # pool's factory rather than as a single attribute.
        self.engine = _Engine(res)
        d._pool = EnginePool(Path(self.tmp.name), [],
                             factory=lambda root: self.engine)
        return d

    def _ask(self, d, window=60.0):
        return d._op_diagnostics(1, "sess", {
            "path": str(self.file), "dedup_window_s": window})

    def test_second_ask_for_unchanged_content_is_replayed(self) -> None:
        d = self.daemon(items=[Diagnostic(uri="u", severity=1, line=1,
                                          character=0, message="boom")])
        first = self._ask(d)
        self.assertFalse(first["deduped"])
        second = self._ask(d)
        self.assertTrue(second["deduped"])
        # The engine was not asked a second time.
        self.assertEqual(self.engine.calls, 1)
        # And the payload is the same answer, not an empty one.
        self.assertEqual(second["diagnostics"], first["diagnostics"])

    def test_an_edit_defeats_the_replay(self) -> None:
        d = self.daemon()
        self._ask(d)
        self.file.write_text("x = 2\n", encoding="utf-8")
        import os
        st = self.file.stat()
        os.utime(self.file, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        self.assertFalse(self._ask(d)["deduped"])
        self.assertEqual(self.engine.calls, 2)

    def test_unsettled_results_are_never_replayed(self) -> None:
        # A cold server publishes nothing, then everything, for the same
        # content. Replaying the empty answer would make a timing
        # artefact permanent.
        d = self.daemon(settled=False)
        self.assertFalse(self._ask(d)["deduped"])
        self.assertFalse(self._ask(d)["deduped"])
        self.assertEqual(self.engine.calls, 2)

    def test_window_of_zero_disables_it(self) -> None:
        d = self.daemon()
        self._ask(d, window=0.0)
        self.assertFalse(self._ask(d, window=0.0)["deduped"])
        self.assertEqual(self.engine.calls, 2)

    def test_the_replay_expires(self) -> None:
        d = self.daemon()
        self._ask(d)
        # Age the record past the window.
        d._served[str(self.file)]["at"] -= 3600
        self.assertFalse(self._ask(d)["deduped"])

    def test_a_missing_file_does_not_dedup(self) -> None:
        d = self.daemon()
        self._ask(d)
        self.file.unlink()
        # No stamp means no basis for claiming the content is unchanged.
        self.assertFalse(self._ask(d)["deduped"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
