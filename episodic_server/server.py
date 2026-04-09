#!/usr/bin/env python3
"""
Tiny HTTP server that fronts episodic-memory with two endpoints:

    POST /ingest   — accept a transcript JSONL, save to archive, re-index
    GET  /search    — semantic search across all indexed conversations
    GET  /health    — liveness check

Stdlib only. Designed to run as a systemd service or Docker container on
the host that has episodic-memory installed.

Usage:
    python3 server.py                       # default :11435
    python3 server.py --port 11435
    python3 server.py --host 0.0.0.0        # listen on all interfaces
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse


# Where episodic-memory stores its archive.
DEFAULT_ARCHIVE = Path(
    os.environ.get(
        "EPISODIC_ARCHIVE",
        os.path.expanduser("~/.config/superpowers/conversation-archive"),
    )
)

# episodic-memory binary.
EPISODIC_BIN = os.environ.get("EPISODIC_BIN", "episodic-memory")


class EpisodicHandler(BaseHTTPRequestHandler):
    """Handle ingest, search, and health requests."""

    # Suppress default logging per request — we log ourselves.
    def log_message(self, fmt, *args):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        sys.stderr.write(f"{ts} {fmt % args}\n")

    # ------------------------------------------------------------------ #
    # Routing
    # ------------------------------------------------------------------ #
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._health()
        elif parsed.path == "/search":
            self._search(parsed)
        elif parsed.path == "/stats":
            self._stats()
        else:
            self._json_response(404, {"error": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/ingest":
            self._ingest()
        elif parsed.path == "/sync":
            self._sync()
        else:
            self._json_response(404, {"error": "not found"})

    # ------------------------------------------------------------------ #
    # Endpoints
    # ------------------------------------------------------------------ #
    def _health(self):
        self._json_response(200, {
            "status": "ok",
            "archive": str(DEFAULT_ARCHIVE),
            "archive_exists": DEFAULT_ARCHIVE.exists(),
        })

    def _stats(self):
        """Run episodic-memory stats and return the output."""
        result = subprocess.run(
            [EPISODIC_BIN, "stats"],
            capture_output=True, text=True, timeout=30,
        )
        self._json_response(200, {
            "stdout": result.stdout.strip(),
            "returncode": result.returncode,
        })

    def _search(self, parsed):
        """Search indexed conversations. Query param: ?q=...&limit=N"""
        params = parse_qs(parsed.query)
        query = (params.get("q") or params.get("query") or [""])[0]
        if not query:
            self._json_response(400, {"error": "missing ?q= parameter"})
            return
        limit = int((params.get("limit") or ["10"])[0])

        try:
            result = subprocess.run(
                [EPISODIC_BIN, "search", query],
                capture_output=True, text=True, timeout=60,
            )
            # Parse the output into structured results.
            results = _parse_search_output(result.stdout, limit)
            self._json_response(200, {
                "query": query,
                "count": len(results),
                "results": results,
            })
        except subprocess.TimeoutExpired:
            self._json_response(504, {"error": "search timed out"})
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    def _ingest(self):
        """Accept a transcript JSONL and save to the archive."""
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            self._json_response(400, {"error": "empty body"})
            return
        body = self.rfile.read(content_length)

        # Metadata from headers.
        project = self.headers.get("X-Project", "remote")
        session_id = self.headers.get("X-Session-Id", f"remote-{int(time.time())}")
        source_host = self.headers.get("X-Source-Host", "unknown")

        # Sanitize project name for filesystem.
        safe_project = "".join(
            c if c.isalnum() or c in "-_." else "-"
            for c in project
        ).strip("-")
        if not safe_project:
            safe_project = "remote"

        # Prefix with source host to avoid collisions.
        archive_dir = DEFAULT_ARCHIVE / f"{source_host}-{safe_project}"
        archive_dir.mkdir(parents=True, exist_ok=True)

        # Write the transcript.
        dest = archive_dir / f"{session_id}.jsonl"
        dest.write_bytes(body)
        size = len(body)

        # Trigger re-index in background.
        subprocess.Popen(
            [EPISODIC_BIN, "sync", "--background"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        self._json_response(200, {
            "status": "ingested",
            "path": str(dest),
            "bytes": size,
            "project": safe_project,
            "session_id": session_id,
        })

    def _sync(self):
        """Trigger a manual sync/re-index."""
        result = subprocess.run(
            [EPISODIC_BIN, "sync"],
            capture_output=True, text=True, timeout=120,
        )
        self._json_response(200, {
            "status": "synced",
            "stdout": result.stdout.strip(),
            "returncode": result.returncode,
        })

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _json_response(self, code: int, data: dict):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)


def _parse_search_output(stdout: str, limit: int) -> list[dict]:
    """Parse episodic-memory search text output into structured results."""
    results: list[dict] = []
    lines = stdout.strip().splitlines()
    i = 0
    while i < len(lines) and len(results) < limit:
        line = lines[i].strip()
        # Pattern: "N. [project, date] - X% match"
        if line and line[0].isdigit() and "." in line.split()[0]:
            entry: dict = {"raw": line}
            # Extract match percentage if present.
            if "% match" in line:
                try:
                    pct_str = line.split("% match")[0].rsplit(" ", 1)[-1]
                    entry["match_pct"] = int(pct_str)
                except (ValueError, IndexError):
                    pass
            # Next line is usually the quote.
            if i + 1 < len(lines):
                quote = lines[i + 1].strip().strip('"')
                entry["quote"] = quote
            # Line after that may have file info.
            if i + 2 < len(lines) and "Lines" in lines[i + 2]:
                entry["location"] = lines[i + 2].strip()
            results.append(entry)
            i += 3
        else:
            i += 1
    return results


def main():
    ap = argparse.ArgumentParser(description="episodic-memory HTTP server")
    ap.add_argument("--host", default="0.0.0.0", help="bind address")
    ap.add_argument("--port", type=int, default=11435, help="listen port")
    args = ap.parse_args()

    server = HTTPServer((args.host, args.port), EpisodicHandler)
    print(f"episodic-server listening on {args.host}:{args.port}")
    print(f"  archive: {DEFAULT_ARCHIVE}")
    print(f"  binary:  {EPISODIC_BIN}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    main()
