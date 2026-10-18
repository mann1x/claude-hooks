"""Writable lane views: a git worktree per lane, a patch when it ends.

Plan E3/E4 (design 04 §5.4–5.5), the ``write`` shell mode on a git
root. Each lane that runs a command gets its own worktree, checked out
from one **base** snapshot of the user's tree — HEAD plus uncommitted
and untracked (non-ignored) files, written as a commit under
``refs/claude-hooks/council/<sid>/base`` and never into ``refs/stash``.
Inside the sandbox the worktree is bound at the repo's real path, so
absolute paths, tests and imports behave as in the user's tree; the
user's tree itself is never written.

**Lifecycle** (``sandbox.json`` per session, plus the host registry
``~/.claude/consultants-sandbox-views.json`` so a crash is found again):

    creating → active → (lane ends) → held  (changes captured to changes.patch)
                                    → empty (no changes; removed)
    held → applied | discarded | expired

A view is frozen — changes captured, worktree removed — as soon as its
council ends, so a held lane is a *patch*, not a checkout: nothing big
outlives a council, and nothing is ever mounted in the host namespace
(the worktree is bound only inside each command's own mount namespace).
A council the engine died under is frozen by :func:`sweep` at the next
start unless it is resuming; a ``creating`` view a crash cut short is
removed.

**git inside the jail.** The repo's real path shows the worktree, so
the worktree's ``.git`` file (pointing at the host repo) leads nowhere
there. Instead the common git dir is bound read-only at ``/council/git``,
the worktree's admin dir writable inside it, and new objects go to a
lane-private directory: ``git status / add / commit / stash`` work, the
user's object store and refs are never written from inside.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger("claude_hooks.sandbox.views")

JAIL_GIT = "/council/git"
JAIL_OBJECTS = "/council/objects"
REF_PREFIX = "refs/claude-hooks/council"
#: Ignored entries a view does not mount from the user's tree: our own
#: session store, and caches a command recreates in the view.
SKIP_IGNORED = {"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache",
                ".tox", ".nox"}
STORE_PARTS = (".claude-hooks", "consultants")
#: More ignored entries than this and only directories are mounted.
MAX_IGNORED_BINDS = 200
#: Untracked (non-ignored) bytes the base snapshot may hash into the
#: user's object store. Past it a write view is refused.
MAX_UNTRACKED_BYTES = 256 * 2**20
ACTIVE = ("creating", "active")
HELD = "held"
TERMINAL = ("empty", "applied", "discarded", "expired", "lost")


class ViewError(RuntimeError):
    """A view could not be made or changed; the text is for the model or
    the operator."""


def _git(args: list[str], cwd: str, *, env: Optional[dict] = None,
         check: bool = True, timeout: int = 300) -> subprocess.CompletedProcess:
    full = ["git", "-c", "safe.directory=*", "-c", "core.hooksPath=/dev/null",
            *args]
    e = dict(os.environ)
    for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
              "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
        e.pop(k, None)
    e.update({"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0",
              "GIT_AUTHOR_NAME": "council", "GIT_AUTHOR_EMAIL": "council@localhost",
              "GIT_COMMITTER_NAME": "council",
              "GIT_COMMITTER_EMAIL": "council@localhost"})
    e.update(env or {})
    r = subprocess.run(full, cwd=cwd, env=e, capture_output=True, text=True,
                       timeout=timeout)
    if check and r.returncode != 0:
        raise ViewError(f"git {' '.join(args[:3])} failed: "
                        f"{(r.stderr or r.stdout).strip()[:400]}")
    return r


def toplevel(path: str) -> Optional[str]:
    """The git work tree ``path`` is in, or None."""
    if not shutil.which("git"):
        return None
    try:
        r = _git(["rev-parse", "--show-toplevel"], path, check=False, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    top = r.stdout.strip()
    return os.path.realpath(top) if r.returncode == 0 and top else None


def _chown_tree(path: str, uid: Optional[int]) -> None:
    if uid is None or not hasattr(os, "chown"):
        return
    os.chown(path, uid, uid)
    for dp, dns, fns in os.walk(path):
        for n in dns + fns:
            try:
                os.lchown(os.path.join(dp, n), uid, uid)
            except OSError:
                pass


def _atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# ---------------------------------------------------------------- registry
def registry_path() -> Path:
    override = os.environ.get("CONSULTANTS_SANDBOX_REGISTRY")
    if override:
        return Path(override)
    return Path(os.path.expanduser("~/.claude/consultants-sandbox-views.json"))


_REG_LOCK = threading.Lock()


def _registry_update(key: str, record: Optional[dict]) -> None:
    with _REG_LOCK:
        p = registry_path()
        data = _read_json(p)
        sessions = data.setdefault("sessions", {})
        if record is None:
            sessions.pop(key, None)
        else:
            sessions[key] = record
        _atomic_json(p, data)


def registered() -> list[dict]:
    return list((_read_json(registry_path()).get("sessions") or {}).values())


# ----------------------------------------------------------------- views
@dataclass
class Mounts:
    """What the backend binds for one command in a lane's view, in order."""
    binds: list[tuple[str, str, str]] = field(default_factory=list)
    env: dict = field(default_factory=dict)


class SessionViews:
    """All lane views of one council session in one repo."""

    def __init__(self, *, sid: str, repo: str, session_dir: str,
                 uid: Optional[int] = None):
        self.sid = sid
        self.repo = os.path.realpath(repo)
        self.dir = Path(session_dir)
        self.uid = uid
        self._lock = threading.RLock()

    # ------------------------------------------------------------ state
    @property
    def state_path(self) -> Path:
        return self.dir / "sandbox.json"

    def _load(self) -> dict:
        d = _read_json(self.state_path)
        d.setdefault("sid", self.sid)
        d.setdefault("repo", self.repo)
        d.setdefault("lanes", {})
        return d

    def _save(self, d: dict) -> None:
        _atomic_json(self.state_path, d)
        live = {k: v["state"] for k, v in d["lanes"].items()
                if v.get("state") in ACTIVE + (HELD,)}
        key = f"{self.repo}::{self.sid}"
        _registry_update(key, {"sid": self.sid, "repo": self.repo,
                               "session_dir": str(self.dir), "lanes": live,
                               "updated_at": time.time()} if live else None)

    def lanes(self) -> dict:
        return self._load()["lanes"]

    def lane_dir(self, lane: str) -> Path:
        return self.dir / "lanes" / lane

    # ------------------------------------------------------------- base
    def base(self) -> str:
        with self._lock:
            d = self._load()
            if d.get("base"):
                return d["base"]
            sha = self._snapshot()
            d["base"] = sha
            d["base_ref"] = f"{REF_PREFIX}/{self.sid}/base"
            d["created_at"] = time.time()
            self._save(d)
            return sha

    def _untracked_bytes(self) -> int:
        r = _git(["ls-files", "--others", "--exclude-standard", "-z"], self.repo)
        total = 0
        for rel in filter(None, r.stdout.split("\0")):
            try:
                total += os.lstat(os.path.join(self.repo, rel)).st_size
            except OSError:
                pass
        return total

    def _snapshot(self) -> str:
        untracked = self._untracked_bytes()
        if untracked > MAX_UNTRACKED_BYTES:
            raise ViewError(
                f"the repo has {untracked / 2**20:.0f} MiB of untracked files; "
                f"a write view would copy them into its base (limit "
                f"{MAX_UNTRACKED_BYTES // 2**20} MiB). Commit, ignore or remove "
                f"them, or use shell mode 'read'.")
        idx = str(self.dir / "base.index")
        self.dir.mkdir(parents=True, exist_ok=True)
        env = {"GIT_INDEX_FILE": idx}
        has_head = _git(["rev-parse", "--verify", "-q", "HEAD"], self.repo,
                        check=False).returncode == 0
        try:
            if has_head:
                _git(["read-tree", "HEAD"], self.repo, env=env)
            _git(["add", "-A"], self.repo, env=env)
            tree = _git(["write-tree"], self.repo, env=env).stdout.strip()
            parent = ["-p", "HEAD"] if has_head else []
            sha = _git(["commit-tree", tree, *parent, "-m",
                        f"council {self.sid}: base snapshot"],
                       self.repo).stdout.strip()
            _git(["update-ref", f"{REF_PREFIX}/{self.sid}/base", sha], self.repo)
        finally:
            try:
                os.unlink(idx)
            except OSError:
                pass
        return sha

    # ----------------------------------------------------------- ensure
    def ensure(self, lane: str) -> Mounts:
        """The lane's view, created on its first command."""
        with self._lock:
            rec = self._load()["lanes"].get(lane)
            if rec and rec.get("state") == "active" and \
                    os.path.isdir(rec.get("wt", "")):
                return self._mounts(lane, rec)
            if rec and rec.get("state") not in (None, "creating", "active"):
                raise ViewError(f"this lane's view is {rec['state']}; it takes "
                                "no more commands")
            base = self.base()
            ldir = self.lane_dir(lane)
            wt, objects = ldir / "wt", ldir / "objects"
            d = self._load()
            d["lanes"][lane] = {"state": "creating", "kind": "git-worktree",
                                "wt": str(wt), "created_at": time.time()}
            self._save(d)
            if wt.exists():   # a creating view a crash cut short
                self._remove_worktree(str(wt))
            ldir.mkdir(parents=True, exist_ok=True)
            _git(["worktree", "add", "--detach", "--lock", "--reason",
                  f"council {self.sid} lane {lane}", str(wt), base], self.repo)
            admin = _git(["rev-parse", "--path-format=absolute", "--git-dir"],
                         str(wt)).stdout.strip()
            objects.mkdir(exist_ok=True)
            for p in (str(wt), admin, str(objects)):
                _chown_tree(p, self.uid)
            d = self._load()
            d["lanes"][lane].update(state="active", admin=admin,
                                    objects=str(objects))
            self._save(d)
            return self._mounts(lane, d["lanes"][lane])

    def _common_dir(self) -> str:
        return _git(["rev-parse", "--path-format=absolute",
                     "--git-common-dir"], self.repo).stdout.strip()

    def _ignored(self) -> list[str]:
        r = _git(["status", "--ignored=traditional", "--untracked-files=normal",
                  "--porcelain=v1", "-z"], self.repo, check=False)
        out = []
        for ent in filter(None, r.stdout.split("\0")):
            if not ent.startswith("!! "):
                continue
            rel = ent[3:].rstrip("/")
            parts = rel.split("/")
            # Never mount the session store (other lanes' views, scratch,
            # transcripts), nor an ignored dir that contains it.
            if parts[-1] in SKIP_IGNORED or parts[0] == ".git" \
                    or tuple(parts[:2]) == STORE_PARTS \
                    or tuple(STORE_PARTS[:len(parts)]) == tuple(parts):
                continue
            out.append(rel)
        if len(out) > MAX_IGNORED_BINDS:
            out = [r for r in out if os.path.isdir(os.path.join(self.repo, r))]
            out = out[:MAX_IGNORED_BINDS]
        return out

    def _mounts(self, lane: str, rec: dict) -> Mounts:
        common = self._common_dir()
        name = os.path.basename(rec["admin"])
        m = Mounts()
        m.binds.append(("rw", rec["wt"], self.repo))
        for rel in self._ignored():
            src = os.path.join(self.repo, rel)
            if os.path.lexists(src) and not os.path.islink(src):
                m.binds.append(("ro", src, src))
        m.binds += [("ro", common, JAIL_GIT),
                    ("rw", rec["admin"], f"{JAIL_GIT}/worktrees/{name}"),
                    ("rw", rec["objects"], JAIL_OBJECTS)]
        m.env = {"GIT_DIR": f"{JAIL_GIT}/worktrees/{name}",
                 "GIT_WORK_TREE": self.repo,
                 "GIT_OBJECT_DIRECTORY": JAIL_OBJECTS,
                 "GIT_ALTERNATE_OBJECT_DIRECTORIES": f"{JAIL_GIT}/objects"}
        return m

    def mappings(self, lane: str) -> list[tuple[str, str]]:
        """``(real path, host path in the view)`` of an active lane."""
        rec = self.lanes().get(lane) or {}
        if rec.get("state") != "active":
            return []
        return [(self.repo, rec["wt"])]

    def host_path(self, lane: str, path: str) -> str:
        """``path`` (a real-tree path) as it is in ``lane``'s view on the
        host, or unchanged when it is outside the repo or there is none."""
        rec = self.lanes().get(lane) or {}
        if rec.get("state") != "active":
            return path
        real = os.path.realpath(path)
        if real == self.repo or real.startswith(self.repo + os.sep):
            return rec["wt"] + real[len(self.repo):]
        return path

    def status(self, lane: str) -> list[str]:
        """What the view differs in from the base (the user's tree as the
        council found it) — committed inside the jail or not — read
        without writing the view's index."""
        d = self._load()
        rec = d["lanes"].get(lane) or {}
        if rec.get("state") != "active" or not d.get("base"):
            return []
        env = self._host_env(rec)
        diff = _git(["diff", "--name-status", d["base"]], rec["wt"], env=env,
                    check=False).stdout.splitlines()
        new = _git(["ls-files", "--others", "--exclude-standard"], rec["wt"],
                   env=env, check=False).stdout.splitlines()
        return ([ln.replace("\t", " ", 1) for ln in diff if ln.strip()]
                + [f"A {p} (untracked)" for p in new if p.strip()])

    @staticmethod
    def _host_env(rec: dict, index: Optional[str] = None) -> dict:
        env = {"GIT_DIR": rec["admin"], "GIT_WORK_TREE": rec["wt"],
               "GIT_ALTERNATE_OBJECT_DIRECTORIES": rec["objects"]}
        if index:
            env["GIT_INDEX_FILE"] = index
        return env

    # ----------------------------------------------------------- freeze
    def freeze(self, lane: str) -> dict:
        """Capture the lane's changes to ``changes.patch`` and remove its
        worktree. Returns the lane record (``held`` / ``empty`` / ``lost``)."""
        with self._lock:
            d = self._load()
            rec = d["lanes"].get(lane)
            if not rec or rec.get("state") not in ACTIVE:
                return rec or {}
            wt = rec.get("wt", "")
            if rec.get("state") == "creating" or not rec.get("admin") \
                    or not os.path.isdir(wt):
                self._remove_worktree(wt)
                rec.update(state="lost" if rec.get("state") == "active" else "empty",
                           frozen_at=time.time())
                shutil.rmtree(rec.get("objects", ""), ignore_errors=True)
                self._save(d)
                self._drop_ref_if_unused(d)
                return rec
            ldir = self.lane_dir(lane)
            idx = str(ldir / "capture.index")
            env = self._host_env(rec, idx)
            try:
                _git(["read-tree", d["base"]], wt, env=env)
                _git(["add", "-A"], wt, env=env)
                names = _git(["diff", "--cached", "--name-status", d["base"]],
                             wt, env=env).stdout.splitlines()
                numstat = _git(["diff", "--cached", "--numstat", d["base"]],
                               wt, env=env).stdout.splitlines()
                patch = _git(["diff", "--cached", "--binary", d["base"]],
                             wt, env=env).stdout
            except ViewError as e:
                log.error("capture of %s lane %s failed: %s", self.sid, lane, e)
                rec.update(state="lost", error=str(e), frozen_at=time.time())
                self._save(d)
                return rec
            finally:
                try:
                    os.unlink(idx)
                except OSError:
                    pass
            if patch.strip():
                (ldir / "changes.patch").write_text(patch, encoding="utf-8")
                rec.update(state=HELD, patch=str(ldir / "changes.patch"),
                           files=_files(names, numstat))
            else:
                rec.update(state="empty", files=[])
            rec["frozen_at"] = time.time()
            self._remove_worktree(wt)
            shutil.rmtree(rec.get("objects", ""), ignore_errors=True)
            self._save(d)
            self._drop_ref_if_unused(d)
            return rec

    def freeze_all(self) -> dict:
        return {lane: self.freeze(lane) for lane, rec in self.lanes().items()
                if rec.get("state") in ACTIVE}

    def _remove_worktree(self, wt: str) -> None:
        if not wt:
            return
        _git(["worktree", "unlock", wt], self.repo, check=False)
        _git(["worktree", "remove", "--force", "--force", wt], self.repo,
             check=False)
        shutil.rmtree(wt, ignore_errors=True)
        _git(["worktree", "prune"], self.repo, check=False)

    def _drop_ref_if_unused(self, d: dict) -> None:
        if any(r.get("state") in ACTIVE + (HELD,) for r in d["lanes"].values()):
            return
        if d.get("base_ref"):
            _git(["update-ref", "-d", d["base_ref"]], self.repo, check=False)

    # ---------------------------------------------------- apply/discard
    def apply(self, lane: str, *, dry_run: bool = False,
              three_way: bool = False) -> str:
        """Apply a held lane's patch to the user's tree. ``dry_run`` only
        checks. Without ``three_way`` the user's index is not touched."""
        with self._lock:
            d = self._load()
            rec = d["lanes"].get(lane)
            if not rec or rec.get("state") != HELD:
                raise ViewError(f"lane {lane!r} has no held changes "
                                f"({(rec or {}).get('state', 'unknown')})")
            args = ["apply", *(["--3way"] if three_way else [])]
            chk = _git(args + ["--check", rec["patch"]], self.repo, check=False)
            if chk.returncode != 0:
                raise ViewError(
                    "the patch does not apply to your tree as it is now:\n"
                    + (chk.stderr or chk.stdout).strip()
                    + ("" if three_way else
                       "\nIt was made against your tree when the council "
                       "started; try --3way, or review the patch by hand."))
            if dry_run:
                return "applies cleanly"
            _git(args + [rec["patch"]], self.repo)
            rec.update(state="applied", applied_at=time.time())
            self._save(d)
            self._drop_ref_if_unused(d)
            return "applied"

    def discard(self, lane: str) -> dict:
        with self._lock:
            d = self._load()
            rec = d["lanes"].get(lane)
            if not rec:
                raise ViewError(f"no lane {lane!r} in {self.sid}")
            if rec.get("state") in ACTIVE:
                self._remove_worktree(rec.get("wt", ""))
                shutil.rmtree(rec.get("objects", ""), ignore_errors=True)
            if rec.get("state") in TERMINAL:
                return rec
            rec.update(state="discarded", discarded_at=time.time())
            self._save(d)
            self._drop_ref_if_unused(d)
            return rec

    def expire(self, lane: str) -> dict:
        """A held patch past its TTL: moved to ``expired/`` (kept, never
        silently lost), the lane marked ``expired``."""
        with self._lock:
            d = self._load()
            rec = d["lanes"].get(lane)
            if not rec or rec.get("state") != HELD:
                return rec or {}
            dst = self.dir / "expired" / f"{lane}.patch"
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(rec["patch"], dst)
            except OSError:
                pass
            rec.update(state="expired", patch=str(dst), expired_at=time.time())
            self._save(d)
            self._drop_ref_if_unused(d)
            return rec


def _files(names: list[str], numstat: list[str]) -> list[dict]:
    counts = {}
    for ln in numstat:
        parts = ln.split("\t")
        if len(parts) >= 3:
            counts[parts[-1]] = (parts[0], parts[1])
    out = []
    for ln in names:
        parts = ln.split("\t")
        if len(parts) < 2:
            continue
        path = parts[-1]
        add, rm = counts.get(path, ("-", "-"))
        out.append({"status": parts[0][0], "path": path, "added": add,
                    "removed": rm})
    return out


def session_views(*, sid: str, cwd: str, uid: Optional[int] = None,
                  scopes: Optional[list] = None,
                  copy_cap_bytes: Optional[int] = None):
    """The session's write views at ``cwd``: reflink clones of
    ``scopes`` when the run declared write scopes (or the session's
    ``sandbox.json`` says it did), else a git worktree per lane when
    ``cwd`` is in a work tree, else None."""
    sdir = os.path.join(cwd, ".claude-hooks", "consultants", sid)
    saved = _read_json(Path(sdir) / "sandbox.json")
    if scopes or saved.get("kind") == "clone":
        from claude_hooks.sandbox.clone_views import CloneViews
        kw = {} if copy_cap_bytes is None else {"copy_cap_bytes": copy_cap_bytes}
        return CloneViews(sid=sid, root=saved.get("repo") or cwd,
                          session_dir=sdir, scopes=scopes, uid=uid, **kw)
    top = toplevel(cwd)
    if top is None:
        return None
    return SessionViews(sid=sid, repo=top, uid=uid, session_dir=sdir)


def _from_record(rec: dict):
    if rec.get("kind") == "clone":
        from claude_hooks.sandbox.clone_views import CloneViews
        return CloneViews(sid=rec["sid"], root=rec["repo"],
                          session_dir=rec["session_dir"])
    return SessionViews(sid=rec["sid"], repo=rec["repo"],
                        session_dir=rec["session_dir"])


def summary(views: SessionViews, *, include_empty: bool = False) -> str:
    """The answer's "Sandbox changes" section, or "" when nothing is held."""
    lanes = {k: v for k, v in views.lanes().items()
             if v.get("state") == HELD or (include_empty and v.get("state"))}
    if not lanes:
        return ""
    held_as = ("as copies of the changed files" if getattr(
        views, "kind", "") == "clone" else "as patches")
    L = ["## Sandbox changes", "",
         f"Your files are UNCHANGED. These changes are held, {held_as}, "
         "from the lanes' private views:", ""]
    for lane, rec in sorted(lanes.items()):
        files = rec.get("files") or []
        L.append(f"- lane `{lane}` — {len(files)} file(s), "
                 f"patch `{rec.get('patch')}`")
        for f in files[:15]:
            L.append(f"  - {f['status']} `{f['path']}` "
                     f"(+{f['added']}/-{f['removed']})")
        if len(files) > 15:
            L.append(f"  - … and {len(files) - 15} more")
    L += ["", f"Review: `claude-consultants changes {views.sid} --diff`; "
          f"apply: `claude-consultants apply {views.sid} --lane <lane>`; "
          f"drop: `claude-consultants discard {views.sid} --all`."]
    return "\n".join(L)


def sweep(*, live_sids: set[str], held_ttl_days: float = 7.0,
          now: Optional[float] = None, dry_run: bool = False) -> list[str]:
    """Freeze views of councils that are not running (a crash, a kill -9),
    and expire held patches past ``held_ttl_days``. Returns what it did."""
    now = time.time() if now is None else now
    did = []
    for rec in registered():
        sid, repo, sdir = rec.get("sid"), rec.get("repo"), rec.get("session_dir")
        if not (sid and repo and sdir):
            continue
        if not os.path.isdir(repo):
            if not dry_run:
                _registry_update(f"{repo}::{sid}", None)
            did.append(f"forget {sid} (repo gone)")
            continue
        try:
            v = _from_record(rec)
        except ViewError as e:      # a scope that is gone
            did.append(f"skip {sid} ({e})")
            continue
        for lane, lrec in v.lanes().items():
            st = lrec.get("state")
            if st in ACTIVE and sid not in live_sids:
                did.append(f"freeze {sid}/{lane}")
                if not dry_run:
                    v.freeze(lane)
            elif st == HELD and now - float(lrec.get("frozen_at") or now) \
                    > held_ttl_days * 86400:
                did.append(f"expire {sid}/{lane}")
                if not dry_run:
                    v.expire(lane)
    return did


__all__ = ["SessionViews", "Mounts", "ViewError", "session_views", "summary",
           "sweep", "toplevel", "registered", "registry_path"]
