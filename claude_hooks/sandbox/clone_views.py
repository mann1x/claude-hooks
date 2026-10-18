"""Writable lane views of declared write scopes: a reflink clone per lane.

Plan E3 (design 04 §5.4), the ``write`` shell mode with
``write_scopes``, on any tree — a git worktree needs a git root, and a
root like ``backup_models`` (50 TB, not a repository) can only be given
a writable view of the part the council works on. Each scope (a
directory under the root) is cloned per lane with ``cp --reflink``:
on XFS / btrfs the clone shares every data extent with the original, so
it costs metadata, not data (measured on backup_models: ``scripts/``,
4,746 files / 137 GiB, cloned in 0.24 s warm and 9.6 s cold, 2 MiB of
disk). A filesystem without reflink falls back to a plain copy only
while the scope is under ``copy_cap_bytes``; past it the view is refused,
never silently made expensive.

Inside the sandbox each clone is bound read-write at the scope's real
path; the rest of the root stays read-only. The user's files are never
written until an explicit :meth:`CloneViews.apply`.

**Lifecycle** — the same states and API as the git views
(:class:`claude_hooks.sandbox.views.SessionViews`), in ``sandbox.json``
with ``"kind": "clone"``:

    creating → active → (council ends) → held   (changed files kept)
                                       → empty  (nothing changed; removed)
    held → applied | discarded | expired

A **manifest** of every entry (type, size, mtime, mode, owner, and the
clone's ctime after it was handed to the sandbox user) is written when
the clone is made. Freezing walks the clone against it: an entry whose
ctime moved was touched — ``touch -d`` resets mtime but never ctime —
and is moved (a rename, free) into ``held/``; the clone is removed. A
held lane is its changed files plus ``changes.json``, and a review diff
in ``changes.patch``. :meth:`apply` refuses any file the user changed
since the clone (its size or mtime no longer the manifest's), so a
held copy never overwrites newer work.
"""
from __future__ import annotations

import difflib
import errno
import json
import logging
import os
import shutil
import stat
import subprocess
import threading
import time
from pathlib import Path
from typing import Iterable, Optional

from claude_hooks.sandbox.views import (
    ACTIVE, HELD, TERMINAL, STORE_PARTS, Mounts, ViewError, _atomic_json,
    _read_json, _registry_update)

log = logging.getLogger("claude_hooks.sandbox.clone_views")

KIND = "clone"
#: Entries a scope may hold; a bigger tree is refused (the manifest and
#: the freeze walk are per entry).
MAX_SCOPE_ENTRIES = 250_000
#: Text files up to this size get a unified diff in ``changes.patch``.
DIFF_MAX_BYTES = 1 << 20
#: ``FICLONE`` (linux/fs.h): share src's extents with dst.
FICLONE = 0x40049409


# ------------------------------------------------------------------ helpers
def resolve_scopes(root: str, scopes: Iterable[str]) -> list[str]:
    """``scopes`` as real directories under ``root``, deduplicated and
    without nesting (an inner scope of an outer one is dropped). Raises
    :class:`ViewError` for one that is outside the root, missing, not a
    directory, or the session store."""
    root = os.path.realpath(root)
    out: list[str] = []
    for s in scopes:
        if not isinstance(s, str) or not s.strip():
            raise ViewError("a write scope must be a non-empty path")
        p = os.path.realpath(s if os.path.isabs(s) else os.path.join(root, s))
        if p != root and not p.startswith(root + os.sep):
            raise ViewError(f"write scope {s!r} is outside the project root "
                            f"{root}")
        if not os.path.isdir(p):
            raise ViewError(f"write scope {s!r} is not a directory")
        rel = os.path.relpath(p, root).split(os.sep)
        if tuple(rel[:2]) == STORE_PARTS:
            raise ViewError(f"write scope {s!r} is the council's own store")
        out.append(p)
    out = sorted(set(out), key=len)
    kept: list[str] = []
    for p in out:
        if not any(p == k or p.startswith(k + os.sep) for k in kept):
            kept.append(p)
    if not kept:
        raise ViewError("write mode on this root needs at least one write "
                        "scope")
    return kept


def _scan(path: str, exclude: tuple[str, ...], limit: int) -> tuple[int, int]:
    """``(entries, regular-file bytes)`` under ``path``; stops past
    ``limit`` entries."""
    n = size = 0
    stack = [path]
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                if e.path in exclude:
                    continue
                n += 1
                if n > limit:
                    return n, size
                try:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                    elif e.is_file(follow_symlinks=False):
                        size += e.stat(follow_symlinks=False).st_size
                except OSError:
                    pass
    return n, size


def _first_file(path: str) -> Optional[str]:
    for dp, _dns, fns in os.walk(path):
        for f in fns:
            p = os.path.join(dp, f)
            if os.path.isfile(p) and not os.path.islink(p):
                return p
    return None


def clone_file(src: str, dst: str) -> bool:
    """Copy ``src`` to ``dst`` sharing extents when the filesystem can
    (``FICLONE``), else byte for byte. Returns whether it reflinked."""
    import fcntl
    with open(src, "rb") as fs, open(dst, "wb") as fd:
        try:
            fcntl.ioctl(fd.fileno(), FICLONE, fs.fileno())
            linked = True
        except OSError:
            shutil.copyfileobj(fs, fd, 1 << 20)
            linked = False
    shutil.copystat(src, dst, follow_symlinks=False)
    return linked


def reflink_supported(src_dir: str, dst_dir: str) -> Optional[bool]:
    """Whether a file under ``src_dir`` can be reflinked into ``dst_dir``
    (same filesystem and one that shares extents); None when there is
    no regular file to try."""
    import fcntl
    probe = _first_file(src_dir)
    if probe is None:
        return None
    os.makedirs(dst_dir, exist_ok=True)
    tmp = os.path.join(dst_dir, f".reflink-probe-{os.getpid()}")
    try:
        with open(probe, "rb") as fs, open(tmp, "wb") as fd:
            fcntl.ioctl(fd.fileno(), FICLONE, fs.fileno())
        return True
    except OSError:
        return False
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _cp(src: str, dst: str, reflink: bool) -> None:
    r = subprocess.run(["cp", "-a", "--no-target-directory",
                        f"--reflink={'always' if reflink else 'never'}",
                        src, dst], capture_output=True, text=True)
    if r.returncode != 0:
        raise ViewError(f"could not clone {src}: "
                        f"{(r.stderr or r.stdout).strip()[:400]}")


def _clone_tree(src: str, dst: str, exclude: tuple[str, ...],
                reflink: bool) -> None:
    """``cp -a`` of ``src`` to ``dst``, leaving out every path in
    ``exclude`` (the session store, when a scope contains it)."""
    inner = [x for x in exclude if x.startswith(src + os.sep)]
    if not inner:
        _cp(src, dst, reflink)
        return
    os.mkdir(dst)
    for e in os.scandir(src):
        if e.path in exclude:
            # An empty stand-in: the sandbox masks the store with a tmpfs
            # there, and a mountpoint it had to create would read as a
            # change of the lane's.
            os.mkdir(os.path.join(dst, e.name))
            continue
        if e.is_dir(follow_symlinks=False) and \
                any(x.startswith(e.path + os.sep) for x in inner):
            _clone_tree(e.path, os.path.join(dst, e.name), exclude, reflink)
        else:
            _cp(e.path, os.path.join(dst, e.name), reflink)
    shutil.copystat(src, dst, follow_symlinks=False)


def _kind(st: os.stat_result) -> str:
    m = st.st_mode
    return ("d" if stat.S_ISDIR(m) else "l" if stat.S_ISLNK(m)
            else "f" if stat.S_ISREG(m) else "o")


def _walk(path: str):
    """Every entry under ``path`` as ``(rel, abs)``, parents first."""
    for dp, dns, fns in os.walk(path):
        dns.sort()
        for n in sorted(dns + fns):
            a = os.path.join(dp, n)
            yield os.path.relpath(a, path), a


def _hand_over(clone: str, uid: Optional[int]) -> dict:
    """Give the clone to ``uid`` and return its manifest: per entry the
    original's ``[kind, size, mtime_ns, mode, uid, gid, link]`` plus the
    clone's ctime after the hand-over."""
    man: dict[str, list] = {}
    chown = uid is not None and hasattr(os, "lchown")
    if chown:
        os.chown(clone, uid, uid)
    for rel, a in _walk(clone):
        st = os.lstat(a)
        k = _kind(st)
        if chown:
            try:
                os.lchown(a, uid, uid)
            except OSError:
                pass
            ct = os.lstat(a).st_ctime_ns
        else:
            ct = st.st_ctime_ns
        man[rel] = [k, st.st_size if k == "f" else 0, st.st_mtime_ns,
                    stat.S_IMODE(st.st_mode), st.st_uid, st.st_gid,
                    os.readlink(a) if k == "l" else None, ct]
    return man


def _past_the_tick(where: str, newest_ns: int, limit_s: float = 2.0) -> None:
    """Return once a new inode's ctime is past ``newest_ns``. File times
    come from the kernel's coarse clock (a tick of a few ms): a write in
    the tick the manifest was taken in would carry the same ctime — and,
    same size, be invisible to :func:`changes`."""
    probe = os.path.join(where, f".tick-{os.getpid()}-{threading.get_ident()}")
    deadline = time.monotonic() + limit_s
    try:
        while time.monotonic() < deadline:
            with open(probe, "w"):
                pass
            if os.lstat(probe).st_ctime_ns > newest_ns:
                return
            os.unlink(probe)
            time.sleep(0.001)
    finally:
        try:
            os.unlink(probe)
        except OSError:
            pass


def changes(clone: str, manifest: dict) -> list[dict]:
    """What the clone differs in from its manifest: ``A`` / ``M`` / ``D``
    per path (directories only when added or removed)."""
    seen: set[str] = set()
    out: list[dict] = []
    for rel, a in _walk(clone):
        seen.add(rel)
        try:
            st = os.lstat(a)
        except OSError:
            continue
        k = _kind(st)
        base = manifest.get(rel)
        if base is None:
            out.append({"status": "A", "path": rel, "kind": k})
            continue
        if k != base[0]:
            out.append({"status": "M", "path": rel, "kind": k})
            continue
        if k == "d":
            continue
        if st.st_ctime_ns != base[7] or st.st_mtime_ns != base[2] \
                or (k == "f" and st.st_size != base[1]) \
                or (k == "l" and os.readlink(a) != base[6]):
            out.append({"status": "M", "path": rel, "kind": k})
    # Every removed entry is listed, a removed directory's contents
    # included: apply deletes file by file, each checked against the
    # user's tree, and removes a directory only once it is empty.
    for rel in sorted(r for r in manifest if r not in seen):
        out.append({"status": "D", "path": rel, "kind": manifest[rel][0]})
    return out


def _is_text(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return b"\0" not in f.read(8192)
    except OSError:
        return False


def _read_lines(path: Optional[str]) -> list[str]:
    if not path or not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.readlines()


def review_patch(items: list[dict], orig_root: str, held_root: str) -> str:
    """A unified diff of the held files against the user's tree as it
    is now — for review only; :meth:`CloneViews.apply` copies files."""
    parts: list[str] = []
    for it in items:
        rel, k = it["path"], it.get("kind")
        o, h = os.path.join(orig_root, rel), os.path.join(held_root, rel)
        if k == "d":
            parts.append(f"{'new' if it['status'] == 'A' else 'deleted'} "
                         f"directory {rel}\n")
            continue
        if k == "l":
            tgt = os.readlink(h) if it["status"] != "D" else ""
            parts.append(f"symlink {rel} -> {tgt or '(deleted)'}\n")
            continue
        big = any(os.path.isfile(p) and os.path.getsize(p) > DIFF_MAX_BYTES
                  for p in (o, h))
        if big or any(os.path.isfile(p) and not _is_text(p) for p in (o, h)):
            parts.append(f"Binary or large file {rel} "
                         f"{'added' if it['status'] == 'A' else 'deleted' if it['status'] == 'D' else 'differs'}\n")
            continue
        a = _read_lines(o) if it["status"] != "A" else []
        b = _read_lines(h) if it["status"] != "D" else []
        parts.append("".join(difflib.unified_diff(
            a, b, fromfile=f"a/{rel}" if a or it["status"] != "A" else "/dev/null",
            tofile=f"b/{rel}" if it["status"] != "D" else "/dev/null")))
    return "".join(p if p.endswith("\n") else p + "\n" for p in parts if p)


# -------------------------------------------------------------------- views
class CloneViews:
    """All lane views of one council session's write scopes."""

    kind = KIND

    def __init__(self, *, sid: str, root: str, session_dir: str,
                 scopes: Optional[Iterable[str]] = None,
                 uid: Optional[int] = None,
                 copy_cap_bytes: int = 2 * 2**30):
        self.sid = sid
        self.repo = os.path.realpath(root)      # the name SessionViews uses
        self.dir = Path(session_dir)
        self.uid = uid
        self.copy_cap_bytes = int(copy_cap_bytes)
        self._lock = threading.RLock()
        saved = _read_json(self.state_path).get("scopes")
        self.scopes = resolve_scopes(self.repo, scopes if scopes else
                                     (saved or []))

    # ------------------------------------------------------------ state
    @property
    def state_path(self) -> Path:
        return self.dir / "sandbox.json"

    def _load(self) -> dict:
        d = _read_json(self.state_path)
        d.setdefault("sid", self.sid)
        d.setdefault("repo", self.repo)
        d.setdefault("kind", KIND)
        d.setdefault("scopes", self.scopes)
        d.setdefault("lanes", {})
        return d

    def _save(self, d: dict) -> None:
        _atomic_json(self.state_path, d)
        live = {k: v["state"] for k, v in d["lanes"].items()
                if v.get("state") in ACTIVE + (HELD,)}
        _registry_update(f"{self.repo}::{self.sid}", {
            "sid": self.sid, "repo": self.repo, "kind": KIND,
            "session_dir": str(self.dir), "lanes": live,
            "updated_at": time.time()} if live else None)

    def lanes(self) -> dict:
        return self._load()["lanes"]

    def lane_dir(self, lane: str) -> Path:
        return self.dir / "lanes" / lane

    def _store(self) -> str:
        return os.path.join(self.repo, *STORE_PARTS)

    # ----------------------------------------------------------- ensure
    def ensure(self, lane: str) -> Mounts:
        """The lane's view, created on its first command."""
        with self._lock:
            rec = self._load()["lanes"].get(lane)
            if rec and rec.get("state") == "active" and \
                    all(os.path.isdir(c) for c in rec.get("clones", {}).values()):
                return self._mounts(rec)
            if rec and rec.get("state") not in (None, "creating", "active"):
                raise ViewError(f"this lane's view is {rec['state']}; it takes "
                                "no more commands")
            ldir = self.lane_dir(lane)
            d = self._load()
            d["lanes"][lane] = {"state": "creating", "kind": KIND,
                                "created_at": time.time()}
            self._save(d)
            shutil.rmtree(ldir / "clone", ignore_errors=True)  # a cut-short try
            (ldir / "clone").mkdir(parents=True, exist_ok=True)
            exclude = (self._store(),)
            clones, manifests, how = {}, {}, {}
            t0 = time.monotonic()
            try:
                for i, scope in enumerate(self.scopes):
                    n, size = _scan(scope, exclude, MAX_SCOPE_ENTRIES)
                    if n > MAX_SCOPE_ENTRIES:
                        raise ViewError(
                            f"write scope {scope} holds more than "
                            f"{MAX_SCOPE_ENTRIES} entries; name a smaller one")
                    dst = str(ldir / "clone" / str(i))
                    rl = reflink_supported(scope, str(ldir / "clone"))
                    if rl is False and size > self.copy_cap_bytes:
                        raise ViewError(
                            f"write scope {scope} is {size / 2**30:.1f} GiB "
                            f"on a filesystem that cannot reflink it into "
                            f"the session store, and a plain copy is capped "
                            f"at {self.copy_cap_bytes / 2**30:.1f} GiB "
                            f"([tools.shell] lane_disk_gib); name a smaller "
                            f"scope")
                    _clone_tree(scope, dst, exclude, reflink=rl is not False)
                    manifests[str(i)] = _hand_over(dst, self.uid)
                    clones[scope] = dst
                    how[scope] = "reflink" if rl is not False else "copy"
            except (ViewError, OSError) as e:
                shutil.rmtree(ldir / "clone", ignore_errors=True)
                d = self._load()
                d["lanes"].pop(lane, None)
                self._save(d)
                raise e if isinstance(e, ViewError) else ViewError(str(e))
            _past_the_tick(str(ldir), max(
                (e[7] for m in manifests.values() for e in m.values()),
                default=0))
            man_path = ldir / "manifest.json"
            _atomic_json(man_path, manifests)
            d = self._load()
            d["lanes"][lane].update(
                state="active", clones=clones, how=how,
                manifest=str(man_path),
                wt=clones.get(self.repo, ""),     # the whole root, if cloned
                clone_s=round(time.monotonic() - t0, 3))
            self._save(d)
            return self._mounts(d["lanes"][lane])

    @staticmethod
    def _mounts(rec: dict) -> Mounts:
        m = Mounts()
        for scope, clone in sorted(rec.get("clones", {}).items(),
                                   key=lambda kv: len(kv[0])):
            m.binds.append(("rw", clone, scope))
        return m

    def mappings(self, lane: str) -> list[tuple[str, str]]:
        """``(real path, host path in the view)`` per scope of an active
        lane, longest first."""
        rec = self.lanes().get(lane) or {}
        if rec.get("state") != "active":
            return []
        return sorted(rec.get("clones", {}).items(), key=lambda kv: -len(kv[0]))

    def host_path(self, lane: str, path: str) -> str:
        real = os.path.realpath(path)
        for scope, clone in self.mappings(lane):
            if real == scope or real.startswith(scope + os.sep):
                return clone + real[len(scope):]
        return path

    def _scope_items(self, rec: dict) -> list[tuple[str, str, dict]]:
        mans = _read_json(Path(rec.get("manifest", "")))
        out = []
        for scope, clone in rec.get("clones", {}).items():
            idx = os.path.basename(clone)
            out.append((scope, clone, mans.get(idx, {})))
        return out

    def status(self, lane: str) -> list[str]:
        rec = self.lanes().get(lane) or {}
        if rec.get("state") != "active":
            return []
        lines = []
        for scope, clone, man in self._scope_items(rec):
            rel_scope = os.path.relpath(scope, self.repo)
            for c in changes(clone, man):
                p = os.path.normpath(os.path.join(rel_scope, c["path"]))
                lines.append(f"{c['status']} {p}" + ("/" if c["kind"] == "d"
                                                     else ""))
        return lines

    # ----------------------------------------------------------- freeze
    def freeze(self, lane: str) -> dict:
        """Keep the lane's changed files in ``held/`` and remove its clone.
        Returns the lane record (``held`` / ``empty`` / ``lost``)."""
        with self._lock:
            d = self._load()
            rec = d["lanes"].get(lane)
            if not rec or rec.get("state") not in ACTIVE:
                return rec or {}
            ldir = self.lane_dir(lane)
            if rec.get("state") == "creating" or not rec.get("clones"):
                shutil.rmtree(ldir / "clone", ignore_errors=True)
                rec.update(state="empty", frozen_at=time.time())
                self._save(d)
                return rec
            held = ldir / "held"
            shutil.rmtree(held, ignore_errors=True)
            items: list[dict] = []
            try:
                for scope, clone, man in self._scope_items(rec):
                    if not os.path.isdir(clone):
                        raise ViewError(f"the clone of {scope} is gone")
                    rel_scope = os.path.relpath(scope, self.repo)
                    for c in changes(clone, man):
                        rel = os.path.normpath(os.path.join(rel_scope,
                                                            c["path"]))
                        base = man.get(c["path"])
                        item = dict(c, path=rel, base=base)
                        if c["status"] != "D" and c["kind"] != "d":
                            dst = held / rel
                            dst.parent.mkdir(parents=True, exist_ok=True)
                            os.rename(os.path.join(clone, c["path"]), dst)
                        elif c["status"] == "A" and c["kind"] == "d":
                            (held / rel).mkdir(parents=True, exist_ok=True)
                        items.append(item)
            except (ViewError, OSError) as e:
                log.error("capture of %s lane %s failed: %s", self.sid, lane, e)
                rec.update(state="lost", error=str(e), frozen_at=time.time())
                self._save(d)
                return rec
            if items:
                _atomic_json(ldir / "changes.json", {"items": items})
                (ldir / "changes.patch").write_text(
                    review_patch(items, self.repo, str(held)), encoding="utf-8")
                rec.update(state=HELD, held=str(held),
                           patch=str(ldir / "changes.patch"),
                           files=[{"status": i["status"],
                                   "path": i["path"] + ("/" if i["kind"] == "d"
                                                        else ""),
                                   "added": "-", "removed": "-"}
                                  for i in items])
            else:
                rec.update(state="empty", files=[])
            rec["frozen_at"] = time.time()
            shutil.rmtree(ldir / "clone", ignore_errors=True)
            self._save(d)
            return rec

    def freeze_all(self) -> dict:
        return {lane: self.freeze(lane) for lane, rec in self.lanes().items()
                if rec.get("state") in ACTIVE}

    # ---------------------------------------------------- apply/discard
    def _conflicts(self, items: list[dict]) -> list[str]:
        bad = []
        for it in items:
            p = os.path.join(self.repo, it["path"])
            base = it.get("base")
            try:
                st = os.lstat(p)
            except FileNotFoundError:
                st = None
            if it["status"] == "A":
                if st is not None and not (it["kind"] == "d"
                                           and stat.S_ISDIR(st.st_mode)):
                    bad.append(f"{it['path']}: exists in your tree now")
                continue
            if st is None:
                bad.append(f"{it['path']}: gone from your tree")
                continue
            if base and base[0] != "d" and (
                    _kind(st) != base[0] or st.st_mtime_ns != base[2]
                    or (base[0] == "f" and st.st_size != base[1])):
                bad.append(f"{it['path']}: changed in your tree since the "
                           f"council cloned it")
        return bad

    def apply(self, lane: str, *, dry_run: bool = False,
              three_way: bool = False) -> str:
        """Copy a held lane's files into the user's tree (and delete what
        it deleted). Refuses when any of them changed there since the
        clone. ``dry_run`` only checks."""
        with self._lock:
            d = self._load()
            rec = d["lanes"].get(lane)
            if not rec or rec.get("state") != HELD:
                raise ViewError(f"lane {lane!r} has no held changes "
                                f"({(rec or {}).get('state', 'unknown')})")
            if three_way:
                raise ViewError("a write-scope view holds files, not a git "
                                "patch: there is no 3-way merge. Review "
                                "the diff and apply, or copy by hand.")
            items = (_read_json(self.lane_dir(lane) / "changes.json")
                     .get("items") or [])
            bad = self._conflicts(items)
            if bad:
                raise ViewError("these held files would overwrite newer "
                                "work, so nothing was applied:\n  "
                                + "\n  ".join(bad[:20])
                                + (f"\n  … and {len(bad) - 20} more"
                                   if len(bad) > 20 else ""))
            if dry_run:
                return "applies cleanly"
            held = Path(rec["held"])
            for it in sorted((i for i in items if i["status"] != "D"),
                             key=lambda i: i["path"].count("/")):
                self._put(it, held)
            kept: list[str] = []
            for it in sorted((i for i in items if i["status"] == "D"),
                             key=lambda i: (i["kind"] == "d",
                                            -i["path"].count("/"))):
                self._remove(it, kept)
            rec.update(state="applied", applied_at=time.time())
            self._save(d)
            return "applied" + (
                f" (kept {len(kept)} deleted director"
                f"{'y' if len(kept) == 1 else 'ies'} that hold new files: "
                + ", ".join(kept[:5]) + ")" if kept else "")

    def _put(self, it: dict, held: Path) -> None:
        if it["kind"] == "o":
            return       # a fifo or socket: nothing a copy could carry
        dst = os.path.join(self.repo, it["path"])
        src = str(held / it["path"])
        parent = os.path.dirname(dst)
        if not os.path.isdir(parent):
            os.makedirs(parent)
        pst = os.stat(parent)
        base = it.get("base")
        owner = (base[4], base[5]) if base else (pst.st_uid, pst.st_gid)
        if it["kind"] == "d":
            if os.path.lexists(dst) and (os.path.islink(dst)
                                         or not os.path.isdir(dst)):
                os.unlink(dst)
            if not os.path.isdir(dst):
                os.mkdir(dst)
                shutil.copystat(src, dst)
            self._chown(dst, owner)
            return
        tmp = os.path.join(parent, f".{os.path.basename(dst)}.council-{os.getpid()}")
        if it["kind"] == "l":
            os.symlink(os.readlink(src), tmp)
        else:
            clone_file(src, tmp)
        self._chown(tmp, owner)
        if it["kind"] != "l" and os.path.isdir(dst) and not os.path.islink(dst):
            shutil.rmtree(dst)
        os.replace(tmp, dst)

    @staticmethod
    def _chown(path: str, owner: tuple[int, int]) -> None:
        try:
            os.lchown(path, *owner)
        except OSError as e:
            if e.errno != errno.EPERM:
                raise

    def _remove(self, it: dict, kept: list[str]) -> None:
        p = os.path.join(self.repo, it["path"])
        if it["kind"] == "d" and os.path.isdir(p) and not os.path.islink(p):
            try:
                os.rmdir(p)
            except OSError as e:
                if e.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                    raise
                kept.append(it["path"])   # the user put something in it
        elif it["kind"] != "d" and os.path.lexists(p):
            os.unlink(p)

    def discard(self, lane: str) -> dict:
        with self._lock:
            d = self._load()
            rec = d["lanes"].get(lane)
            if not rec:
                raise ViewError(f"no lane {lane!r} in {self.sid}")
            ldir = self.lane_dir(lane)
            if rec.get("state") in TERMINAL:
                return rec
            shutil.rmtree(ldir / "clone", ignore_errors=True)
            shutil.rmtree(ldir / "held", ignore_errors=True)
            rec.update(state="discarded", discarded_at=time.time())
            self._save(d)
            return rec

    def expire(self, lane: str) -> dict:
        """A held lane past its TTL: its files move to ``expired/<lane>``
        (kept, never silently lost)."""
        with self._lock:
            d = self._load()
            rec = d["lanes"].get(lane)
            if not rec or rec.get("state") != HELD:
                return rec or {}
            dst = self.dir / "expired" / lane
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(rec["held"], dst)
                rec["held"] = str(dst)
            except OSError:
                pass
            rec.update(state="expired", expired_at=time.time())
            self._save(d)
            return rec


__all__ = ["CloneViews", "resolve_scopes", "changes", "clone_file",
           "reflink_supported", "review_patch", "KIND", "MAX_SCOPE_ENTRIES"]
