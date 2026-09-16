"""UTF-8 on the stdio transport.

The bug these cover was found on pandorum, where ``sys.stdin.encoding``
is ``cp1252``: a JSON-RPC line containing ``café — 你好`` round-tripped
to something else with no error raised anywhere.
"""
from __future__ import annotations

import io
import json
import sys
import unittest
from unittest import mock

from claude_hooks.mcp_stdio import force_utf8_stdio


class _FakeStream:
    """A stream that records what ``reconfigure`` was asked for."""

    def __init__(self, *, raises=None):
        self.calls: list[dict] = []
        self._raises = raises

    def reconfigure(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises is not None:
            raise self._raises


class ForceUtf8StdioTests(unittest.TestCase):
    def test_reconfigures_all_three_streams(self):
        streams = {name: _FakeStream() for name in
                   ("stdin", "stdout", "stderr")}
        with mock.patch.multiple(sys, **streams):
            force_utf8_stdio()
        for name, stream in streams.items():
            with self.subTest(stream=name):
                self.assertEqual(
                    stream.calls,
                    [{"encoding": "utf-8", "errors": "replace"}])

    def test_errors_replace_so_one_bad_byte_costs_one_line(self):
        # Not cosmetic: a UnicodeDecodeError raised by the `for raw in
        # sys.stdin` iterator is raised by the *iterator*, so the
        # per-line try/except inside the loop never sees it and the
        # server ends.
        stream = _FakeStream()
        with mock.patch.object(sys, "stdin", stream):
            force_utf8_stdio()
        self.assertEqual(stream.calls[0]["errors"], "replace")

    def test_stream_without_reconfigure_is_skipped(self):
        # A test's StringIO holds str, never bytes, so it needs no
        # decoding — and must not crash the server that calls this.
        with mock.patch.object(sys, "stdin", io.StringIO("{}")):
            force_utf8_stdio()  # must not raise

    def test_detached_stream_does_not_prevent_startup(self):
        stream = _FakeStream(raises=ValueError("underlying buffer detached"))
        with mock.patch.object(sys, "stdout", stream):
            force_utf8_stdio()  # must not raise

    def test_os_error_does_not_prevent_startup(self):
        stream = _FakeStream(raises=OSError("not seekable"))
        with mock.patch.object(sys, "stdout", stream):
            force_utf8_stdio()  # must not raise

    def test_idempotent(self):
        stream = _FakeStream()
        with mock.patch.object(sys, "stdin", stream):
            force_utf8_stdio()
            force_utf8_stdio()
        self.assertEqual(len(stream.calls), 2)
        self.assertEqual(stream.calls[0], stream.calls[1])


class Cp1252RegressionTests(unittest.TestCase):
    """The concrete corruption, reproduced without needing Windows."""

    PAYLOAD = {"body": "café — 你好"}

    def test_cp1252_decode_silently_corrupts_a_utf8_frame(self):
        raw = json.dumps(self.PAYLOAD).encode("utf-8")
        # ensure_ascii means the *response* path is safe; the request
        # path is not, because the client is not obliged to escape.
        wire = json.dumps(self.PAYLOAD, ensure_ascii=False).encode("utf-8")

        decoded = wire.decode("cp1252", errors="replace")
        self.assertNotEqual(json.loads(decoded)["body"],
                            self.PAYLOAD["body"],
                            "cp1252 should mangle this — if it round-trips, "
                            "the fixture is no longer exercising the bug")
        self.assertEqual(json.loads(wire.decode("utf-8"))["body"],
                         self.PAYLOAD["body"])
        # The all-ASCII form is what our responses look like, and it is
        # immune either way.
        self.assertEqual(json.loads(raw.decode("cp1252"))["body"],
                         self.PAYLOAD["body"])

    def test_stdin_wrapped_utf8_reads_the_frame_intact(self):
        wire = json.dumps(self.PAYLOAD, ensure_ascii=False).encode("utf-8")
        stream = io.TextIOWrapper(io.BytesIO(wire), encoding="cp1252")
        force = getattr(stream, "reconfigure")
        force(encoding="utf-8", errors="replace")
        self.assertEqual(json.loads(stream.readline())["body"],
                         self.PAYLOAD["body"])


class ServersCallItTests(unittest.TestCase):
    """Each stdio server must actually invoke the fix.

    Asserted per-server rather than once, because the reason all three
    needed it is that each grew its own copy of the read loop.
    """

    def test_pgvector_serve_stdio_forces_utf8(self):
        self._assert_forces("claude_hooks.pgvector_mcp.server")

    def test_sqlite_vec_serve_stdio_forces_utf8(self):
        self._assert_forces("claude_hooks.sqlite_vec_mcp.server")

    def test_lsp_mcp_serve_stdio_forces_utf8(self):
        self._assert_forces("claude_hooks.lsp_mcp.server")

    def _assert_forces(self, module_name: str):
        import importlib
        mod = importlib.import_module(module_name)
        self.assertTrue(
            hasattr(mod, "force_utf8_stdio"),
            f"{module_name} does not import force_utf8_stdio")
        called: list[bool] = []
        with mock.patch.object(mod, "force_utf8_stdio",
                               lambda: called.append(True)), \
                mock.patch.object(sys, "stdin", io.StringIO("")):
            try:
                mod.serve_stdio()
            except Exception:
                # Reaching a configuration failure is fine; the call we
                # care about happens before any of that.
                pass
        self.assertTrue(called, f"{module_name}.serve_stdio() did not call "
                                f"force_utf8_stdio()")


class HookStdinTests(unittest.TestCase):
    """The hook entry point reads UTF-8 too.

    Higher stakes than the MCP servers: this stdin carries the user's
    own prompt, so a cp1252 decode corrupts the recall query and
    whatever the Stop hook stores from that turn.
    """

    def test_read_event_from_stdin_forces_utf8(self):
        from claude_hooks import dispatcher
        called: list[bool] = []
        with mock.patch.object(dispatcher, "force_utf8_stdio",
                               lambda: called.append(True)), \
                mock.patch.object(sys, "stdin", io.StringIO('{"a": 1}')):
            self.assertEqual(dispatcher.read_event_from_stdin(), {"a": 1})
        self.assertTrue(called, "read_event_from_stdin() did not call "
                                "force_utf8_stdio()")

    def test_non_ascii_prompt_survives(self):
        from claude_hooks import dispatcher
        prompt = "fix the café — 你好 handler"
        wire = json.dumps({"prompt": prompt}, ensure_ascii=False)
        stream = io.TextIOWrapper(io.BytesIO(wire.encode("utf-8")),
                                  encoding="cp1252")
        with mock.patch.object(sys, "stdin", stream):
            event = dispatcher.read_event_from_stdin()
        self.assertEqual(event.get("prompt"), prompt)

    def test_hook_responses_are_ascii_so_stdout_cannot_fail(self):
        # The write side is safe only because nothing passes
        # ensure_ascii=False. Pin that, since the failure it would
        # cause on Windows is a crashed hook, not a garbled string.
        payload = {"hookSpecificOutput": {"additionalContext": "café 你好"}}
        rendered = json.dumps(payload)
        self.assertTrue(rendered.isascii())
        rendered.encode("cp1252")  # must not raise


if __name__ == "__main__":
    unittest.main()
