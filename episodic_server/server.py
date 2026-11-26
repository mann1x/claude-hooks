#!/usr/bin/env python3
"""
Tiny HTTP server that fronts episodic-memory with two endpoints:

    POST /ingest   — accept a transcript JSONL, save to archive, re-index
    POST /sync     — re-index (``?background=1``: start it and answer 202)
    GET  /search    — semantic search across all indexed conversations
    GET  /stats     — ``episodic-memory stats``
    GET  /health    — answers at once: the last probe search, searches in
                      flight, whether a sync is indexing, the last sync, the
                      index size (``?fresh=1`` waits for a new probe)

Every endpoint that shells out to the CLI answers 502 with the CLI's
stderr when it exits non-zero. It used to answer 200 with an empty
``stdout`` and drop stderr, and ``/health`` only checked that the archive
directory existed — so from 2026-09-14 to 09-26, with better-sqlite3
built for the wrong Node ABI and every CLI call throwing, the server
looked healthy while nothing was indexed.

The probe is a real vector search, not ``stats``: ``stats`` never loads
the embedding model, so from 2026-10-31 to 11-26, with the model cache
gone (it lived inside ``node_modules``, which deploy reinstalled, and the
sandboxed service cannot write there), ``/health`` said ``ok`` while every
search and every sync's indexing failed.

The server is threaded (``ThreadingHTTPServer``): it used to serve one
request at a time, so ``/health`` waited behind any search, and a search
took ~4 min (result formatting read a 6.4 GB transcript to count its
lines, fixed in ``episodic-memory/src/search.ts``). Searches share
``MAX_CONCURRENT_SEARCHES`` slots; the probe runs on its own thread.

Stdlib only. Designed to run as a systemd service or Docker container on
the host that has episodic-memory installed.

Usage:
    python3 server.py                       # default :11435
    python3 server.py --port 11435
    python3 server.py --host 0.0.0.0        # listen on all interfaces
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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

# episodic-memory's log dir (``getLogDir()``): the background sync's log and
# the sync lock that ``sync`` and ``index`` share.
LOG_DIR = Path(os.environ.get("EPISODIC_LOG_DIR", str(DEFAULT_ARCHIVE.parent / "logs")))
SYNC_LOG = LOG_DIR / "episodic-memory.log"
# ``getSyncLockPath()``: the holder's pid; the mutex is the ``.lock`` dir
# proper-lockfile keeps beside it, its mtime refreshed while held and
# stale after ``DEFAULT_STALE_MS`` (src/file-lock.ts).
SYNC_LOCK = LOG_DIR / "episodic-memory-sync.lock"
SYNC_LOCK_STALE_S = 600
# How much of the log tail is read for the last sync's summary.
SYNC_LOG_TAIL_BYTES = 256 * 1024

# episodic-memory binary.
EPISODIC_BIN = os.environ.get("EPISODIC_BIN", "episodic-memory")

# Searches run at most this many at once (~450 MB of Node each); the rest
# wait for a slot, up to SEARCH_TIMEOUT_S.
MAX_CONCURRENT_SEARCHES = max(1, int(os.environ.get("EPISODIC_MAX_SEARCHES", "2")))

# The probe searches (it loads the embedding model), so /health reuses a
# probe this young (?fresh=1 forces a new one).
HEALTH_PROBE_TTL_S = 300.0
# A vector search for this loads the model and reads the vector index —
# the path a dead model cache breaks while ``stats`` still answers.
PROBE_ARGS = ["search", "--vector", "--limit", "1", "episodic health probe"]
STATS_TIMEOUT_S = 60
# The first search in a process loads the embedding model: ~35 s on
# solidpc. 60 s left no margin.
SEARCH_TIMEOUT_S = 180
SYNC_TIMEOUT_S = 120
STDERR_TAIL_CHARS = 2000

_probe_cache: dict = {}
_probe_running = threading.Lock()

_search_slots = threading.BoundedSemaphore(MAX_CONCURRENT_SEARCHES)
_searches = {"running": 0, "waiting": 0}
_searches_lock = threading.Lock()


@contextlib.contextmanager
def search_slot(timeout: float):
    """Hold one of MAX_CONCURRENT_SEARCHES slots; counted for /health.
    Yields False when no slot came free within ``timeout``."""
    with _searches_lock:
        _searches["waiting"] += 1
    got = _search_slots.acquire(timeout=timeout)
    with _searches_lock:
        _searches["waiting"] -= 1
        if got:
            _searches["running"] += 1
    try:
        yield got
    finally:
        if got:
            with _searches_lock:
                _searches["running"] -= 1
            _search_slots.release()


def searches_state() -> dict:
    with _searches_lock:
        return {**_searches, "max": MAX_CONCURRENT_SEARCHES}


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def indexing_state(now: float) -> dict:
    """Is a sync (or ``index``) holding the shared lock? Read from the lock
    itself, so it sees syncs from any source (SessionEnd, /ingest, a shell)."""
    try:
        beat = (SYNC_LOCK.parent / (SYNC_LOCK.name + ".lock")).stat().st_mtime
    except OSError:
        return {"running": False}
    if now - beat > SYNC_LOCK_STALE_S:
        return {"running": False, "stale_lock": True}
    out: dict = {"running": True}
    try:
        out["pid"] = int(SYNC_LOCK.read_text().strip())
        out["since"] = _iso(SYNC_LOCK.stat().st_mtime)
    except (OSError, ValueError):
        pass
    return out


_STARTED = re.compile(r"^(\S+) \[info\] Starting background sync")
_COUNT = re.compile(r"^\s+(Copied|Skipped|Indexed|Summarized|Errors): (\d+)")


def last_sync(text: str | None = None) -> dict | None:
    """The last completed background sync in the sync log: when it started,
    its counts, and whether it skipped semantic indexing — which a sync
    reports as "Sync complete!" with "Indexed: 0" (it did, for 26 days)."""
    if text is None:
        try:
            with SYNC_LOG.open("rb") as f:
                f.seek(max(0, f.seek(0, os.SEEK_END) - SYNC_LOG_TAIL_BYTES))
                text = f.read().decode("utf-8", "replace")
        except OSError:
            return None
    lines = text.splitlines()
    done = max((i for i, ln in enumerate(lines) if "Sync complete!" in ln), default=None)
    if done is None:
        return None
    out: dict = {}
    for ln in reversed(lines[:done]):
        m = _STARTED.match(ln)
        if m:
            out["started_at"] = m.group(1)
            break
    for ln in lines[done + 1:]:
        if _STARTED.match(ln):
            break
        m = _COUNT.match(ln)
        if m:
            out[m.group(1).lower()] = int(m.group(2))
        if "Semantic indexing skipped" in ln:
            out["indexing_skipped"] = True
            out["error"] = ln.strip()
    return out


def index_exchanges() -> int | None:
    """Rows in the index (read-only; the vec0 tables are not needed)."""
    try:
        con = sqlite3.connect(f"file:{INDEX_DB}?mode=ro", uri=True, timeout=5)
        try:
            return con.execute("SELECT COUNT(*) FROM exchanges").fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error:
        return None

# claude-hooks config, read for the episodic block (the server runs from
# the repo; see episodic-server.service).
CONFIG_PATH = Path(
    os.environ.get(
        "CLAUDE_HOOKS_CONFIG",
        str(Path(__file__).resolve().parent.parent / "config" / "claude-hooks.json"),
    )
)


def compress_after_days() -> float:
    """``episodic.compress_after_days`` (default 7), else the env var."""
    try:
        ep = json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get("episodic") or {}
        return float(ep.get("compress_after_days", 7))
    except (OSError, ValueError, TypeError, AttributeError):
        try:
            return float(os.environ.get("EPISODIC_MEMORY_COMPRESS_AFTER_DAYS", "0"))
        except ValueError:
            return 0.0


def sync_env() -> dict:
    """Environment for a sync: sync compresses transcripts idle this long."""
    env = dict(os.environ)
    days = compress_after_days()
    if days > 0:
        env["EPISODIC_MEMORY_COMPRESS_AFTER_DAYS"] = f"{days:g}"
    else:
        env.pop("EPISODIC_MEMORY_COMPRESS_AFTER_DAYS", None)
    return env


def start_background_sync() -> None:
    """Start ``sync --background`` from this service, which may write the
    archive and the index. stderr stays on the service's (the journal):
    discarding it is how a failing sync went unseen."""
    subprocess.Popen(
        [EPISODIC_BIN, "sync", "--background"],
        stdout=subprocess.DEVNULL,
        env=sync_env(),
    )


def _run_cli(args: list[str], timeout: float, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [EPISODIC_BIN, *args],
        capture_output=True, text=True, timeout=timeout, env=env,
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
    """Run one vector search (``PROBE_ARGS``) and say whether it worked.
    Cached for HEALTH_PROBE_TTL_S."""
    now = time.time() if now is None else now
    cached = _probe_cache.get("result")
    if cached and not fresh and now - cached["checked_at"] < HEALTH_PROBE_TTL_S:
        return cached
    try:
        r = _run_cli(PROBE_ARGS, SEARCH_TIMEOUT_S)
        res = {"cli_ok": r.returncode == 0}
        if r.returncode != 0:
            res.update(cli_failure(r))
    except subprocess.TimeoutExpired:
        res = {"cli_ok": False,
               "error": f"probe search timed out after {SEARCH_TIMEOUT_S}s"}
    except OSError as e:
        res = {"cli_ok": False, "error": f"cannot run {EPISODIC_BIN}: {e}"}
    res["exchanges"] = index_exchanges()
    res["checked_at"] = now
    _probe_cache["result"] = res
    return res


def refresh_probe_in_background(now: float) -> bool:
    """Start a probe on its own thread when the cached one is missing or
    stale and none is running. True when one was started."""
    cached = _probe_cache.get("result")
    if cached and now - cached["checked_at"] < HEALTH_PROBE_TTL_S:
        return False
    if not _probe_running.acquire(blocking=False):
        return False

    def run():
        try:
            probe(fresh=True)
        finally:
            _probe_running.release()

    threading.Thread(target=run, name="episodic-probe", daemon=True).start()
    return True


def health(*, fresh: bool = False, now: float | None = None) -> tuple[int, dict]:
    """Answer at once from what the server knows; only ``fresh`` waits for a
    new probe (on the caller's own thread — the server is threaded)."""
    now = time.time() if now is None else now
    body = {
        "archive": str(DEFAULT_ARCHIVE),
        "archive_exists": DEFAULT_ARCHIVE.exists(),
        "index_db": str(INDEX_DB),
    }
    try:
        st = INDEX_DB.stat()
        # The index's age is the only sign of a sync that stopped running
        # while the CLI itself still works.
        body["index_age_hours"] = round((now - st.st_mtime) / 3600, 1)
        body["index_bytes"] = st.st_size
    except OSError:
        body["index_age_hours"] = None
    body["searches"] = searches_state()
    body["indexing"] = indexing_state(now)
    body["last_sync"] = last_sync()
    if fresh:
        body.update(probe(fresh=True, now=now))
    else:
        refresh_probe_in_background(now)
        cached = _probe_cache.get("result")
        body.update(cached if cached else {"cli_ok": None})
        body["probe_running"] = _probe_running.locked()
    body["activity"] = ("indexing" if body["indexing"].get("running")
                        else "searching" if body["searches"]["running"] else "idle")
    skipped = bool((body["last_sync"] or {}).get("indexing_skipped"))
    if body["cli_ok"] is None:
        body["status"] = "probing"
    elif body["cli_ok"] and body["archive_exists"] and not skipped:
        body["status"] = "ok"
    else:
        body["status"] = "degraded"
        if skipped and body["cli_ok"]:
            body["error"] = ("the last sync skipped semantic indexing: "
                             + body["last_sync"].get("error", ""))
    return (200 if body["status"] == "ok" else 503), body


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
            self._sync(parsed)
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
            with search_slot(SEARCH_TIMEOUT_S) as got:
                if not got:
                    self._json_response(503, {
                        "error": "no search slot came free",
                        "searches": searches_state()})
                    return
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
        # A re-pushed session supersedes a copy sync compressed earlier;
        # the plain file wins on read, so the .zst would only waste space.
        Path(f"{dest}.zst").unlink(missing_ok=True)
        size = len(body)

        start_background_sync()

        self._json_response(200, {
            "status": "ingested",
            "path": str(dest),
            "bytes": size,
            "project": safe_project,
            "session_id": session_id,
        })

    def _sync(self, parsed):
        """Re-index. ``?background=1`` starts the sync and answers 202 at
        once: that is how a SessionEnd hook asks for one, since the hook
        daemon's sandbox cannot write the archive, the index or the sync
        log itself."""
        background = (parse_qs(parsed.query).get("background") or ["0"])[0]
        if background not in ("0", ""):
            try:
                start_background_sync()
            except OSError as e:
                self._json_response(500, {"error": f"cannot run {EPISODIC_BIN}: {e}"})
                return
            self._json_response(202, {"status": "started"})
            return
        try:
            result = _run_cli(["sync"], SYNC_TIMEOUT_S, env=sync_env())
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


class EpisodicServer(ThreadingHTTPServer):
    """One thread per request, so /health never waits behind a search or a
    sync. Request threads are daemons: shutdown never waits on a search."""
    daemon_threads = True
    request_queue_size = 64


def main():
    ap = argparse.ArgumentParser(description="episodic-memory HTTP server")
    ap.add_argument("--host", default="0.0.0.0", help="bind address")
    ap.add_argument("--port", type=int, default=11435, help="listen port")
    args = ap.parse_args()

    server = EpisodicServer((args.host, args.port), EpisodicHandler)
    refresh_probe_in_background(time.time())
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
