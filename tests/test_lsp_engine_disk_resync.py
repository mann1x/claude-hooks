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
        self.closed: list[str] = []
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
        self.closed.append(str(path))

    def document_symbols(self, path, **kw):
        return []

    def references(self, path, line, ch, **kw):
        return []

    def hover(self, path, line, ch, **kw):
        return ""

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


class OtherOpenFilesTests(unittest.TestCase):
    """A sweep reads across files, so every open one has to be current.

    ``_ensure_open`` only refreshes the path in the request. The server
    also holds result files it opened itself while answering an earlier
    sweep, and those stay frozen until something names them — so a call
    site added to an unnamed file is missed, and one removed from it is
    still reported. Reported 2026-09-16 with a phantom reference at line
    348 of a 345-line file after a git checkout.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.named = self.root / "named.py"
        self.other = self.root / "other.py"
        self.named.write_text("named v1\n", encoding="utf-8")
        self.other.write_text("other v1\n", encoding="utf-8")
        self.addCleanup(self.tmp.cleanup)

        self.spec = LspServerSpec(command=("fake-ls",), extensions=("py",))
        self.client = _Client()
        self.eng = Engine(self.root, [self.spec])
        self.eng._client_for = lambda spec: self.client  # type: ignore
        self.eng._clients = {self.spec: self.client}
        # Both files open, as they would be after one sweep.
        self.eng._ensure_open(self.named)
        self.eng._ensure_open(self.other)

    def _sent_for(self, path: Path) -> list[str]:
        return [c for p, c in self.client.opened + self.client.changed
                if p == str(path)]

    def _edit(self, path: Path, text: str) -> None:
        path.write_text(text, encoding="utf-8")
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))

    def test_unnamed_open_file_is_refreshed_by_a_sweep(self) -> None:
        self._edit(self.other, "other v2\n")
        # The request names `named`, but reads across files.
        self.eng.references(self.named, 0, 0)
        self.assertEqual(self._sent_for(self.other)[-1], "other v2\n",
                         "the unnamed open file was left stale")

    def test_a_per_file_request_does_not_sweep(self) -> None:
        # hover is about one position; paying for every open file on
        # every hover would be the wrong trade.
        self._edit(self.other, "other v2\n")
        self.eng.hover(self.named, 0, 0)
        self.assertEqual(self._sent_for(self.other)[-1], "other v1\n")

    def test_reverted_file_stops_reporting_the_old_content(self) -> None:
        # The git-checkout case, in both directions.
        self._edit(self.other, "other v2 with an extra line\n")
        self.eng.references(self.named, 0, 0)
        self._edit(self.other, "other v1\n")
        self.eng.references(self.named, 0, 0)
        self.assertEqual(self._sent_for(self.other)[-1], "other v1\n")

    def test_deleted_file_is_closed_not_left_open(self) -> None:
        # Left open, its stale copy keeps producing references to code
        # that no longer exists.
        uri = [u for u in self.eng._uri_routing
               if u.endswith("other.py")][0]
        self.other.unlink()
        self.eng.references(self.named, 0, 0)
        self.assertNotIn(uri, self.eng._uri_routing)
        self.assertIn(str(self.other), [str(p) for p in self.client.closed])

    def test_unchanged_files_are_not_resent(self) -> None:
        before = len(self.client.opened) + len(self.client.changed)
        for _ in range(3):
            self.eng.references(self.named, 0, 0)
        self.assertEqual(len(self.client.opened) + len(self.client.changed),
                         before, "re-sent files whose stamp had not moved")
