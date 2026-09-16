"""An edit the engine never saw must not produce a confident answer.

Only edits routed through ``did_change`` reach a language server. In auto
mode Claude edits with ``sed`` and shell heredocs, so nothing fires — and
a second session, a git checkout or another editor are the same shape.
The server then answers from the content it first read.

That failure does not look like a failure. A peer session measured it on
2026-09-16: after editing through Bash, ``find_references`` returned
pre-edit positions (6/175/188/201 where disk held 7/201/214/227) and
missed three call sites that had just been added. Shifted positions and
absent results are a wrong answer wearing the shape of a right one, which
is worse than the empty results the rest of this engine works to avoid.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from claude_hooks.lsp_engine.config import LspServerSpec  # noqa: E402
from claude_hooks.lsp_engine.engine import Engine  # noqa: E402


class _Client:
    """Records what the engine actually sent."""

    def __init__(self) -> None:
        self.opened: list[tuple[str, str]] = []
        self.changed: list[tuple[str, str]] = []
        self.alive = True

    @property
    def is_desynced(self) -> bool:
        return False

    @property
    def is_alive(self) -> bool:
        return self.alive

    def did_open(self, path, content) -> None:
        self.opened.append((str(path), content))

    def did_change(self, path, content) -> None:
        self.changed.append((str(path), content))

    def did_close(self, path) -> None:
        pass

    def document_symbols(self, path, **kw):
        return []

    def references(self, path, line, ch, **kw):
        return []

    def progress_snapshot(self):
        return None

    @property
    def last_content(self) -> str:
        """What this server would be answering from."""
        sends = self.opened + self.changed
        return sends[-1][1] if sends else ""


class DiskResyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.file = self.root / "x.py"
        self.file.write_text("original\n", encoding="utf-8")
        self.addCleanup(self.tmp.cleanup)

        self.spec = LspServerSpec(command=("fake-ls",), extensions=("py",))
        self.client = _Client()
        self.eng = Engine(self.root, [self.spec])
        self.eng._client_for = lambda spec: self.client  # type: ignore
        self.eng._clients = {self.spec: self.client}

    def _edit_on_disk(self, text: str) -> None:
        """Change the file the way sed or a heredoc would."""
        self.file.write_text(text, encoding="utf-8")
        # Guarantee the stamp moves even on a coarse clock.
        st = self.file.stat()
        os.utime(self.file, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))

    def test_edit_behind_the_engine_is_picked_up(self) -> None:
        self.eng._ensure_open(self.file)
        self.assertEqual(self.client.last_content, "original\n")

        self._edit_on_disk("edited\n")
        # No did_change: this is the sed / heredoc case.
        self.eng._ensure_open(self.file)

        self.assertEqual(self.client.last_content, "edited\n",
                         "server is still answering from pre-edit content")

    def test_unchanged_file_is_not_resent(self) -> None:
        self.eng._ensure_open(self.file)
        sends = len(self.client.opened) + len(self.client.changed)
        for _ in range(5):
            self.eng._ensure_open(self.file)
        self.assertEqual(len(self.client.opened) + len(self.client.changed),
                         sends, "re-sent an unchanged file")

    def test_did_change_updates_the_stamp(self) -> None:
        # An edit that DID route through the hook must not then be
        # re-read and re-sent by the next request.
        self.eng._ensure_open(self.file)
        self.file.write_text("via hook\n", encoding="utf-8")
        self.eng.did_change(self.file, "via hook\n")
        before = len(self.client.opened) + len(self.client.changed)
        self.eng._ensure_open(self.file)
        self.assertEqual(len(self.client.opened) + len(self.client.changed),
                         before)

    def test_a_truncating_edit_is_caught(self) -> None:
        # Same mtime resolution, different size: the stamp carries both
        # because an edit can land inside one clock tick.
        self.eng._ensure_open(self.file)
        self.file.write_text("x\n", encoding="utf-8")
        self.eng._ensure_open(self.file)
        self.assertEqual(self.client.last_content, "x\n")

    def test_navigation_resyncs_before_answering(self) -> None:
        # The path that actually matters: a reference query after an
        # unseen edit must not be answered from the stale copy.
        self.eng._ensure_open(self.file)
        self._edit_on_disk("brand new\n")
        self.eng.references(self.file, 0, 0)
        self.assertEqual(self.client.last_content, "brand new\n")

    def test_missing_file_does_not_raise(self) -> None:
        self.eng._ensure_open(self.file)
        self.file.unlink()
        # Reporting honestly is the request's job; resync must not be
        # the thing that explodes.
        self.eng._ensure_open(self.file)

    def test_external_writer_is_the_same_case(self) -> None:
        # A git checkout or a second session is indistinguishable from
        # a sed edit as far as the engine is concerned.
        self.eng._ensure_open(self.file)
        time.sleep(0.01)
        self._edit_on_disk("from another branch\n")
        self.eng._ensure_open(self.file)
        self.assertEqual(self.client.last_content, "from another branch\n")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
