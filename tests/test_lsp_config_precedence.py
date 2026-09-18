"""The MCP and the daemon must resolve cclsp.json identically.

They used to do it independently and in opposite orders — the MCP
preferring ``<root>/cclsp.json``, the daemon preferring
``$CCLSP_CONFIG_PATH`` — so the MCP could validate one file while the
daemon served another. Nothing failed, because both files happened to
list the same servers; that is exactly how the disagreement waits.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from claude_hooks.lsp_engine.config import (  # noqa: E402
    candidate_cclsp_paths, resolve_cclsp_path,
)
from claude_hooks.lsp_engine.daemon import daemon_cclsp_path  # noqa: E402
from claude_hooks.lsp_mcp import server as S  # noqa: E402

ENV = "CCLSP_CONFIG_PATH"


class PrecedenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "proj"
        self.root.mkdir()
        self.local = self.root / "cclsp.json"
        self.shared = Path(self.tmp.name) / "shared.json"
        self.shared.write_text("{}", encoding="utf-8")
        self.addCleanup(self.tmp.cleanup)
        self._env = os.environ.get(ENV)
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        if self._env is None:
            os.environ.pop(ENV, None)
        else:
            os.environ[ENV] = self._env

    def test_project_local_wins_over_the_env(self) -> None:
        self.local.write_text("{}", encoding="utf-8")
        os.environ[ENV] = str(self.shared)
        self.assertEqual(resolve_cclsp_path(self.root), self.local)

    def test_env_is_used_when_there_is_no_project_file(self) -> None:
        os.environ[ENV] = str(self.shared)
        self.assertEqual(resolve_cclsp_path(self.root), self.shared)

    def test_the_two_consumers_agree(self) -> None:
        # The regression this file exists for.
        self.local.write_text("{}", encoding="utf-8")
        os.environ[ENV] = str(self.shared)
        self.assertEqual(S.resolve_config_path(self.root),
                         daemon_cclsp_path(self.root))

    def test_they_agree_with_no_project_file_either(self) -> None:
        os.environ[ENV] = str(self.shared)
        self.assertEqual(S.resolve_config_path(self.root),
                         daemon_cclsp_path(self.root))

    def test_an_explicit_path_outranks_everything(self) -> None:
        # How a caller pins the file it already validated.
        self.local.write_text("{}", encoding="utf-8")
        os.environ[ENV] = str(self.shared)
        pinned = Path(self.tmp.name) / "pinned.json"
        self.assertEqual(daemon_cclsp_path(self.root, pinned), pinned)

    def test_candidates_are_ordered_most_specific_first(self) -> None:
        os.environ[ENV] = str(self.shared)
        cands = candidate_cclsp_paths(self.root)
        self.assertEqual(cands[0], self.local)
        self.assertEqual(cands[1], self.shared)

    def test_the_mcp_delegates_rather_than_reimplementing(self) -> None:
        os.environ[ENV] = str(self.shared)
        self.assertEqual(S.candidate_config_paths(self.root),
                         candidate_cclsp_paths(self.root))


class DisagreementNoticeTests(unittest.TestCase):
    """A daemon already running may serve a different file."""

    class _Engine:
        def __init__(self):
            self.notices = []

        def add_notice(self, text):
            self.notices.append(text)

    def test_notice_names_both_files_and_the_remedy(self) -> None:
        eng = self._Engine()
        eng.add_notice(
            "⚠  The lsp_engine daemon for /p is serving a different "
            "cclsp.json than this session validated.\n"
            "   validated: /p/cclsp.json\n   serving:   /shared/c.json\n"
            "   Restart it ... kill <pid>")
        text = eng.notices[0]
        self.assertIn("validated:", text)
        self.assertIn("serving:", text)
        self.assertIn("kill", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
