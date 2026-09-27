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
from typing import Optional

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


def check_lsp_daemons(r: Results) -> None:
    """No lsp_engine daemon may be older than the code on disk.

    Deploy stops them so they respawn on the new code, and this is the
    check that the claim is worth anything. The failure it guards
    against is silent by construction: a stale daemon answers every
    request, just from the code it imported at spawn — which is how a
    session restarted after two deploys still drew the staleness banner
    on 2026-09-17.

    A daemon that came up *after* the newest source file is fine, and
    the common case here is none running at all, since they are
    spawn-on-demand.
    """
    print("lsp engine")
    try:
        from claude_hooks.lsp_engine_manager import LspEngineManager
    except Exception as e:
        r.add(WARN, "lsp daemons", f"not importable ({type(e).__name__})")
        return

    try:
        listing = LspEngineManager().list()
    except Exception as e:
        r.add(WARN, "lsp daemons", f"could not be listed: {e}")
        return
    if not listing.get("available"):
        r.add(PASS, "lsp daemons", "supervision disabled")
        return

    live = [d for d in listing.get("daemons", []) if d.get("running")]
    newest = _newest_source_mtime()
    stale = []
    for d in live:
        started = _process_start(d.get("pid"))
        if started is not None and started < newest:
            stale.append(f"{d.get('project')} (pid {d.get('pid')})")
    if stale:
        r.add(FAIL, "lsp daemons",
              f"{len(stale)} serving code older than the tree: "
              + ", ".join(stale[:3])
              + " — run `claude-hooks-daemon-ctl lsp stop`")
    else:
        r.add(PASS, "lsp daemons",
              f"{len(live)} running, none older than the tree")

    wedged = [d for d in listing.get("daemons", []) if d.get("wedged")]
    if wedged:
        # Process up, socket down, lock still held — so nothing can
        # spawn a replacement and the whole repository is out. It is not
        # covered by the staleness loop above, which only looks at
        # daemons that are *running*, and a wedged one by definition is
        # not answering. This is the state that survived a deploy on
        # 2026-09-17 and left a session on stale code for half a day.
        r.add(FAIL, "lsp daemons",
              f"{len(wedged)} wedged (up, not serving, holding the lock): "
              + ", ".join(f"{d.get('project')} (pid {d.get('pid')})"
                          for d in wedged[:3])
              + " — run `claude-hooks-daemon-ctl lsp stop`")

    for d in listing.get("stateless", []):
        # Unreachable over IPC and invisible to every disk lookup, so
        # deploy cannot stop it. Say so rather than let a clean report
        # imply it is not there.
        r.add(WARN, "lsp daemon (stateless)",
              f"pid {d.get('pid')} {d.get('project')} — {d.get('reason')}")


def _process_start(pid) -> Optional[float]:
    """Process start time, or None where it cannot be read."""
    if not pid:
        return None
    try:
        from claude_hooks.lsp_engine.daemon import process_start_time
    except Exception:
        return None
    return process_start_time(pid)


def _newest_source_mtime() -> float:
    """The newest ``.py`` in the installed package.

    The same signal the staleness detector uses, and for the same
    reason: a fix can ship without touching ``pyproject.toml``, so a
    version comparison alone would stay silent through exactly the
    twelve-file change that motivated all of this.
    """
    import claude_hooks
    root = Path(claude_hooks.__file__).resolve().parent
    newest = 0.0
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue
    return newest


def check_embedder(r: Results) -> None:
    """Can this host actually turn text into a vector?

    ``check_providers`` counts rows, which proves the database is
    reachable and proves nothing about recall: every recall embeds the
    query first, and an embedder that cannot answer degrades recall to
    ``0 hits`` — the same output as an empty corpus, with no error. So
    this probes the embedder each provider really uses, rather than the
    one the local daemon happens to supervise. The distinction matters
    on a host that consumes another host's embedder over the LAN
    (``daemon_ensure=false``): nothing is managed here, and the thing
    that can break is somewhere else entirely.
    """
    print("embedder")
    try:
        from claude_hooks.config import load_config as load_hooks_config
        from claude_hooks.dispatcher import build_providers
        from claude_hooks.embedders import NullEmbedder
    except Exception as e:
        r.add(WARN, "embedder", f"import failed: {type(e).__name__}: {e}")
        return
    try:
        providers = build_providers(load_hooks_config())
    except Exception as e:
        r.add(WARN, "embedder", f"providers unavailable: {type(e).__name__}")
        return

    probed = False
    for p in providers:
        # Probe first: providers build their embedder lazily inside
        # ``_ensure_ready``, so inspecting the attribute beforehand
        # reports "no embedder" for every provider that has one.
        try:
            vec = p.embed_for_store("verify_deploy embedder probe")
        except Exception as e:
            probed = True
            r.add(FAIL, f"embedder {p.name}", f"{type(e).__name__}: {e}")
            continue
        emb = getattr(p, "_embedder", None)
        if emb is None or isinstance(emb, NullEmbedder):
            # Qdrant / Memory KG embed server-side — nothing local to
            # break, and nothing this probe can say about them.
            continue
        probed = True
        target = getattr(emb, "url", None) or type(emb).__name__
        if vec:
            r.add(PASS, f"embedder {p.name}", f"{len(vec)}-dim via {target}")
        else:
            # embed_for_store soft-fails to None so a store never dies
            # on it. Here that silence is the whole finding.
            r.add(FAIL, f"embedder {p.name}",
                  f"cannot embed via {target} — recall returns 0 hits, "
                  f"which is indistinguishable from an empty corpus")
    if not probed:
        r.add(PASS, "embedder", "no client-side embedder on this host")


def check_episodic(r: Results) -> None:
    """Does episodic-memory actually run?

    The server's ``/health`` used to report ``ok`` whenever the archive
    directory existed. On solidpc better-sqlite3 was built for Node 22,
    Node went to 26, and every CLI call threw for 12 days behind that
    ``ok``. ``/health?fresh=1`` now runs the CLI, and this reads it. On a
    server host a dead CLI fails the deploy; on a client it only warns,
    because the fix is on another machine.
    """
    import json
    import urllib.error
    import urllib.request

    print("episodic")
    try:
        from claude_hooks.config import load_config as load_hooks_config
        ep = load_hooks_config().get("episodic") or {}
    except Exception as e:
        r.add(WARN, "episodic", f"config unavailable: {type(e).__name__}")
        return
    mode = ep.get("mode") or "off"
    if mode == "server":
        host = ep.get("server_host") or "127.0.0.1"
        if host in ("0.0.0.0", "::"):
            host = "127.0.0.1"
        url = f"http://{host}:{int(ep.get('server_port') or 11435)}"
    elif mode == "client" and ep.get("server_url"):
        url = ep["server_url"].rstrip("/")
    else:
        r.add(PASS, "episodic", f"mode {mode!r}: nothing to check")
        return
    bad = FAIL if mode == "server" else WARN
    if mode == "server":
        # The CLI must be the vendored copy this repo deploys, not an
        # out-of-tree checkout nothing updates (that one sat 79 commits
        # behind upstream).
        sys.path.insert(0, str(REPO / "scripts"))
        try:
            import episodic_doctor
        finally:
            sys.path.pop(0)
        linked = episodic_doctor.linked_root()
        vendored = episodic_doctor.VENDORED
        if linked is None or linked.resolve() != vendored.resolve():
            r.add(FAIL, "episodic CLI",
                  f"episodic-memory on PATH is {linked}, not {vendored} "
                  "(scripts/deploy.py links it)")
        else:
            r.add(PASS, "episodic CLI", f"vendored ({vendored})")
    try:
        with urllib.request.urlopen(f"{url}/health?fresh=1", timeout=90) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
        except ValueError:
            r.add(bad, f"episodic {url}", f"HTTP {e.code}")
            return
    except (OSError, ValueError) as e:
        r.add(bad, f"episodic {url}", f"unreachable: {e}")
        return
    if "cli_ok" not in body:
        # A server from before the CLI probe: its "ok" means only that a
        # directory exists.
        r.add(WARN, f"episodic {url}", "server predates the CLI probe; redeploy it")
        return
    if not body.get("cli_ok"):
        detail = body.get("hint") or body.get("error") or "CLI failed"
        r.add(bad, f"episodic {url}", detail)
        return
    age = body.get("index_age_hours")
    r.add(PASS, f"episodic {url}",
          "CLI ok" + (f", index updated {age} h ago" if age is not None else ""))


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
        check_embedder(r)
        check_lsp_daemons(r)
        check_episodic(r)
        check_skills(r)
        check_store(r)

    failed = r.failed
    warned = sum(1 for s, _, _ in r.rows if s == WARN)
    print(f"\n{len(r.rows)} checks — {len(r.rows) - failed - warned} ok, "
          f"{warned} warn, {failed} fail")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
