"""The host's own toolchains, made visible inside the council sandbox.

Council run csl-2026-10-18-1120-95ce (mail #1625): no research lane
could run vitest. ``node`` in the jail was ``/usr/bin/node`` v12, because
the host's current one, ``/usr/local/bin/node``, is a symlink into
``/root/.hermes/node`` — a directory the sandbox user cannot reach — and
``bun`` lived in ``/root/.bun/bin``, which was not on the jail's PATH at
all. Lanes spent their iterations on ``which bun`` and ``ls ~/.bun``.

:func:`discover` finds each tool in :data:`TOOLS` the way the engine's
user would (its ``PATH``, then the usual per-user install dirs), follows
the symlink to the real binary, and returns what the jail must mount:

- a binary the sandbox user can already reach is bound in place (its
  ``bin`` directory, or the install prefix for ``node`` so ``npm`` /
  ``npx`` find their ``lib``);
- one under a directory it cannot traverse (a mode-700 home) is mirrored
  once into :func:`mirror_root` — root-owned, world-readable — keyed on
  the source's identity, so an upgrade on the host makes a new mirror;
- nothing is ever bound from a directory that is a home itself, and a
  prefix over :data:`MIRROR_MAX_BYTES` is never mirrored.

The result's ``bins`` go first on the jail's PATH, so the host's current
``node`` wins over the distribution's old one.
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import stat
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger("claude_hooks.sandbox.toolchains")

#: Tools a lane is likely to need that are often installed per user.
#: Not rustup's proxies (cargo / rustc need ``~/.rustup`` too), and not
#: a tool living in a virtualenv's ``bin`` (its ``python`` would shadow
#: the system one on the jail's PATH).
TOOLS = ("node", "bun", "deno", "go")
#: Per-user install dirs searched after the engine's PATH.
HOME_BIN_DIRS = (".bun/bin", ".deno/bin", ".cargo/bin", ".local/bin",
                 "go/bin", ".volta/bin")
#: Tools whose install prefix (``<prefix>/bin/<tool>``) is mounted, not
#: only the binary's directory: their launchers read ``<prefix>/lib``.
PREFIX_TOOLS = ("node", "go")
MIRROR_MAX_BYTES = 2 * 2**30
#: Already visible in every jail.
SYSTEM_DIRS = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc")
_LOCK = threading.Lock()
_CACHE: dict[tuple, "Toolchains"] = {}


@dataclass
class Toolchains:
    #: Directories to bind read-only at their own path, in order.
    binds: list[str] = field(default_factory=list)
    #: Directories to put first on the jail's PATH, in order.
    bins: list[str] = field(default_factory=list)
    #: ``tool -> real binary as the jail sees it``, for the tool text.
    found: dict[str, str] = field(default_factory=dict)
    #: What was not made visible, and why.
    skipped: dict[str, str] = field(default_factory=dict)


def mirror_root() -> Path:
    override = os.environ.get("CLAUDE_HOOKS_SANDBOX_TOOLCHAINS")
    return Path(override) if override else Path(
        "/var/cache/claude-hooks-sandbox/toolchains")


def reachable_by_others(path: str) -> bool:
    """Whether a user who is neither the owner nor in the group can
    reach ``path`` (every ancestor ``o+x``, the file ``o+r``)."""
    p = Path(path)
    try:
        if not p.stat().st_mode & stat.S_IROTH:
            return False
        for parent in p.parents:
            if not parent.stat().st_mode & stat.S_IXOTH:
                return False
    except OSError:
        return False
    return True


def _homes() -> set[str]:
    out = {os.path.realpath(os.path.expanduser("~")), "/root", "/home"}
    try:
        out.update(os.path.realpath(os.path.join("/home", d))
                   for d in os.listdir("/home"))
    except OSError:
        pass
    return out


def _find(tool: str, path: Optional[str]) -> Optional[str]:
    hit = shutil.which(tool, path=path)
    if hit:
        return hit
    home = os.path.expanduser("~")
    extra = os.pathsep.join(os.path.join(home, d) for d in HOME_BIN_DIRS)
    return shutil.which(tool, path=extra)


def _visible(real: str, roots: tuple[str, ...]) -> bool:
    return any(real == d or real.startswith(d.rstrip("/") + "/")
               for d in (*SYSTEM_DIRS, *roots))


def _tree_bytes(path: str, limit: int) -> int:
    total = 0
    for dp, _dns, fns in os.walk(path):
        for f in fns:
            try:
                total += os.lstat(os.path.join(dp, f)).st_size
            except OSError:
                pass
            if total > limit:
                return total
    return total


def _mirror(src: str, tool: str) -> str:
    """``src`` copied once under :func:`mirror_root`; returns the copy."""
    st = os.stat(src)
    key = hashlib.sha256(
        f"{src}\0{st.st_mtime_ns}\0{st.st_ino}".encode()).hexdigest()[:12]
    dst = mirror_root() / f"{tool}-{key}"
    if dst.is_dir():
        return str(dst)
    if _tree_bytes(src, MIRROR_MAX_BYTES) > MIRROR_MAX_BYTES:
        raise OSError(f"{src} is over {MIRROR_MAX_BYTES >> 30} GiB")
    root = mirror_root()
    root.mkdir(parents=True, exist_ok=True)
    for d in (root, *root.parents):
        if str(d) in ("/", "/var", "/var/cache"):
            break
        os.chmod(d, 0o755)
    tmp = root / f".{dst.name}.tmp{os.getpid()}"
    shutil.rmtree(tmp, ignore_errors=True)
    r = subprocess.run(["cp", "-a", "--reflink=auto", "--no-target-directory",
                        src, str(tmp)], capture_output=True, text=True)
    if r.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        raise OSError(f"cp failed: {(r.stderr or r.stdout).strip()[:200]}")
    subprocess.run(["chmod", "-R", "a+rX,go-w", str(tmp)], check=False)
    if os.geteuid() == 0:
        os.chown(tmp, 0, 0)
    os.replace(tmp, dst)
    # Older mirrors of the same tool are left for the next sweep; a jail
    # that is running may still have one bound.
    return str(dst)


def discover(*, roots: tuple[str, ...] = (), path: Optional[str] = None,
             others_must_reach: bool = True) -> Toolchains:
    """What the jail needs to see the host's toolchains. ``path`` is the
    PATH to search (default: this process's). ``others_must_reach``: the
    command runs as another uid (nobody, on a root engine), so a binary
    it cannot traverse to is mirrored."""
    key = (roots, path or os.environ.get("PATH", ""), others_must_reach)
    with _LOCK:
        hit = _CACHE.get(key)
        if hit is not None and all(os.path.isdir(b) for b in hit.binds):
            return hit
        tc = _discover(roots=roots, path=path,
                       others_must_reach=others_must_reach)
        _CACHE[key] = tc
        return tc


def _discover(*, roots, path, others_must_reach) -> Toolchains:
    tc = Toolchains()
    homes = _homes()
    for tool in TOOLS:
        hit = _find(tool, path)
        if not hit:
            continue
        real = os.path.realpath(hit)
        if os.path.basename(real) != tool and tool not in PREFIX_TOOLS:
            tc.skipped[tool] = f"{hit} is a proxy for {real}"
            continue
        if _visible(real, roots):
            continue      # /usr/bin/node and the like: already there
        bindir = os.path.dirname(real)
        prefixed = tool in PREFIX_TOOLS and os.path.basename(bindir) == "bin"
        src = os.path.dirname(bindir) if prefixed else bindir
        if src in homes or os.path.dirname(src) == "/":
            tc.skipped[tool] = f"{real}: its directory is {src}"
            continue
        try:
            if others_must_reach and not reachable_by_others(real):
                src = _mirror(src, tool)
        except OSError as e:
            tc.skipped[tool] = f"{real}: not reachable and not mirrored ({e})"
            log.warning("toolchain %s not made visible: %s", tool, e)
            continue
        jail_bin = os.path.join(src, "bin") if prefixed else src
        if src not in tc.binds:
            tc.binds.append(src)
        if jail_bin not in tc.bins:
            tc.bins.append(jail_bin)
        tc.found[tool] = os.path.join(jail_bin, os.path.basename(real))
    return tc


__all__ = ["Toolchains", "discover", "mirror_root", "reachable_by_others",
           "TOOLS"]
