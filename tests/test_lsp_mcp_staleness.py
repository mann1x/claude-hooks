"""The stale-process notice.

What is being pinned here is that the warning fires on the case that
actually happened. ``f3c4bd3`` was a twelve-file fix that did not touch
``pyproject.toml``, so a version-only check would have stayed silent
through the exact incident that motivated this module — the source-mtime
signal is not a nicety, it is the one that would have caught it.

The rest is restraint: once per session (a banner on every ``get_hover``
is noise, and noise is how a real warning gets filtered out), and never
an automatic re-exec.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from claude_hooks.lsp_mcp import server as S  # noqa: E402
from claude_hooks.lsp_mcp.staleness import (  # noqa: E402
    ENV_DISABLE,
    StalenessDetector,
)


def _tree(root: Path, mtime: float) -> None:
    """A miniature package whose files all predate the import."""
    (root / "sub").mkdir(parents=True, exist_ok=True)
    for rel in ("__init__.py", "server.py", "sub/engine.py"):
        p = root / rel
        p.write_text("# x\n", encoding="utf-8")
        import os
        os.utime(p, (mtime, mtime))


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "claude_hooks"
        self.import_time = 1_000_000.0
        _tree(self.root, self.import_time - 100)
        self.addCleanup(self.tmp.cleanup)

    def detector(self, installed: str = "1.16.0", **kw) -> StalenessDetector:
        return StalenessDetector(
            pkg_root=self.root,
            imported_version=kw.pop("imported", "1.16.0"),
            import_time=self.import_time,
            installed_version_fn=lambda: installed,
            min_recheck_seconds=kw.pop("min_recheck", 30.0),
        )


class CleanProcessTests(_Fixture):
    def test_matching_version_and_untouched_source_is_silent(self) -> None:
        d = self.detector()
        self.assertIsNone(d.check(now=self.import_time + 1))
        self.assertIsNone(d.banner(now=self.import_time + 1))
        self.assertFalse(d.announced)


class VersionSignalTests(_Fixture):
    def test_differing_version_is_reported(self) -> None:
        d = self.detector(installed="1.17.0", imported="1.16.0")
        rep = d.check(now=self.import_time + 1)
        self.assertIsNotNone(rep)
        assert rep is not None
        self.assertEqual(rep.reason, "version")
        self.assertEqual(rep.imported_version, "1.16.0")
        self.assertEqual(rep.installed_version, "1.17.0")

    def test_banner_names_both_versions_and_the_remedy(self) -> None:
        d = self.detector(installed="1.17.0", imported="1.16.0")
        text = d.banner(now=self.import_time + 1)
        assert text is not None
        self.assertIn("1.16.0", text)
        self.assertIn("1.17.0", text)
        self.assertIn("Restart the MCP client", text)
        # The remedy has to say what will NOT work, because reaching for
        # restart_server is the natural first move and it does nothing.
        self.assertIn("restart_server", text)
        self.assertIn("docs/lsp-engine.md", text)


class SourceSignalTests(_Fixture):
    """The f3c4bd3 case: same version, newer code."""

    def _touch(self, rel: str, when: float) -> None:
        import os
        p = self.root / rel
        os.utime(p, (when, when))

    def test_source_newer_than_import_is_reported(self) -> None:
        self._touch("sub/engine.py", self.import_time + 500)
        d = self.detector(installed="1.16.0", imported="1.16.0")
        rep = d.check(now=self.import_time + 600)
        self.assertIsNotNone(rep)
        assert rep is not None
        self.assertEqual(rep.reason, "source")
        # Same version on both sides — this is precisely the blind spot
        # a version-only check has.
        self.assertEqual(rep.imported_version, rep.installed_version)
        assert rep.changed_path is not None
        self.assertEqual(rep.changed_path.name, "engine.py")

    def test_banner_names_the_changed_file(self) -> None:
        self._touch("sub/engine.py", self.import_time + 500)
        d = self.detector()
        text = d.banner(now=self.import_time + 600)
        assert text is not None
        self.assertIn("engine.py", text)
        self.assertIn("same version", text)
        self.assertIn("Restart the MCP client", text)

    def test_pycache_is_ignored(self) -> None:
        import os
        cache = self.root / "__pycache__"
        cache.mkdir()
        stale = cache / "server.cpython-311.pyc"
        stale.write_text("x", encoding="utf-8")
        os.utime(stale, (self.import_time + 900, self.import_time + 900))
        # A recompile is not a source change.
        d = self.detector()
        self.assertIsNone(d.check(now=self.import_time + 1000))

    def test_version_wins_over_source_when_both_differ(self) -> None:
        self._touch("sub/engine.py", self.import_time + 500)
        d = self.detector(installed="1.17.0", imported="1.16.0")
        rep = d.check(now=self.import_time + 600)
        assert rep is not None
        self.assertEqual(rep.reason, "version")


class OncePerSessionTests(_Fixture):
    def test_banner_is_emitted_exactly_once(self) -> None:
        d = self.detector(installed="1.17.0", imported="1.16.0")
        first = d.banner(now=self.import_time + 1)
        self.assertIsNotNone(first)
        self.assertTrue(d.announced)
        for i in range(5):
            self.assertIsNone(d.banner(now=self.import_time + 100 + i))

    def test_check_goes_inert_after_announcing(self) -> None:
        d = self.detector(installed="1.17.0", imported="1.16.0")
        d.banner(now=self.import_time + 1)
        self.assertIsNone(d.check(now=self.import_time + 10_000))


class ThrottleTests(_Fixture):
    def test_clean_tree_is_not_rewalked_on_every_call(self) -> None:
        calls = []

        def counting() -> str:
            calls.append(1)
            return "1.16.0"

        d = StalenessDetector(
            pkg_root=self.root,
            imported_version="1.16.0",
            import_time=self.import_time,
            installed_version_fn=counting,
            min_recheck_seconds=30.0,
        )
        d.check(now=self.import_time + 1)
        d.check(now=self.import_time + 2)
        d.check(now=self.import_time + 3)
        self.assertEqual(len(calls), 1)
        # Past the window it checks again.
        d.check(now=self.import_time + 40)
        self.assertEqual(len(calls), 2)


class DisableTests(_Fixture):
    def test_env_flag_silences_it(self) -> None:
        import os
        d = self.detector(installed="1.17.0", imported="1.16.0")
        os.environ[ENV_DISABLE] = "0"
        self.addCleanup(os.environ.pop, ENV_DISABLE, None)
        self.assertIsNone(d.check(now=self.import_time + 1))
        self.assertIsNone(d.banner(now=self.import_time + 1))


class DispatchIntegrationTests(unittest.TestCase):
    """The notice must reach the tool output, not only the log."""

    class _FakeDetector:
        def __init__(self, text):
            self.text = text
            self.calls = 0

        def banner(self):
            self.calls += 1
            out, self.text = self.text, None
            return out

    def _patch(self, text):
        fake = self._FakeDetector(text)
        original = S.DETECTOR
        S.DETECTOR = fake
        self.addCleanup(lambda: setattr(S, "DETECTOR", original))
        return fake

    def test_banner_is_prefixed_to_tool_output(self) -> None:
        self._patch("STALE-NOTICE")
        out = S.LspMcpServer._announce("24 diagnostics")
        self.assertTrue(out.startswith("STALE-NOTICE"))
        # The real payload survives intact underneath it.
        self.assertIn("24 diagnostics", out)

    def test_clean_process_adds_nothing(self) -> None:
        self._patch(None)
        self.assertEqual(S.LspMcpServer._announce("24 diagnostics"),
                         "24 diagnostics")

    def test_second_call_is_unprefixed(self) -> None:
        self._patch("STALE-NOTICE")
        S.LspMcpServer._announce("first")
        self.assertEqual(S.LspMcpServer._announce("second"), "second")

    def test_errors_carry_the_notice_too(self) -> None:
        # A stale process can produce the error as easily as the wrong
        # answer, so the error path must not swallow the explanation.
        self._patch("STALE-NOTICE")
        out = S.LspMcpServer._announce("ToolError: no such symbol")
        self.assertIn("STALE-NOTICE", out)
        self.assertIn("no such symbol", out)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
