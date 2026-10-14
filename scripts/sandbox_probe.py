#!/usr/bin/env python3
"""What can this host give a council sandbox? A read-mostly capability matrix.

Milestone E of ``docs/PLAN-council-cerebriline.md`` builds the shell
sandbox from layers (namespaces, cgroup v2, seccomp, optional Landlock)
and lane views (git worktree, reflink clone, overlay, copy). Which of
those a host supports is a probe result, never an assumption, and this
script is that probe:

    python scripts/sandbox_probe.py [PATH ...] [--json] [--overlay]
                                    [--floor-gib N] [--floor-pct P]

Each PATH is a root a council might work in (default: the current
directory). For each it reports the filesystem, free space against the
floors, and whether ``FICLONE`` (reflink) works there.

``--overlay`` also runs the overlay copy-up test that settles the
EOVERFLOW question from report 04: lower on the PATH's filesystem, upper
on that filesystem or on tmpfs (default, ``inode32``), with
``xino=auto`` and ``xino=off``. It runs only when a private mount
namespace can be created (``unshare --mount --propagation private``, plus
``--user --map-root-user`` when not root), so nothing is ever mounted in
the host namespace; the namespace and its mounts vanish when the test
process exits.

Writes: only inside a temporary directory created under each PATH
(``.sandbox-probe-*``, removed afterwards). Nothing is written to /tmp.
Linux gets the full matrix; macOS and Windows report what applies there
and mark the rest ``n/a``. Exit status is 0 unless the arguments are bad.
"""
from __future__ import annotations

import argparse
import ctypes
import errno
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

IS_LINUX = sys.platform.startswith("linux")
IS_MAC = sys.platform == "darwin"
IS_WIN = os.name == "nt"

FICLONE = 0x40049409
#: landlock_create_ruleset is 444 on every architecture that has it.
SYS_LANDLOCK_CREATE_RULESET = 444
LANDLOCK_CREATE_RULESET_VERSION = 1


# ---------------------------------------------------------------------- #
# helpers
# ---------------------------------------------------------------------- #
def _run(argv: list[str], timeout: float = 20.0) -> tuple[int, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except FileNotFoundError:
        return 127, f"{argv[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except OSError as e:
        return 126, str(e)


def _read(path: str) -> Optional[str]:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def _na(why: str = "not Linux") -> dict:
    return {"ok": None, "detail": f"n/a ({why})"}


# ---------------------------------------------------------------------- #
# host-wide checks
# ---------------------------------------------------------------------- #
def check_namespaces() -> dict:
    if not IS_LINUX:
        return _na()
    out: dict[str, Any] = {
        "max_user_namespaces": _read("/proc/sys/user/max_user_namespaces"),
        "unprivileged_userns_clone":
            _read("/proc/sys/kernel/unprivileged_userns_clone"),
    }
    unshare = shutil.which("unshare")
    if not unshare:
        out.update(ok=False, detail="unshare(1) not found")
        return out
    for name, flags in (("user", ["--user", "--map-root-user"]),
                        ("pid", ["--pid", "--fork", "--mount-proc"]),
                        ("net", ["--net"]),
                        ("mount", ["--mount", "--propagation", "private"])):
        argv = [unshare] + flags + ["true"]
        if name in ("pid", "net", "mount") and os.geteuid() != 0:
            argv = [unshare, "--user", "--map-root-user"] + flags + ["true"]
        rc, msg = _run(argv)
        out[name] = "ok" if rc == 0 else f"fail ({msg[:120]})"
    out["ok"] = all(out[k] == "ok" for k in ("user", "pid", "net", "mount"))
    out["detail"] = "user, pid, net and mount namespaces" + (
        "" if out["ok"] else ": some unavailable")
    return out


def check_cgroup() -> dict:
    if not IS_LINUX:
        return _na()
    controllers = _read("/sys/fs/cgroup/cgroup.controllers")
    if controllers is None:
        return {"ok": False, "detail": "cgroup v2 not mounted at /sys/fs/cgroup"}
    own = ""
    for line in (_read("/proc/self/cgroup") or "").splitlines():
        if line.startswith("0::"):
            own = line[3:]
    own_dir = Path("/sys/fs/cgroup") / own.lstrip("/")
    out = {
        "controllers": controllers.split(),
        "own_cgroup": own or "/",
        "own_writable": os.access(own_dir, os.W_OK),
        "subtree_control": (_read(str(own_dir / "cgroup.subtree_control"))
                            or "").split(),
        "systemd_run": bool(shutil.which("systemd-run")),
    }
    need = {"cpu", "memory", "pids"}
    out["ok"] = need.issubset(out["controllers"]) and (
        out["own_writable"] or out["systemd_run"])
    out["detail"] = ("cpu/memory/pids available; delegation via "
                     + ("own cgroup" if out["own_writable"]
                        else "systemd-run --scope"))
    return out


def check_seccomp() -> dict:
    if not IS_LINUX:
        return _na()
    actions = (_read("/proc/sys/kernel/seccomp/actions_avail") or "").split()
    mode = ""
    for line in (_read("/proc/self/status") or "").splitlines():
        if line.startswith("Seccomp:"):
            mode = line.split(":", 1)[1].strip()
    return {"ok": "errno" in actions, "actions": actions,
            "this_process_mode": mode,
            "detail": "filter mode available" if actions else "no seccomp"}


def check_landlock() -> dict:
    """Optional layer (plan §11.5): reported, never required."""
    if not IS_LINUX:
        return _na()
    lsm = (_read("/sys/kernel/security/lsm") or "").split(",")
    abi: Optional[int] = None
    err = ""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.syscall.restype = ctypes.c_long
        r = libc.syscall(SYS_LANDLOCK_CREATE_RULESET, None, ctypes.c_size_t(0),
                         ctypes.c_uint32(LANDLOCK_CREATE_RULESET_VERSION))
        if r >= 0:
            abi = int(r)
        else:
            err = errno.errorcode.get(ctypes.get_errno(), str(ctypes.get_errno()))
    except Exception as e:  # pragma: no cover - exotic libc
        err = str(e)
    active = abi is not None
    return {"ok": active, "optional": True, "abi": abi,
            "in_lsm_list": "landlock" in lsm,
            "detail": (f"active, ABI {abi}" if active else
                       f"inactive ({err or 'unknown'}); optional, other "
                       f"layers carry the sandbox")}


def check_bwrap() -> dict:
    exe = shutil.which("bwrap")
    if not exe:
        return {"ok": False, "detail": "bwrap not found"}
    rc, ver = _run([exe, "--version"])
    rc2, msg = _run([exe, "--ro-bind", "/", "/", "--unshare-net",
                     "--unshare-pid", "--dev", "/dev", "--proc", "/proc",
                     "true"])
    return {"ok": rc2 == 0, "version": ver,
            "detail": "runs (ro root, no net, own pid ns)" if rc2 == 0
            else f"present but fails: {msg[:160]}"}


def check_platform_sandbox() -> dict:
    if IS_MAC:
        return {"ok": bool(shutil.which("sandbox-exec")),
                "detail": "sandbox-exec (Seatbelt)"}
    if IS_WIN:
        return {"ok": None, "detail": "AppContainer/Job Object: probe not "
                "implemented; Milestone E7"}
    return _na("Linux uses the layers above")


# ---------------------------------------------------------------------- #
# per-path checks
# ---------------------------------------------------------------------- #
def _fs_type(path: Path) -> str:
    if not IS_LINUX:
        return platform.system().lower()
    best, fstype = "", "?"
    real = str(path.resolve())
    for line in (_read("/proc/self/mounts") or "").splitlines():
        parts = line.split()
        if len(parts) >= 3:
            mnt = parts[1].replace("\\040", " ")
            if (real == mnt or real.startswith(mnt.rstrip("/") + "/")) \
                    and len(mnt) > len(best):
                best, fstype = mnt, parts[2]
    return fstype


def check_free(path: Path, floor_gib: float, floor_pct: float) -> dict:
    u = shutil.disk_usage(path)
    free_gib = u.free / 2**30
    pct = 100.0 * u.free / u.total if u.total else 0.0
    ok = free_gib >= floor_gib and pct >= floor_pct
    return {"ok": ok, "free_gib": round(free_gib, 1),
            "free_pct": round(pct, 1),
            "detail": f"{free_gib:.1f} GiB free ({pct:.1f}%), floor "
                      f"{floor_gib:g} GiB / {floor_pct:g}%"}


def check_ficlone(tmp: Path) -> dict:
    if not (IS_LINUX or IS_MAC):
        return _na("no reflink probe on this OS")
    if IS_MAC:
        return {"ok": None, "detail": "clonefile on APFS: probe not "
                "implemented"}
    import fcntl
    src, dst = tmp / "src", tmp / "dst"
    src.write_bytes(b"x" * 65536)
    try:
        with open(src, "rb") as s, open(dst, "wb") as d:
            fcntl.ioctl(d.fileno(), FICLONE, s.fileno())
        return {"ok": True, "detail": "reflink works"}
    except OSError as e:
        return {"ok": False,
                "detail": f"no reflink ({errno.errorcode.get(e.errno, e.errno)})"}


_OVERLAY_SCRIPT = r"""
set -u
T="$1"
L="$T/lower"; mkdir -p "$L"; echo base > "$L/f"
variant() {   # name upper-fs xino tmpfs-opts
  name="$1"; where="$2"; xino="$3"; topts="$4"
  B="$T/v-$name"; mkdir -p "$B"
  if [ "$where" = tmpfs ]; then
    if ! out=$(mount -t tmpfs -o "size=16m${topts:+,$topts}" tmpfs "$B" 2>&1); then
      echo "$name|mount-tmpfs-failed|$out"; return
    fi
  fi
  mkdir -p "$B/u" "$B/w" "$B/m"
  if ! out=$(mount -t overlay overlay -o "lowerdir=$L,upperdir=$B/u,workdir=$B/w,xino=$xino" "$B/m" 2>&1); then
    echo "$name|mount-overlay-failed|$out"; return
  fi
  if out=$( (echo more >> "$B/m/f") 2>&1 ); then
    echo "$name|ok|copy-up works"
  else
    echo "$name|copy-up-failed|$out"
  fi
}
variant fs-xino-auto fs auto ""
variant fs-xino-off fs off ""
variant tmpfs-xino-auto tmpfs auto ""
variant tmpfs-inode32-xino-auto tmpfs auto inode32
variant tmpfs-xino-off tmpfs off ""
# Control: lower AND upper on one tmpfs mounted in this namespace. If
# this works where the variants above fail, the host-fs lower is the
# trigger, not xino or the tmpfs inode width.
B="$T/v-all-tmpfs"; mkdir -p "$B"
if out=$(mount -t tmpfs -o size=16m tmpfs "$B" 2>&1); then
  mkdir -p "$B/l" "$B/u" "$B/w" "$B/m"; echo base > "$B/l/f"
  if out=$(mount -t overlay overlay -o "lowerdir=$B/l,upperdir=$B/u,workdir=$B/w" "$B/m" 2>&1); then
    if out=$( (echo more >> "$B/m/f") 2>&1 ); then echo "all-tmpfs|ok|copy-up works"
    else echo "all-tmpfs|copy-up-failed|$out"; fi
  else echo "all-tmpfs|mount-overlay-failed|$out"; fi
else echo "all-tmpfs|mount-tmpfs-failed|$out"; fi
"""


def check_overlay(tmp: Path) -> dict:
    """Copy-up under each upper/xino combination, in a private mount
    namespace only."""
    if not IS_LINUX:
        return _na()
    unshare = shutil.which("unshare")
    if not unshare:
        return {"ok": None, "detail": "skipped: unshare(1) not found"}
    mount_ns = ["--mount", "--propagation", "private"]
    userns = [unshare, "--user", "--map-root-user"] + mount_ns
    # A sandbox launcher mounts its overlay inside a user namespace, and
    # that is where overlay behaves differently (the fork's EOVERFLOW),
    # so that run is the one that matters. As real root, also run it
    # without a user namespace for comparison.
    runs = [("userns", userns)]
    if os.geteuid() == 0:
        runs.append(("root", [unshare] + mount_ns))
    variants: dict[str, str] = {}
    skipped: list[str] = []
    for label, base in runs:
        rc, msg = _run(base + ["true"])
        if rc != 0:
            skipped.append(f"{label}: no private mount namespace "
                           f"({msg[:100]})")
            continue
        sub = tmp / label
        sub.mkdir()
        rc, out = _run(base + ["sh", "-c", _OVERLAY_SCRIPT, "sh", str(sub)],
                       timeout=60)
        for line in out.splitlines():
            parts = line.split("|", 2)
            if len(parts) == 3:
                variants[f"{label}/{parts[0]}"] = parts[1] \
                    if parts[1] == "ok" else f"{parts[1]}: {parts[2][:160]}"
    if not variants:
        return {"ok": None, "detail": "skipped: " + "; ".join(skipped)}
    return {"ok": all(v == "ok" for v in variants.values()),
            "variants": variants,
            "detail": ", ".join(f"{k}={'ok' if v == 'ok' else 'FAIL'}"
                                for k, v in variants.items())}


def probe_path(path: Path, *, floor_gib: float, floor_pct: float,
               overlay: bool) -> dict:
    out: dict[str, Any] = {"path": str(path), "fs": _fs_type(path)}
    try:
        out["free"] = check_free(path, floor_gib, floor_pct)
    except OSError as e:
        out["free"] = {"ok": False, "detail": str(e)}
    try:
        tmp = Path(tempfile.mkdtemp(prefix=".sandbox-probe-", dir=path))
    except OSError as e:
        out["ficlone"] = {"ok": None, "detail": f"not writable: {e}"}
        if overlay:
            out["overlay"] = {"ok": None, "detail": "not writable"}
        return out
    try:
        out["ficlone"] = check_ficlone(tmp)
        if overlay:
            out["overlay"] = check_overlay(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out


# ---------------------------------------------------------------------- #
def probe(paths: list[Path], *, floor_gib: float = 5.0,
          floor_pct: float = 5.0, overlay: bool = False) -> dict:
    return {
        "platform": {"system": platform.system(), "release": platform.release(),
                     "machine": platform.machine(),
                     "root": IS_LINUX and os.geteuid() == 0},
        "host": {
            "namespaces": check_namespaces(),
            "cgroup_v2": check_cgroup(),
            "seccomp": check_seccomp(),
            "landlock": check_landlock(),
            "bwrap": check_bwrap(),
            "platform_sandbox": check_platform_sandbox(),
        },
        "paths": [probe_path(p, floor_gib=floor_gib, floor_pct=floor_pct,
                             overlay=overlay) for p in paths],
    }


def _mark(ok: Any) -> str:
    return {True: "OK  ", False: "FAIL", None: "--  "}.get(ok, "?   ")


def render(report: dict) -> str:
    p = report["platform"]
    lines = [f"sandbox probe — {p['system']} {p['release']} {p['machine']}"
             f"{' (root)' if p.get('root') else ''}", ""]
    for name, r in report["host"].items():
        mark = "off " if r.get("optional") and not r.get("ok") \
            else _mark(r.get("ok"))
        lines.append(f"  {mark} {name:<17} {r.get('detail', '')}")
        if name == "namespaces" and "user" in r:
            lines.append("       " + ", ".join(
                f"{k}={r[k]}" for k in ("user", "pid", "net", "mount")))
    for pr in report["paths"]:
        lines += ["", f"  {pr['path']}  [{pr['fs']}]"]
        for key in ("free", "ficlone", "overlay"):
            if key in pr:
                r = pr[key]
                lines.append(f"    {_mark(r.get('ok'))} {key:<8} "
                             f"{r.get('detail', '')}")
                for v, res in (r.get("variants") or {}).items():
                    if res != "ok":
                        lines.append(f"           {v}: {res}")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("paths", nargs="*", help="roots to probe (default: cwd)")
    ap.add_argument("--json", action="store_true", help="JSON output")
    ap.add_argument("--overlay", action="store_true",
                    help="run the overlay copy-up test (private mount "
                         "namespace only)")
    ap.add_argument("--floor-gib", type=float, default=5.0)
    ap.add_argument("--floor-pct", type=float, default=5.0)
    args = ap.parse_args(argv)
    paths = [Path(p) for p in (args.paths or [os.getcwd()])]
    for p in paths:
        if not p.is_dir():
            ap.error(f"not a directory: {p}")
    report = probe(paths, floor_gib=args.floor_gib, floor_pct=args.floor_pct,
                   overlay=args.overlay)
    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
