#!/usr/bin/env python3
"""Post-deploy verification. Run on every host after a deploy.

Until now "verify" meant opening a session and eyeballing the log, which
catches a broken hook but not a *silently* wrong configuration. Every
check here answers a question that has actually gone wrong on a real
host, and each one is written to fail loudly rather than degrade into a
plausible-looking pass.

The store check exists because of a specific trap. The consultants
``[store]`` block is **not** in ``config/claude-hooks.json`` — reading it
from there returns ``None`` on every host, healthy or not. It lives in:

    user-global   ~/.claude/consultants-config.toml
    per-project   <cwd>/.claude-hooks/consultants.toml

Note the asymmetry: the user-global file is ``consultants-config.toml``
while the project one is plain ``consultants.toml``, so looking for the
project name in the user-global location finds nothing on a perfectly
configured machine. Both mistakes were made on 2026-07-30 and produced a
false "pandorum's store backend is unset" report.

Checks resolve config through the real loader and then *use* the backend,
because *configured* and *working* are different claims and only the
second one matters after a deploy.

Usage:
    scripts/verify_deploy.py              # every check
    scripts/verify_deploy.py --store      # store checks only
    scripts/verify_deploy.py --quiet      # only failures

Exit code: 0 if every check passed, 1 otherwise.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"


class Results:
    def __init__(self, quiet: bool = False):
        self.rows: list[tuple[str, str, str]] = []
        self.quiet = quiet

    def add(self, status: str, name: str, detail: str = "") -> None:
        self.rows.append((status, name, detail))
        if self.quiet and status == PASS:
            return
        mark = {PASS: "ok", FAIL: "FAIL", WARN: "warn"}[status]
        print(f"  [{mark:>4}] {name}" + (f" — {detail}" if detail else ""))

    @property
    def failed(self) -> int:
        return sum(1 for s, _, _ in self.rows if s == FAIL)


# --------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------- #
def check_store(r: Results) -> None:
    print("consultants store")
    try:
        from consultants.config import (
            load_config,
            project_config_path,
            user_config_path,
        )
    except Exception as e:
        r.add(WARN, "store config", f"consultants package unavailable ({e})")
        return

    # Which file is actually in play. Reported explicitly because the
    # user-global / per-project filenames differ and guessing is how the
    # false alarm happened.
    try:
        user_p = user_config_path()
        proj_p = project_config_path(Path.cwd())
    except Exception as e:
        r.add(FAIL, "store config paths", str(e))
        return
    found = [str(p) for p in (user_p, proj_p) if p.exists()]
    if found:
        r.add(PASS, "store config file", " + ".join(found))
    else:
        r.add(WARN, "store config file",
              f"neither {user_p} nor {proj_p} exists — running on defaults")

    try:
        cfg = load_config()
    except Exception as e:
        r.add(FAIL, "store config load", f"{type(e).__name__}: {e}")
        return
    s = getattr(cfg, "store", None)
    if s is None:
        r.add(FAIL, "store block", "config has no [store] section")
        return

    if not getattr(s, "enabled", False):
        r.add(WARN, "store.enabled", "false — cross-session store is OFF")
        return
    backend = getattr(s, "backend", None)
    if not backend:
        r.add(FAIL, "store.backend", "enabled but no backend set")
        return
    r.add(PASS, "store.backend", str(backend))

    # Effort gate: a perfectly configured store still does nothing if the
    # host's default effort sits outside enable_at_efforts.
    efforts = tuple(getattr(s, "enable_at_efforts", ()) or ())
    effort = getattr(cfg, "effort", None)
    if efforts and effort is not None and effort not in efforts:
        r.add(WARN, "store effort gate",
              f"default effort {effort!r} not in {efforts} — store not wired")
    else:
        r.add(PASS, "store effort gate", f"effort={effort!r}")

    _check_store_backend(r, s, str(backend))


def _check_store_backend(r: Results, s, backend: str) -> None:
    """Actually reach the backend. Configured != working."""
    if backend == "memory":
        r.add(WARN, "store backend reachable",
              "in-memory backend — nothing persists across restarts")
        return

    if backend == "pgvector":
        dsn = getattr(s, "pgvector_dsn", None)
        table = getattr(s, "pgvector_table", None) or "consultants_store"
        if not dsn:
            r.add(FAIL, "store backend reachable", "pgvector_dsn not set")
            return
        try:
            import psycopg
        except ImportError:
            r.add(WARN, "store backend reachable", "psycopg not installed")
            return
        try:
            with psycopg.connect(dsn, connect_timeout=10) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT to_regclass(%s)", (table,))
                    if cur.fetchone()[0] is None:
                        r.add(FAIL, "store backend reachable",
                              f"table {table} missing")
                        return
                    cur.execute(f"SELECT count(*) FROM {table}")
                    n = cur.fetchone()[0]
            host = dsn.split("@")[-1]
            r.add(PASS, "store backend reachable",
                  f"pgvector {host} table={table} rows={n}")
        except Exception as e:
            r.add(FAIL, "store backend reachable",
                  f"{type(e).__name__}: {str(e)[:120]}")
        return

    if backend == "sqlite_vec":
        raw = getattr(s, "sqlite_vec_path", None)
        if not raw:
            r.add(FAIL, "store backend reachable", "sqlite_vec_path not set")
            return
        p = Path(os.path.expanduser(str(raw)))
        if not p.exists():
            r.add(WARN, "store backend reachable",
                  f"{p} does not exist yet (created on first write)")
            return
        try:
            import sqlite3
            # `with sqlite3.connect(...)` commits the transaction but does
            # NOT close the connection. Leaving it open holds a handle on
            # the live store database, and on Windows that locks the file
            # outright. Close it explicitly.
            #
            # as_uri() rather than f"file:{p}" so the read-only URI is
            # well-formed on Windows, where a bare path carries a drive
            # colon and backslashes.
            uri = p.resolve().as_uri() + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
            try:
                tables = [t[0] for t in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")]
            finally:
                conn.close()
            r.add(PASS, "store backend reachable",
                  f"sqlite_vec {p} ({len(tables)} tables)")
        except Exception as e:
            r.add(FAIL, "store backend reachable",
                  f"{type(e).__name__}: {str(e)[:120]}")
        return

    r.add(WARN, "store backend reachable", f"unknown backend {backend!r}")


# --------------------------------------------------------------------- #
# Memory providers
# --------------------------------------------------------------------- #
def check_providers(r: Results) -> None:
    print("memory providers")
    try:
        from claude_hooks.config import load_config as load_hooks_config
        from claude_hooks.dispatcher import build_providers
    except Exception as e:
        r.add(FAIL, "providers import", str(e))
        return
    try:
        cfg = load_hooks_config()
        providers = build_providers(cfg)
    except Exception as e:
        r.add(FAIL, "providers build", f"{type(e).__name__}: {e}")
        return
    if not providers:
        r.add(WARN, "providers enabled", "none enabled")
        return
    for p in providers:
        try:
            n = p.count()
        except Exception as e:
            r.add(FAIL, f"provider {p.name}", f"{type(e).__name__}: {e}")
            continue
        # A live provider reporting 0 is suspicious rather than fatal:
        # it is exactly what a dead connection used to look like.
        if n == 0:
            r.add(WARN, f"provider {p.name}",
                  "reports 0 memories — empty corpus or unreachable backend")
        else:
            r.add(PASS, f"provider {p.name}", f"{n} memories")


def check_version(r: Results) -> None:
    print("version")
    try:
        from claude_hooks import __version__
        r.add(PASS, "claude_hooks importable", f"v{__version__}")
    except Exception as e:
        r.add(FAIL, "claude_hooks importable", str(e))


# --------------------------------------------------------------------- #
# Skills
# --------------------------------------------------------------------- #
def check_skills(r: Results) -> None:
    """Compare the in-repo SKILL.md files with the installed ones.

    Added 2026-08-02 after ``~/.claude/skills/consultants/SKILL.md`` was
    found still at its **21 May** content — 791 lines against the repo's
    1591. Every session since had been loading half a skill: no wait
    patterns, no review loop, no ``accept`` / ``tool-ack`` verbs. It
    failed the way everything in this release failed, by looking fine.

    The cause is a deploy routine, not a bug. ``pip install -e .`` plus a
    service restart makes the *engine* current, and that is what "deploy"
    had come to mean. A skill is not loaded by the service — Claude Code
    reads it at session start — so it sat outside the definition and
    drifted for ten weeks unnoticed. Checking it here makes the deploy
    step mechanical instead of remembered.

    ``install.py`` is the thing that syncs them; this only reports.
    """
    print("skills")
    repo_skills = REPO / ".claude" / "skills"
    user_skills = Path(os.path.expanduser("~/.claude/skills"))
    if not repo_skills.is_dir():
        r.add(WARN, "skills", f"no in-repo skills dir at {repo_skills}")
        return
    if not user_skills.is_dir():
        r.add(WARN, "skills", f"nothing installed at {user_skills}")
        return

    stale: list[str] = []
    missing: list[str] = []
    ok = 0
    for src in sorted(repo_skills.glob("*/SKILL.md")):
        name = src.parent.name
        dst = user_skills / name / "SKILL.md"
        if not dst.is_file():
            # Not an error: skills are opt-in per host, and install.py
            # never auto-installs a new one.
            missing.append(name)
            continue
        try:
            same = (src.read_text(encoding="utf-8")
                    == dst.read_text(encoding="utf-8"))
        except OSError as e:
            r.add(FAIL, f"skill /{name}", f"unreadable: {e}")
            continue
        if same:
            ok += 1
        else:
            src_n = len(src.read_text(encoding="utf-8").splitlines())
            dst_n = len(dst.read_text(encoding="utf-8").splitlines())
            stale.append(f"{name} (installed {dst_n} lines, repo {src_n})")

    if ok:
        r.add(PASS, "skills in sync", f"{ok} up to date")
    if missing:
        r.add(WARN, "skills not installed",
              ", ".join(missing) + " — opt-in; `python3 install.py` offers them")
    if stale:
        # FAIL, not WARN: a stale skill is a session running instructions
        # that do not match the engine it is driving, and nothing in the
        # session surfaces the mismatch.
        r.add(FAIL, "skills STALE",
              "; ".join(stale) + " — run `python3 install.py` to sync")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--store", action="store_true", help="store checks only")
    ap.add_argument("--skills", action="store_true", help="skill checks only")
    ap.add_argument("--quiet", action="store_true", help="show only failures")
    a = ap.parse_args()

    r = Results(quiet=a.quiet)
    if a.store:
        check_store(r)
    elif a.skills:
        check_skills(r)
    else:
        check_version(r)
        check_providers(r)
        check_skills(r)
        check_store(r)

    failed = r.failed
    warned = sum(1 for s, _, _ in r.rows if s == WARN)
    print(f"\n{len(r.rows)} checks — {len(r.rows) - failed - warned} ok, "
          f"{warned} warn, {failed} fail")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
