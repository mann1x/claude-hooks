#!/usr/bin/env python3
"""Full deploy. The only supported way to deploy this repo to a host.

**Why this exists.** On 2026-08-02 the installed
``~/.claude/skills/consultants/SKILL.md`` was found still at its 21 May
content — 791 lines against the repo's 1591. Ten weeks of sessions had
been loading half a skill: no wait patterns, no review loop, no
``accept`` / ``tool-ack`` verbs. Nothing surfaced it, because a stale
skill does not error; it just quietly instructs the model to drive an
engine that has moved on.

The cause was a *routine*, not a bug. "Deploy" had come to mean::

    pip install -e .  &&  systemctl restart <service>

which makes the engine current and touches nothing else. A skill is not
loaded by any service — Claude Code reads it at session start — so it
sat outside that definition and drifted unnoticed. Every artifact class
below has the same property: nothing at runtime complains when it is
behind.

So deploy is not a habit anymore, it is this script. It discovers what
needs doing rather than carrying a hardcoded list (a hardcoded list is
how the next artifact class gets forgotten), performs every step, and
then **gates on** ``verify_deploy.py``. A step that fails makes the whole
deploy fail — there is no partial success, because a partial deploy that
reports success is exactly what happened.

What it does, in order:

1. **Repo state** — report HEAD, warn on uncommitted deployable changes.
2. **Packages** — ``pip install -e .`` into every conda env that already
   has the package (editable, so this only matters when entry points or
   dependencies changed — but that is precisely the case a human
   forgets).
3. **Skills** — sync ``.claude/skills/*/SKILL.md`` into
   ``~/.claude/skills/``. Only ones already installed: skills are opt-in
   per host and ``install.py`` is the thing that offers new ones.
4. **Services** — restart every *active* long-running unit that
   references this repo, at whichever scope (user / system) it lives in.
5. **Verify** — run ``verify_deploy.py`` and adopt its exit code.

Usage::

    scripts/deploy.py                # full deploy
    scripts/deploy.py --dry-run      # show every action, change nothing
    scripts/deploy.py --skip-restart # everything except service restarts

Exit code: 0 only if every step succeeded and verification passed.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
USER_SKILLS = Path(os.path.expanduser("~/.claude/skills"))

#: Units that are not long-running services — restarting a oneshot or a
#: backup job as part of a deploy would run the job, which is a side
#: effect nobody asked for.
_SKIP_UNIT_SUBSTRINGS = ("backup", "health", "rollup", "check")


class Step:
    """One deploy action and whether it worked."""

    def __init__(self, name: str):
        self.name = name
        self.ok = True
        self.notes: list[str] = []

    def note(self, msg: str) -> None:
        self.notes.append(msg)
        print(f"    {msg}")

    def fail(self, msg: str) -> None:
        self.ok = False
        self.notes.append(f"FAIL: {msg}")
        print(f"    FAIL: {msg}")


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


# --------------------------------------------------------------------- #
# 1. Repo state
# --------------------------------------------------------------------- #
def step_repo(dry: bool) -> Step:
    s = Step("repo")
    print("\n[1/5] repo state")
    head = _run(["git", "-C", str(REPO), "log", "--oneline", "-1"])
    if head.returncode != 0:
        s.fail(f"git log failed: {head.stderr.strip()}")
        return s
    s.note(f"HEAD {head.stdout.strip()}")
    branch = _run(["git", "-C", str(REPO), "rev-parse", "--abbrev-ref", "HEAD"])
    s.note(f"branch {branch.stdout.strip()}")

    # Uncommitted changes to deployable paths are a warning, not a
    # failure: deploying a work-in-progress is a legitimate thing to do
    # deliberately. Saying so out loud is what stops it being accidental.
    dirty = _run(["git", "-C", str(REPO), "status", "--porcelain",
                  "--", "claude_hooks", "consultants", ".claude/skills",
                  "bin", "scripts"])
    changed = [ln for ln in dirty.stdout.splitlines() if ln.strip()
               and not ln.startswith("??")]
    if changed:
        s.note(f"WARNING: {len(changed)} uncommitted change(s) in "
               "deployable paths — deploying the working tree, not HEAD")
        for ln in changed[:10]:
            s.note(f"  {ln}")
    return s


# --------------------------------------------------------------------- #
# 2. Packages
# --------------------------------------------------------------------- #
def _envs_with_package() -> list[Path]:
    """Conda envs that already import ``claude_hooks``.

    Discovered, not listed: this host has 25+ envs sharing the editable
    install, and any hardcoded subset would go stale the first time one
    is added.
    """
    roots = [Path(os.path.expanduser("~/anaconda3/envs")),
             Path(os.path.expanduser("~/miniconda3/envs")),
             Path("/opt/conda/envs")]
    found: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for env in sorted(root.iterdir()):
            py = env / "bin" / "python"
            if not py.is_file():
                py = env / "python.exe"          # Windows layout
            if not py.is_file():
                continue
            probe = _run([str(py), "-c", "import claude_hooks"])
            if probe.returncode == 0:
                found.append(py)
    return found


def step_packages(dry: bool, only_env: str | None) -> Step:
    s = Step("packages")
    print("\n[2/5] packages (editable installs)")
    pys = _envs_with_package()
    if only_env:
        pys = [p for p in pys if only_env in str(p)]
    if not pys:
        s.note("no env has claude_hooks installed — nothing to refresh")
        return s
    # Editable installs mean *code* is already live. The reinstall
    # matters for entry points, console scripts and dependency changes —
    # exactly the things nobody remembers to check.
    s.note(f"{len(pys)} env(s) with the package installed")
    targets = [p for p in pys
               if p.parent.parent.name in ("claude-hooks",
                                           "claude-hooks-consultants")]
    if not targets:
        targets = pys[:1]
    for py in targets:
        env_name = py.parent.parent.name
        if dry:
            s.note(f"[dry-run] would pip install -e . in {env_name}")
            continue
        r = _run([str(py), "-m", "pip", "install", "-q", "-e", str(REPO)])
        if r.returncode != 0:
            s.fail(f"{env_name}: pip install -e . -> {r.stderr.strip()[-300:]}")
        else:
            s.note(f"{env_name}: refreshed")
    skipped = len(pys) - len(targets)
    if skipped:
        s.note(f"{skipped} other env(s) share the same editable install "
               "— no per-env action needed")
    return s


# --------------------------------------------------------------------- #
# 3. Skills   <-- the class that drifted for ten weeks
# --------------------------------------------------------------------- #
def step_skills(dry: bool) -> Step:
    s = Step("skills")
    print("\n[3/5] skills")
    repo_skills = REPO / ".claude" / "skills"
    if not repo_skills.is_dir():
        s.note("no in-repo skills directory")
        return s
    if not USER_SKILLS.is_dir():
        s.note(f"nothing installed at {USER_SKILLS} — run install.py first")
        return s

    synced, current, absent = [], 0, []
    for src in sorted(repo_skills.glob("*/SKILL.md")):
        name = src.parent.name
        dst = USER_SKILLS / name / "SKILL.md"
        if not dst.is_file():
            # Opt-in per host; install.py is what offers a new one.
            absent.append(name)
            continue
        try:
            if src.read_text(encoding="utf-8") == dst.read_text(encoding="utf-8"):
                current += 1
                continue
        except OSError as e:
            s.fail(f"/{name}: unreadable ({e})")
            continue
        old_n = len(dst.read_text(encoding="utf-8").splitlines())
        new_n = len(src.read_text(encoding="utf-8").splitlines())
        if dry:
            s.note(f"[dry-run] would update /{name} "
                   f"({old_n} -> {new_n} lines)")
            synced.append(name)
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            s.note(f"/{name} updated ({old_n} -> {new_n} lines)")
            synced.append(name)
        except OSError as e:
            s.fail(f"/{name}: copy failed ({e})")
    if current:
        s.note(f"{current} skill(s) already current")
    if absent:
        s.note(f"not installed on this host (opt-in): {', '.join(absent)}")
    if not synced and not dry:
        s.note("no skill needed updating")
    return s


# --------------------------------------------------------------------- #
# 4. Services
# --------------------------------------------------------------------- #
def _repo_units() -> list[tuple[str, str]]:
    """``(unit, scope)`` for every unit file referencing this repo.

    Both scopes are searched because this host splits them — the
    consultants engine is a ``--user`` unit while the daemon, proxy and
    dashboard are system units. A deploy that knew about only one scope
    would silently leave the other running old code.
    """
    out: list[tuple[str, str]] = []
    locations = [
        (Path("/etc/systemd/system"), "system"),
        (Path(os.path.expanduser("~/.config/systemd/user")), "user"),
    ]
    for d, scope in locations:
        if not d.is_dir():
            continue
        for unit in sorted(d.glob("*.service")):
            try:
                body = unit.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if str(REPO) not in body:
                continue
            if any(k in unit.stem for k in _SKIP_UNIT_SUBSTRINGS):
                continue
            out.append((unit.name, scope))
    return out


def step_services(dry: bool, skip: bool) -> Step:
    s = Step("services")
    print("\n[4/5] services")
    if skip:
        s.note("--skip-restart: not touching any service")
        return s
    units = _repo_units()
    if not units:
        s.note("no systemd unit references this repo")
        return s
    for unit, scope in units:
        base = ["systemctl"] + (["--user"] if scope == "user" else [])
        active = _run(base + ["is-active", "--quiet", unit]).returncode == 0
        if not active:
            s.note(f"{unit} ({scope}) — not running, skipped")
            continue
        if dry:
            s.note(f"[dry-run] would restart {unit} ({scope})")
            continue
        r = _run(base + ["restart", unit])
        if r.returncode != 0:
            s.fail(f"{unit} ({scope}): {r.stderr.strip()[-200:]}")
            continue
        still = _run(base + ["is-active", "--quiet", unit]).returncode == 0
        if still:
            s.note(f"{unit} ({scope}) — restarted")
        else:
            s.fail(f"{unit} ({scope}) did NOT come back up")
    return s


# --------------------------------------------------------------------- #
# 5. Verify
# --------------------------------------------------------------------- #
def step_verify(dry: bool) -> Step:
    s = Step("verify")
    print("\n[5/5] verify")
    if dry:
        s.note("[dry-run] would run scripts/verify_deploy.py")
        return s
    script = REPO / "scripts" / "verify_deploy.py"
    if not script.is_file():
        s.fail("scripts/verify_deploy.py missing")
        return s
    r = subprocess.run([sys.executable, str(script)], text=True)
    if r.returncode != 0:
        s.fail("verification failed — see the checks above")
    else:
        s.note("all checks passed")
    return s


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="print every action, change nothing")
    ap.add_argument("--skip-restart", action="store_true",
                    help="everything except service restarts")
    ap.add_argument("--env", default=None,
                    help="limit the pip refresh to envs matching this string")
    a = ap.parse_args()

    print(f"claude-hooks full deploy — {REPO}"
          + ("  [DRY RUN]" if a.dry_run else ""))

    steps = [
        step_repo(a.dry_run),
        step_packages(a.dry_run, a.env),
        step_skills(a.dry_run),
        step_services(a.dry_run, a.skip_restart),
    ]
    # Verification only means something once the rest actually ran.
    if all(s.ok for s in steps):
        steps.append(step_verify(a.dry_run))
    else:
        print("\n[5/5] verify — SKIPPED (an earlier step failed)")

    failed = [s.name for s in steps if not s.ok]
    print("\n" + "=" * 60)
    if failed:
        # No partial success. A deploy that reports success while one
        # artifact class is behind is the exact failure this replaces.
        print(f"DEPLOY FAILED — {', '.join(failed)}")
        return 1
    print("DEPLOY OK" + (" (dry run — nothing changed)" if a.dry_run else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
