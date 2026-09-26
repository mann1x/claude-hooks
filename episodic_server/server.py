#!/usr/bin/env python3
"""
Tiny HTTP server that fronts episodic-memory with two endpoints:

    POST /ingest   — accept a transcript JSONL, save to archive, re-index
    GET  /search    — semantic search across all indexed conversations
    GET  /stats     — ``episodic-memory stats``
    GET  /health    — does the CLI actually run? (cached probe)

Every endpoint that shells out to the CLI answers 502 with the CLI's
stderr when it exits non-zero. It used to answer 200 with an empty
``stdout`` and drop stderr, and ``/health`` only checked that the archive
directory existed — so from 2026-09-14 to 09-26, with better-sqlite3
built for the wrong Node ABI and every CLI call throwing, the server
looked healthy while nothing was indexed.

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
from urllib.parse import parse_qs, urlparse


# Where episodic-memory stores its archive.
DEFAULT_ARCHIVE = Path(
    os.environ.get(
        "EPISODIC_ARCHIVE",
        os.path.expanduser("~/.config/superpowers/conversation-archive"),
    )
)

# episodic-memory's index, a sibling of the archive.
INDEX_DB = Path(
    os.environ.get(
        "EPISODIC_INDEX_DB",
        str(DEFAULT_ARCHIVE.parent / "conversation-index" / "db.sqlite"),
    )
)

# episodic-memory binary.
EPISODIC_BIN = os.environ.get("EPISODIC_BIN", "episodic-memory")

# ``stats`` takes ~4 s, so /health reuses a probe this young (?fresh=1
# forces a new one).
HEALTH_PROBE_TTL_S = 300.0
STATS_TIMEOUT_S = 60
# The first search in a process loads the embedding model: ~35 s on
# solidpc. 60 s left no margin.
SEARCH_TIMEOUT_S = 180
SYNC_TIMEOUT_S = 120
STDERR_TAIL_CHARS = 2000

_probe_cache: dict = {}


def _run_cli(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        [EPISODIC_BIN, *args],
        capture_output=True, text=True, timeout=timeout,
    )


def cli_failure(result: subprocess.CompletedProcess) -> dict:
    """What a non-zero CLI exit is reported as: the tail of stderr, and a
    pointer to the fix when it is the known native-module mismatch."""
    stderr = (result.stderr or "").strip()
    out = {
        "error": "episodic-memory exited non-zero",
        "returncode": result.returncode,
        "stderr": stderr[-STDERR_TAIL_CHARS:],
    }
    if "NODE_MODULE_VERSION" in stderr or "ERR_DLOPEN_FAILED" in stderr:
        out["hint"] = ("a native module was built for another Node version; "
                       "run scripts/episodic_doctor.py --rebuild")
    return out


def probe(*, fresh: bool = False, now: float | None = None) -> dict:
    """Run ``episodic-memory stats`` and say whether it worked. Cached for
    HEALTH_PROBE_TTL_S."""
    now = time.time() if now is None else now
    cached = _probe_cache.get("result")
    if cached and not fresh and now - cached["checked_at"] < HEALTH_PROBE_TTL_S:
        return cached
    try:
        r = _run_cli(["stats"], STATS_TIMEOUT_S)
        res = {"cli_ok": r.returncode == 0}
        if r.returncode != 0:
            res.update(cli_failure(r))
    except subprocess.TimeoutExpired:
        res = {"cli_ok": False, "error": f"stats timed out after {STATS_TIMEOUT_S}s"}
    except OSError as e:
        res = {"cli_ok": False, "error": f"cannot run {EPISODIC_BIN}: {e}"}
    res["checked_at"] = now
    _probe_cache["result"] = res
    return res


def health(*, fresh: bool = False, now: float | None = None) -> tuple[int, dict]:
    now = time.time() if now is None else now
    body = {
        "archive": str(DEFAULT_ARCHIVE),
        "archive_exists": DEFAULT_ARCHIVE.exists(),
        "index_db": str(INDEX_DB),
    }
    try:
        # The index's age is the only sign of a sync that stopped running
        # while the CLI itself still works.
        body["index_age_hours"] = round((now - INDEX_DB.stat().st_mtime) / 3600, 1)
    except OSError:
        body["index_age_hours"] = None
    body.update(probe(fresh=fresh, now=now))
    ok = body["cli_ok"] and body["archive_exists"]
    body["status"] = "ok" if ok else "degraded"
    return (200 if ok else 503), body


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
            self._health(parsed)
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
    def _health(self, parsed):
        fresh = (parse_qs(parsed.query).get("fresh") or ["0"])[0] not in ("0", "")
        code, body = health(fresh=fresh)
        self._json_response(code, body)

    def _stats(self):
        """Run episodic-memory stats and return the output."""
        try:
            result = _run_cli(["stats"], STATS_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            self._json_response(504, {"error": "stats timed out"})
            return
        if result.returncode != 0:
            self._json_response(502, cli_failure(result))
            return
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
            result = _run_cli(["search", query], SEARCH_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            self._json_response(504, {"error": "search timed out"})
            return
        except Exception as e:
            self._json_response(500, {"error": str(e)})
            return
        if result.returncode != 0:
            # Not "0 results": a CLI that cannot open its database prints
            # nothing, which parses to an empty list.
            self._json_response(502, cli_failure(result))
            return
        results = _parse_search_output(result.stdout, limit)
        self._json_response(200, {
            "query": query,
            "count": len(results),
            "results": results,
        })

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

        # Trigger re-index in background. stderr stays on the service's
        # (the journal): discarding it is how a failing sync went unseen.
        subprocess.Popen(
            [EPISODIC_BIN, "sync", "--background"],
            stdout=subprocess.DEVNULL,
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
        try:
            result = _run_cli(["sync"], SYNC_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            self._json_response(504, {"error": "sync timed out"})
            return
        if result.returncode != 0:
            self._json_response(502, cli_failure(result))
            return
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
