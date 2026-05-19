"""Tests for ``ChatClient.chat_streamed`` — Ollama NDJSON streaming.

We mock ``urllib.request.urlopen`` to return a fake response that
iterates over a list of bytes lines. The streaming code parses each
line as JSON, accumulates content + tool_calls, and returns the
final assembled OpenAI-shape dict.

The full ``ChatClient.chat()`` retry policy is shared with
``chat_streamed`` so we only spot-check it (e.g. one retry success,
one fail-after-max) rather than re-doing the whole 15-attempt
budget matrix that lives in the existing ``test_chat_client*`` and
``test_get_advice_chat_client`` suites.
"""

from __future__ import annotations

import io
import json
import unittest
import urllib.error
from contextlib import contextmanager
from unittest.mock import patch

from claude_hooks.get_advice.chat_client import ChatClient


class _FakeResponse:
    """Mimic enough of urllib's response object: iterable over lines
    + a ``read()`` for the error path. Context-manager compatible."""

    def __init__(self, lines: list[bytes], *, status: int = 200):
        self._lines = list(lines)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(self._lines)

    def read(self):
        return b"".join(self._lines)


def _ndjson_line(**fields) -> bytes:
    """Build one NDJSON line with the given fields, terminated by \\n."""
    return (json.dumps(fields) + "\n").encode()


def _ok_stream(content_chunks: list[str], *,
               tool_calls: list[dict] = None,
               prompt_tokens: int = 10,
               completion_tokens: int = 5) -> list[bytes]:
    """Build a healthy Ollama NDJSON stream that ends with done=true.

    Each ``content_chunks`` entry becomes one in-stream record with
    a content delta. The trailing ``done=true`` record carries
    optional tool_calls + usage counts.
    """
    lines: list[bytes] = []
    for chunk in content_chunks:
        lines.append(_ndjson_line(
            model="stub", created_at="2026-01-01T00:00:00Z",
            message={"role": "assistant", "content": chunk},
            done=False,
        ))
    final_msg = {"role": "assistant", "content": ""}
    if tool_calls is not None:
        final_msg["tool_calls"] = tool_calls
    lines.append(_ndjson_line(
        model="stub", created_at="2026-01-01T00:00:00Z",
        message=final_msg,
        done=True,
        prompt_eval_count=prompt_tokens,
        eval_count=completion_tokens,
    ))
    return lines


@contextmanager
def _patched_urlopen(side_effects):
    """Patch urllib.request.urlopen with a list of responses or
    exceptions. Each call to urlopen pops the next item.

    Items can be ``_FakeResponse`` (returned), ``BaseException``
    (raised), or a callable (called with the request to produce
    one of the above dynamically).
    """
    state = {"i": 0}

    def fake_urlopen(req, timeout=None):
        idx = state["i"]
        state["i"] += 1
        if idx >= len(side_effects):
            raise AssertionError(
                f"unexpected extra urlopen call (#{idx + 1})")
        item = side_effects[idx]
        if callable(item):
            item = item(req)
        if isinstance(item, BaseException):
            raise item
        return item

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        yield state


def _make_client() -> ChatClient:
    # tiny retry budget so failure tests don't sit for 90s.
    return ChatClient(
        "http://stub", timeout_s=5.0,
        max_retries=2, retry_base_delay_s=0.0,
        retry_max_delay_s=0.0,
    )


class TestChatStreamedHappyPath(unittest.TestCase):

    def test_assembles_content_from_chunks(self):
        client = _make_client()
        stream = _ok_stream(["Hello", " ", "world"])
        with _patched_urlopen([_FakeResponse(stream)]):
            out = client.chat_streamed({
                "model": "stub",
                "messages": [{"role": "user", "content": "hi"}],
            })
        self.assertEqual(
            out["choices"][0]["message"]["content"], "Hello world",
        )
        self.assertEqual(out["choices"][0]["finish_reason"], "stop")
        self.assertEqual(out["usage"]["prompt_tokens"], 10)
        self.assertEqual(out["usage"]["completion_tokens"], 5)

    def test_emits_on_token_for_each_chunk(self):
        client = _make_client()
        stream = _ok_stream(["a", "b", "c"])
        tokens: list[str] = []
        with _patched_urlopen([_FakeResponse(stream)]):
            client.chat_streamed(
                {"model": "stub",
                 "messages": [{"role": "user", "content": "hi"}]},
                on_token=tokens.append,
            )
        self.assertEqual(tokens, ["a", "b", "c"])

    def test_tool_calls_pass_through(self):
        client = _make_client()
        tcs = [{
            "id": "tc1", "type": "function",
            "function": {"name": "search",
                          "arguments": {"q": "ollama"}},
        }]
        stream = _ok_stream([], tool_calls=tcs,
                            prompt_tokens=4, completion_tokens=0)
        with _patched_urlopen([_FakeResponse(stream)]):
            out = client.chat_streamed({"model": "stub", "messages": []})
        msg = out["choices"][0]["message"]
        self.assertIn("tool_calls", msg)
        self.assertEqual(len(msg["tool_calls"]), 1)
        # _from_ollama JSON-encodes dict-args back to strings.
        args = msg["tool_calls"][0]["function"]["arguments"]
        self.assertEqual(json.loads(args), {"q": "ollama"})
        self.assertEqual(out["choices"][0]["finish_reason"], "tool_calls")

    def test_malformed_line_is_skipped(self):
        client = _make_client()
        stream = [
            _ndjson_line(message={"role": "assistant", "content": "ok"},
                          done=False),
            b"not-json\n",
            _ndjson_line(message={"role": "assistant", "content": ""},
                          done=True,
                          prompt_eval_count=1, eval_count=1),
        ]
        with _patched_urlopen([_FakeResponse(stream)]):
            out = client.chat_streamed({"model": "stub", "messages": []})
        self.assertEqual(out["choices"][0]["message"]["content"], "ok")

    def test_blank_lines_ignored(self):
        client = _make_client()
        stream = [
            b"\n", b"   \n",
            _ndjson_line(message={"role": "assistant", "content": "x"},
                          done=False),
            b"\n",
            _ndjson_line(message={"role": "assistant", "content": ""},
                          done=True,
                          prompt_eval_count=1, eval_count=1),
        ]
        with _patched_urlopen([_FakeResponse(stream)]):
            out = client.chat_streamed({"model": "stub", "messages": []})
        self.assertEqual(out["choices"][0]["message"]["content"], "x")


class TestChatStreamedCancellation(unittest.TestCase):

    def test_cancel_check_raises_cancelled(self):
        from consultants.engine.stall import CancelledByOrchestrator
        client = _make_client()
        # Cancel after first content chunk arrives. ``cancel_check``
        # is polled before parsing each line.
        stream = _ok_stream(["a", "b", "c"])
        state = {"calls": 0}

        def cancel_after_one():
            state["calls"] += 1
            return state["calls"] > 1

        with _patched_urlopen([_FakeResponse(stream)]):
            with self.assertRaises(CancelledByOrchestrator):
                client.chat_streamed(
                    {"model": "stub", "messages": []},
                    cancel_check=cancel_after_one,
                )


class TestChatStreamedRetries(unittest.TestCase):

    def test_retries_on_503_then_succeeds(self):
        client = _make_client()
        err = urllib.error.HTTPError(
            "http://stub/api/chat", 503, "Service Unavailable",
            None, io.BytesIO(b"upstream down"),
        )
        good = _FakeResponse(_ok_stream(["ok"]))
        with _patched_urlopen([err, good]):
            out = client.chat_streamed({"model": "stub", "messages": []})
        self.assertEqual(out["choices"][0]["message"]["content"], "ok")

    def test_gives_up_after_max_retries(self):
        client = _make_client()
        # Always 502 - exhausts the 2-retry budget.
        def make_err(_req):
            return urllib.error.HTTPError(
                "http://stub/api/chat", 502, "Bad Gateway",
                None, io.BytesIO(b"upstream"),
            )
        with _patched_urlopen([make_err, make_err, make_err]):
            with self.assertRaises(RuntimeError) as cm:
                client.chat_streamed(
                    {"model": "stub", "messages": []})
        self.assertIn("HTTP 502", str(cm.exception))

    def test_4xx_non_retryable_fails_fast(self):
        client = _make_client()
        err = urllib.error.HTTPError(
            "http://stub/api/chat", 401, "Unauthorized",
            None, io.BytesIO(b"bad token"),
        )
        with _patched_urlopen([err]):
            with self.assertRaises(RuntimeError):
                client.chat_streamed(
                    {"model": "stub", "messages": []})


class TestChatStreamedReturnShapeMatchesChat(unittest.TestCase):
    """Verify chat_streamed and chat return shape-identical dicts
    for the same canonical Ollama response so the agent-loop
    runner can be agnostic to which path produced the dict."""

    def test_same_keys_as_non_streaming(self):
        client = _make_client()
        # Build a non-streaming response: one big object.
        non_stream = (json.dumps({
            "model": "stub",
            "message": {"role": "assistant", "content": "Hello world"},
            "done": True,
            "prompt_eval_count": 10,
            "eval_count": 5,
        }) + "\n").encode()

        # Stream version: 2 chunks + done=true.
        stream = _ok_stream(["Hello", " world"])

        with _patched_urlopen([_FakeResponse([non_stream])]):
            non_stream_out = client.chat({"model": "stub", "messages": []})
        with _patched_urlopen([_FakeResponse(stream)]):
            stream_out = client.chat_streamed(
                {"model": "stub", "messages": []})

        # Top-level keys identical.
        self.assertEqual(set(non_stream_out.keys()),
                         set(stream_out.keys()))
        # Content identical.
        self.assertEqual(
            non_stream_out["choices"][0]["message"]["content"],
            stream_out["choices"][0]["message"]["content"],
        )
        # Usage shape identical.
        self.assertEqual(
            set(non_stream_out["usage"].keys()),
            set(stream_out["usage"].keys()),
        )


if __name__ == "__main__":
    unittest.main()
