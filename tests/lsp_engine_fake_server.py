"""Fake LSP server used by ``tests/test_lsp_engine_lsp.py``.

Speaks just enough of the LSP wire protocol for the round-trip tests:

- Replies to ``initialize`` with a minimal capabilities object.
- On every ``textDocument/didOpen`` / ``textDocument/didChange``,
  immediately publishes one synthetic diagnostic whose ``message``
  encodes the *length* of the document text, so tests can verify
  that the latest content actually reached the server (not stale
  ``didOpen`` content).
- Replies to ``shutdown`` and exits cleanly on ``exit``.

Run as a subprocess from the test:

    subprocess.Popen([sys.executable, "tests/lsp_engine_fake_server.py"], ...)

Stays stdlib-only so it can run anywhere the project's tests run.
"""

from __future__ import annotations

import json
import sys
from typing import Optional


def _read_frame(stream) -> Optional[dict]:
    headers: dict[str, str] = {}
    while True:
        line = stream.readline()
        if not line:
            return None
        line = line.decode("ascii", errors="replace").rstrip("\r\n")
        if line == "":
            break
        k, _, v = line.partition(":")
        headers[k.strip().lower()] = v.strip()
    length_str = headers.get("content-length")
    if not length_str:
        return None
    length = int(length_str)
    body = stream.read(length)
    if len(body) != length:
        return None
    return json.loads(body.decode("utf-8"))


def _write_frame(stream, msg: dict) -> None:
    body = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    stream.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii"))
    stream.write(body)
    stream.flush()


def _publish_for(stream, uri: str, content: str) -> None:
    """Emit a single synthetic diagnostic so tests can assert that
    ``content`` (the latest text the server saw) round-tripped.
    """
    _write_frame(
        stream,
        {
            "jsonrpc": "2.0",
            "method": "textDocument/publishDiagnostics",
            "params": {
                "uri": uri,
                "diagnostics": [
                    {
                        "range": {
                            "start": {"line": 0, "character": 0},
                            "end": {"line": 0, "character": 1},
                        },
                        "severity": 1,
                        "message": f"len={len(content)}",
                        "code": "fake",
                        "source": "fake-lsp",
                    }
                ],
            },
        },
    )


def main() -> int:
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    while True:
        msg = _read_frame(stdin)
        if msg is None:
            return 0
        method = msg.get("method")
        msg_id = msg.get("id")

        if method == "initialize":
            _write_frame(
                stdout,
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "capabilities": {
                            "textDocumentSync": 1,  # full
                        },
                        "serverInfo": {"name": "fake-lsp", "version": "0.0.0"},
                    },
                },
            )
        elif method == "initialized":
            pass
        elif method == "textDocument/didOpen":
            params = msg.get("params") or {}
            doc = params.get("textDocument") or {}
            _publish_for(stdout, doc.get("uri", ""), doc.get("text", ""))
        elif method == "textDocument/didChange":
            params = msg.get("params") or {}
            doc = params.get("textDocument") or {}
            changes = params.get("contentChanges") or []
            text = changes[-1].get("text", "") if changes else ""
            _publish_for(stdout, doc.get("uri", ""), text)
        elif method == "textDocument/didClose":
            pass
        elif method == "shutdown":
            _write_frame(stdout, {"jsonrpc": "2.0", "id": msg_id, "result": None})
        elif method == "exit":
            return 0
        elif msg_id is not None:
            # Unknown request — return method-not-found so the client
            # doesn't hang.
            _write_frame(
                stdout,
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32601, "message": f"unknown: {method}"},
                },
            )


if __name__ == "__main__":
    sys.exit(main())
