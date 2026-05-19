#!/usr/bin/env python3
"""Lint a completed consult session's synthesized answer.

Fetches the final answer for ``<sid>`` from the consultants HTTP
service's ``/v1/consult/<sid>`` endpoint, runs the
:mod:`consultants.engine.citation_linter` against it with the
session's cwd as the lone allowed_root, and prints:

* per-issue summary (one line per fabrication caught)
* the annotated answer (markdown, with ``[unverified — …]`` and
  ``[in X, not Y]`` markers inserted)

Exit codes:

* ``0`` — no issues caught (clean answer)
* ``1`` — issues caught (linter annotated the answer)
* ``2`` — usage / fetch error

Useful for post-mortem analysis of pre-linter sessions
(:file:`benchmarks/consultants/results/2026-05-18/m14-first-real-ask/rerun-report.md`
documents the M14 first-real-ask retroactive lint).

Usage:

    scripts/lint_consult_answer.py csl-2026-05-18-1031-9e3b

    scripts/lint_consult_answer.py csl-2026-05-18-1031-9e3b \\
        --root /srv/dev-disk-by-label-opt/dev/claude-hooks \\
        --extra-root /shared/dev/laserRMT

    # Save annotated output to a file for the run record:
    scripts/lint_consult_answer.py csl-... > annotated.md
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional


DEFAULT_ENDPOINT = "http://127.0.0.1:38095"


def _fetch_session(
    endpoint: str, sid: str, *, on_disk_cwd: Optional[str] = None,
) -> dict:
    """Resolve a session by SID, trying HTTP first and on-disk as
    fallback.

    Strategy:
    1. Try ``GET <endpoint>/v1/consult/<sid>`` (warm session, in
       memory or auto-rehydrated from the per-project sessions
       index).
    2. On HTTP 404 or connection error, fall back to reading
       ``<on_disk_cwd>/.claude-hooks/consultants/<sid>/metadata.json``
       — the on-disk artifact written when the consult completed.
       ``on_disk_cwd`` defaults to the current working directory
       (CWD where the script runs); pass ``--cwd`` to override
       for a session whose cwd was elsewhere.
    """
    url = f"{endpoint.rstrip('/')}/v1/consult/{sid}"
    http_error: Optional[str] = None
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            http_error = f"HTTP 404 (session not in memory)"
        else:
            print(
                f"error: HTTP {e.code} fetching {url}: {e.reason}",
                file=sys.stderr,
            )
            sys.exit(2)
    except urllib.error.URLError as e:
        http_error = f"service unreachable ({e.reason})"

    # On-disk fallback. The metadata.json shape is similar enough to
    # the HTTP response for the linter's needs (final_answer + cwd +
    # extra_roots).
    cwd_base = on_disk_cwd or os.getcwd()
    candidate = (
        Path(cwd_base) / ".claude-hooks" / "consultants" / sid /
        "metadata.json"
    )
    if not candidate.is_file():
        print(
            f"error: HTTP failed ({http_error}) AND on-disk "
            f"fallback not found at {candidate}",
            file=sys.stderr,
        )
        sys.exit(2)
    try:
        with open(candidate, "r", encoding="utf-8") as fh:
            on_disk = json.load(fh)
    except (OSError, ValueError) as e:
        print(
            f"error: cannot read {candidate}: {e}",
            file=sys.stderr,
        )
        sys.exit(2)
    sys.stderr.write(
        f"using on-disk metadata: {candidate} "
        f"(HTTP fallback: {http_error})\n"
    )
    return on_disk


def _resolve_roots(
    session: dict,
    cli_root: Optional[str],
    cli_extras: list[str],
) -> list[str]:
    """Pick the allowed_roots to lint against.

    Precedence:
    1. ``--root`` + ``--extra-root`` from the CLI (operator override)
    2. ``cwd`` + ``extra_roots`` from the session metadata (default)

    Caller may pass an empty roots list — the linter then flags
    every cite as ``file not found`` (useful when you only care
    about identifying cites for review, not verifying them).
    """
    if cli_root:
        roots = [cli_root, *cli_extras]
    else:
        cwd = session.get("cwd") or ""
        extras = session.get("extra_roots") or []
        roots = [cwd] if cwd else []
        if isinstance(extras, list):
            roots.extend(str(r) for r in extras if r)
    return [r for r in roots if r]


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Lint a completed consult's synthesized answer.",
    )
    p.add_argument("sid", help="Session ID, e.g. csl-2026-05-18-1031-9e3b")
    p.add_argument(
        "--endpoint", default=DEFAULT_ENDPOINT,
        help=f"consultants HTTP service (default {DEFAULT_ENDPOINT})",
    )
    p.add_argument(
        "--root",
        help=(
            "Override session cwd as the primary allowed_root. "
            "Useful when running against a session whose cwd is "
            "stale or unmounted."
        ),
    )
    p.add_argument(
        "--extra-root", action="append", default=[],
        help=(
            "Additional allowed_root; repeatable. Appended after "
            "--root (or the session cwd)."
        ),
    )
    p.add_argument(
        "--show-original", action="store_true",
        help=(
            "Print the original (un-linted) answer first, then the "
            "annotated one. Default: print only the annotated answer."
        ),
    )
    p.add_argument(
        "--cwd",
        help=(
            "When the HTTP service does not have the session in "
            "memory, fall back to "
            "<cwd>/.claude-hooks/consultants/<sid>/metadata.json. "
            "Defaults to the current working directory."
        ),
    )
    args = p.parse_args(argv)

    # Defer the heavy import until after argparse so --help is fast.
    from consultants.engine.citation_linter import lint_answer

    session = _fetch_session(
        args.endpoint, args.sid, on_disk_cwd=args.cwd,
    )
    if not session.get("ok", True):
        print(f"error: session fetch ok=False: {session}", file=sys.stderr)
        return 2

    answer = session.get("final_answer") or ""
    if not answer:
        # Some routes wrap the answer under "metadata" — try that.
        meta = session.get("metadata") or {}
        answer = meta.get("final_answer") or ""
    if not answer:
        print(
            f"error: session {args.sid} has no final_answer "
            f"(status={session.get('status')!r})",
            file=sys.stderr,
        )
        return 2

    roots = _resolve_roots(session, args.root, args.extra_root)
    if not roots:
        print(
            "warning: no allowed_roots resolvable; linter will flag "
            "every cite as file-not-found",
            file=sys.stderr,
        )

    annotated, issues = lint_answer(answer, allowed_roots=roots)

    sys.stderr.write(f"sid: {args.sid}\n")
    sys.stderr.write(f"allowed_roots: {roots}\n")
    sys.stderr.write(f"issues_caught: {len(issues)}\n")
    for i in issues:
        sys.stderr.write(f"  - [{i.reason}] {i.original_match}\n")
        sys.stderr.write(f"      -> {i.replacement}\n")

    if args.show_original:
        print("===== ORIGINAL ANSWER =====")
        print(answer)
        print()
        print("===== ANNOTATED ANSWER =====")
    print(annotated)
    return 1 if issues else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
