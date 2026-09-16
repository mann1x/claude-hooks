"""Transport behaviour for the navigation surface.

These drive :class:`LspClient` without a real language server: the
child process is stubbed and frames are fed to ``_dispatch`` directly.
That covers the parts a live smoke test cannot reach on demand — a
server that never answers, a server that asks *us* a question, and a
server reporting indexing progress.
"""
from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from claude_hooks.lsp_engine.lsp import LspClient, LspError  # noqa: E402
from claude_hooks.lsp_engine.protocol import (  # noqa: E402
    CallHierarchyItem, Position, Range,
)


class _FakeStdin:
    def __init__(self):
        self.closed = False

    def write(self, _b):
        return None

    def flush(self):
        return None


class _FakeProc:
    def __init__(self):
        self.stdin = _FakeStdin()
        self.stdout = None
        self.stderr = None

    def poll(self):
        return None


def make_client(tmp: Path, *, request_timeout: float = 0.05) -> LspClient:
    c = LspClient(["true"], tmp, request_timeout=request_timeout)
    c._proc = _FakeProc()
    c._sent: list[dict] = []
    c._write_frame = lambda msg: c._sent.append(msg)   # type: ignore[assignment]
    return c


class CancelOnTimeoutTests(unittest.TestCase):
    """A request we stopped waiting for must be cancelled.

    Without it a slow `find_references` keeps a single-threaded server
    busy after we gave up, so the *next* request queues behind work
    nobody wants and times out too. One slow call becomes a hung server.
    """

    def setUp(self):
        self.client = make_client(Path(__file__).parent)

    def test_timeout_sends_cancel_request(self):
        with self.assertRaises(LspError):
            self.client._send_request("textDocument/references", {})

        cancels = [m for m in self.client._sent
                   if m.get("method") == "$/cancelRequest"]
        self.assertEqual(len(cancels), 1)
        sent_id = self.client._sent[0]["id"]
        self.assertEqual(cancels[0]["params"]["id"], sent_id,
                         "must cancel the id we gave up on")

    def test_timeout_drops_the_pending_entry(self):
        """A late answer must not be delivered to a caller that is gone,
        and must not leak the queue either."""
        with self.assertRaises(LspError):
            self.client._send_request("textDocument/hover", {})
        self.assertEqual(self.client._pending, {})

    def test_late_response_after_cancel_is_discarded_quietly(self):
        with self.assertRaises(LspError):
            self.client._send_request("textDocument/hover", {})
        rid = self.client._sent[0]["id"]
        # The server answers anyway — allowed, and must not raise.
        self.client._dispatch({"jsonrpc": "2.0", "id": rid, "result": None})

    def test_successful_request_sends_no_cancel(self):
        def answer():
            self.client._dispatch(
                {"jsonrpc": "2.0", "id": 1, "result": {"contents": "ok"}})

        timer = threading.Timer(0.01, answer)
        timer.start()
        try:
            res = self.client._send_request("textDocument/hover", {}, timeout=2.0)
        finally:
            timer.cancel()
        self.assertEqual(res["contents"], "ok")
        self.assertFalse([m for m in self.client._sent
                          if m.get("method") == "$/cancelRequest"])


class ServerRequestTests(unittest.TestCase):
    """Server-to-client requests that must NOT get method-not-found.

    Each of these changes server behaviour when refused, so answering
    with an error is not a neutral "we don't do that".
    """

    def setUp(self):
        self.client = make_client(Path(__file__).parent)

    def _reply_to(self, method, params=None):
        self.client._dispatch({"jsonrpc": "2.0", "id": 42, "method": method,
                               "params": params or {}})
        self.assertTrue(self.client._sent, f"no reply to {method}")
        return self.client._sent[-1]

    def test_work_done_progress_create_is_acknowledged(self):
        """We declared window.workDoneProgress; refusing the token we
        asked for makes a server stop reporting progress."""
        reply = self._reply_to("window/workDoneProgress/create",
                               {"token": "t1"})
        self.assertNotIn("error", reply)
        self.assertIsNone(reply["result"])

    def test_workspace_configuration_returns_one_null_per_item(self):
        """gopls and pyright ask on startup; an error is a failed
        initialisation step, not "no settings"."""
        reply = self._reply_to("workspace/configuration",
                               {"items": [{"section": "gopls"},
                                          {"section": "other"}]})
        self.assertEqual(reply["result"], [None, None])

    def test_workspace_configuration_without_items(self):
        reply = self._reply_to("workspace/configuration", {})
        self.assertEqual(reply["result"], [None])

    def test_register_capability_is_acknowledged(self):
        for method in ("client/registerCapability", "client/unregisterCapability"):
            with self.subTest(method=method):
                reply = self._reply_to(method, {"registrations": []})
                self.assertNotIn("error", reply)

    def test_genuinely_unknown_request_still_errors(self):
        """Answering everything with success would be its own lie."""
        reply = self._reply_to("window/showMessageRequest", {})
        self.assertEqual(reply["error"]["code"], -32601)

    def test_notifications_never_get_a_reply(self):
        """A reply to a notification is a protocol violation, and an id
        of None would wedge a strict server."""
        self.client._dispatch({"jsonrpc": "2.0", "method": "telemetry/event",
                               "params": {}})
        self.assertEqual(self.client._sent, [])


class ProgressTests(unittest.TestCase):

    def setUp(self):
        self.client = make_client(Path(__file__).parent)

    def progress(self, token, value):
        self.client._dispatch({"jsonrpc": "2.0", "method": "$/progress",
                               "params": {"token": token, "value": value}})

    def test_idle_server_reports_nothing(self):
        self.assertIsNone(self.client.progress_snapshot())

    def test_begin_then_report_tracks_the_latest(self):
        self.progress("t", {"kind": "begin", "title": "Indexing",
                            "percentage": 0})
        self.progress("t", {"kind": "report", "message": "12000 files",
                            "percentage": 40})
        snap = self.client.progress_snapshot()
        self.assertEqual(snap["title"], "Indexing")
        self.assertEqual(snap["message"], "12000 files")
        self.assertEqual(snap["percentage"], 40)

    def test_report_without_title_keeps_the_one_from_begin(self):
        """`begin` carries the title and `report` usually does not; a
        snapshot that forgot it would say only "40%" of nothing."""
        self.progress("t", {"kind": "begin", "title": "Building preamble"})
        self.progress("t", {"kind": "report", "percentage": 10})
        self.assertEqual(self.client.progress_snapshot()["title"],
                         "Building preamble")

    def test_end_clears_it(self):
        self.progress("t", {"kind": "begin", "title": "Indexing"})
        self.progress("t", {"kind": "end"})
        self.assertIsNone(self.client.progress_snapshot())

    def test_eta_is_derived_from_percentage(self):
        self.progress("t", {"kind": "begin", "title": "Indexing",
                            "percentage": 50})
        snap = self.client.progress_snapshot()
        self.assertIsNotNone(snap["eta_seconds"])
        self.assertGreaterEqual(snap["eta_seconds"], 1)

    def test_no_eta_without_a_percentage(self):
        """Inventing one would be worse than admitting we cannot say."""
        self.progress("t", {"kind": "begin", "title": "Loading"})
        self.assertIsNone(self.client.progress_snapshot()["eta_seconds"])

    def test_no_eta_at_the_boundaries(self):
        for pct in (0, 100):
            with self.subTest(pct=pct):
                c = make_client(Path(__file__).parent)
                c._dispatch({"jsonrpc": "2.0", "method": "$/progress",
                             "params": {"token": "t", "value": {
                                 "kind": "begin", "title": "x",
                                 "percentage": pct}}})
                self.assertIsNone(c.progress_snapshot()["eta_seconds"])

    def test_concurrent_tokens_report_the_most_advanced(self):
        self.progress("a", {"kind": "begin", "title": "A", "percentage": 10})
        self.progress("b", {"kind": "begin", "title": "B", "percentage": 80})
        self.assertEqual(self.client.progress_snapshot()["title"], "B")

    def test_malformed_progress_is_ignored(self):
        for value in (None, "nope", {"kind": "unknown"}, {}):
            with self.subTest(value=value):
                self.progress("t", value)
        self.assertIsNone(self.client.progress_snapshot())


class PrepareRenameTests(unittest.TestCase):
    """An unimplemented precondition must not read as a refusal."""

    def setUp(self):
        self.client = make_client(Path(__file__).parent)

    def test_no_rename_provider_at_all_does_not_block(self):
        self.client._server_capabilities = {}
        self.assertTrue(self.client.prepare_rename("f.py", 0, 0))

    def test_rename_provider_without_prepare_support_does_not_block(self):
        """Most servers support rename but not prepareRename. Treating
        the missing check as "no" would disable rename everywhere."""
        self.client._server_capabilities = {"renameProvider": True}
        self.assertTrue(self.client.prepare_rename("f.py", 0, 0))

    def test_explicit_null_is_a_refusal(self):
        self.client._server_capabilities = {
            "renameProvider": {"prepareProvider": True}}
        self.client._send_request = lambda *a, **k: None   # type: ignore
        self.assertFalse(self.client.prepare_rename("f.py", 0, 0))

    def test_default_behavior_false_is_a_refusal(self):
        self.client._server_capabilities = {
            "renameProvider": {"prepareProvider": True}}
        self.client._send_request = lambda *a, **k: {"defaultBehavior": False}
        self.assertFalse(self.client.prepare_rename("f.py", 0, 0))

    def test_a_range_answer_is_permission(self):
        self.client._server_capabilities = {
            "renameProvider": {"prepareProvider": True}}
        self.client._send_request = lambda *a, **k: {
            "start": {"line": 1, "character": 4},
            "end": {"line": 1, "character": 9}}
        self.assertTrue(self.client.prepare_rename("f.py", 1, 4))

    def test_error_from_the_check_does_not_block_the_rename(self):
        self.client._server_capabilities = {
            "renameProvider": {"prepareProvider": True}}

        def boom(*a, **k):
            raise LspError("unsupported")
        self.client._send_request = boom   # type: ignore
        self.assertTrue(self.client.prepare_rename("f.py", 0, 0))


class CallHierarchyWireTests(unittest.TestCase):
    """The item must round-trip exactly; rust-analyzer and jdtls key
    their lookup on the range they sent."""

    def test_item_round_trips_with_both_ranges(self):
        from claude_hooks.lsp_engine.lsp import _call_item_wire
        item = CallHierarchyItem(
            name="run", kind=12, uri="file:///r.py",
            range=Range(Position(5, 0), Position(9, 0)),
            selection=Range(Position(5, 4), Position(5, 7)),
            detail="fn run()",
        )
        wire = _call_item_wire(item)
        self.assertEqual(wire["name"], "run")
        self.assertEqual(wire["range"]["start"]["line"], 5)
        self.assertEqual(wire["selectionRange"]["start"]["character"], 4)
        self.assertEqual(wire["detail"], "fn run()")

    def test_empty_detail_is_omitted_not_sent_blank(self):
        from claude_hooks.lsp_engine.lsp import _call_item_wire
        item = CallHierarchyItem(
            name="x", kind=12, uri="file:///r.py",
            range=Range(Position(0, 0), Position(0, 1)),
            selection=Range(Position(0, 0), Position(0, 1)),
        )
        self.assertNotIn("detail", _call_item_wire(item))


if __name__ == "__main__":
    unittest.main()
