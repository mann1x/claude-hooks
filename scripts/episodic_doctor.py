#!/usr/bin/env python3
"""Check, and rebuild, episodic-memory's native Node modules.

episodic-memory depends on better-sqlite3, which binds V8 directly
rather than through N-API, so its compiled ``.node`` file works with
exactly one Node ABI. A Node major upgrade therefore kills every CLI
call with

    was compiled against a different Node.js version using
    NODE_MODULE_VERSION 127. This version of Node.js requires
    NODE_MODULE_VERSION 147.   code: 'ERR_DLOPEN_FAILED'

and nothing else notices (see episodic_server/server.py). That happened
on solidpc when Node went 22 -> 26: nothing was indexed for 12 days.

The obvious fix, ``npm rebuild``, fails there in a *different* way,
which is what makes this expensive to rediagnose:

- Node 26's V8 headers include C++20 ``<source_location>``, so the
  compiler must be GCC >= 11. Debian 11 ships 10.2 and apt has nothing
  newer. The conda-forge toolchain (``x86_64-conda-linux-gnu-g++``) in
  any conda env is the fix.
- A module built by conda's GCC 11 links against a libstdc++ newer than
  the system's. It builds cleanly and then fails to *load*. Linking the
  C++ runtime statically (``-static-libstdc++ -static-libgcc``) removes
  the dependency.
- There may be no prebuilt binary for a new ABI (better-sqlite3 12.8.0
  has none for ABI 147), so this cannot be avoided by waiting for one.

Usage:
    scripts/episodic_doctor.py              # check: which modules load
    scripts/episodic_doctor.py --rebuild    # rebuild those that don't
    scripts/episodic_doctor.py --rebuild --force   # rebuild all of them

Exit code: 0 when every native module loads (after the rebuild, with
--rebuild), 1 otherwise.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

MIN_GCC = 11
STATIC_RUNTIME = "-static-libstdc++ -static-libgcc"


def package_root(bin_name: str = "episodic-memory") -> Optional[Path]:
    """The episodic-memory checkout behind the CLI on PATH (the CLI is an
    ``npm link`` symlink into it)."""
    exe = shutil.which(bin_name)
    if not exe:
        return None
    for d in Path(exe).resolve().parents:
        pj = d / "package.json"
        if pj.is_file():
            try:
                if json.loads(pj.read_text(encoding="utf-8")).get("name") == bin_name:
                    return d
            except (OSError, ValueError):
                pass
    return None


def native_modules(root: Path) -> list[Path]:
    """The ``.node`` files compiled on this host (``build/``), which are
    the ones bound to one Node ABI and the ones a rebuild can fix.
    Vendored prebuilts (``bin/napi-v3/<os>/<arch>``, ``vendor/…``) are
    skipped: they are N-API, and most of them are for other platforms and
    never load here by design."""
    out = []
    for p in sorted((root / "node_modules").rglob("*.node")):
        if "build" not in p.parts:
            continue
        if "obj.target" in p.parts or p.name.startswith("test_"):
            continue
        out.append(p)
    return out


def load_error(module: Path, node: str = "node") -> Optional[str]:
    """None when ``node`` can load the module, else its error."""
    r = subprocess.run(
        [node, "-e", f"require({json.dumps(str(module))})"],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode == 0:
        return None
    lines = [ln for ln in r.stderr.splitlines() if ln.strip()]
    for ln in lines:
        if "NODE_MODULE_VERSION" in ln or "GLIBCXX" in ln or "Error:" in ln:
            return ln.strip()
    return lines[-1].strip() if lines else f"exit {r.returncode}"


def module_package(module: Path) -> Path:
    """The npm package directory that owns a built module."""
    for d in module.parents:
        if (d / "package.json").is_file():
            return d
    return module.parent


def gcc_major(cxx: str) -> Optional[int]:
    try:
        r = subprocess.run([cxx, "-dumpversion"], capture_output=True,
                           text=True, timeout=30)
    except OSError:
        return None
    m = re.match(r"(\d+)", r.stdout.strip())
    return int(m.group(1)) if m else None


def conda_cxx_candidates() -> list[str]:
    roots = [os.environ.get("CONDA_PREFIX", "")]
    for base in ("~/anaconda3", "~/miniconda3", "~/miniforge3", "/opt/conda"):
        b = os.path.expanduser(base)
        roots.append(b)
        roots.extend(sorted(glob.glob(os.path.join(b, "envs", "*"))))
    return [c for r in roots if r
            for c in glob.glob(os.path.join(r, "bin", "*-conda-linux-gnu-g++"))]


def pick_compiler() -> Optional[tuple[str, str]]:
    """(CC, CXX) that can compile C++20, preferring an explicit $CXX, then
    the system g++, then a conda-forge toolchain."""
    tried = []
    if os.environ.get("CXX"):
        tried.append((os.environ.get("CC", "gcc"), os.environ["CXX"]))
    tried.append(("gcc", "g++"))
    for cxx in conda_cxx_candidates():
        tried.append((cxx[:-2] + "cc", cxx))
    for cc, cxx in tried:
        major = gcc_major(cxx)
        if major is not None and major >= MIN_GCC:
            return cc, cxx
    return None


def rebuild_env(cc: str, cxx: str, base: Optional[dict] = None) -> dict:
    env = dict(os.environ if base is None else base)
    env["CC"], env["CXX"] = cc, cxx
    # Keep what the caller set; the static runtime is the part that must
    # not be dropped.
    ldflags = env.get("LDFLAGS", "")
    if STATIC_RUNTIME not in ldflags:
        env["LDFLAGS"] = (ldflags + " " + STATIC_RUNTIME).strip()
    return env


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--rebuild", action="store_true",
                    help="rebuild the modules that fail to load")
    ap.add_argument("--force", action="store_true",
                    help="with --rebuild: rebuild every module, loading or not")
    ap.add_argument("--package-dir", help="episodic-memory checkout "
                    "(default: resolved from the CLI on PATH)")
    a = ap.parse_args(argv)

    root = Path(a.package_dir) if a.package_dir else package_root()
    if root is None:
        print("episodic-memory is not on PATH (nothing to check)")
        return 0
    node_v = subprocess.run(["node", "-p", "process.version + ' ABI ' + "
                             "process.versions.modules"],
                            capture_output=True, text=True).stdout.strip()
    print(f"episodic-memory: {root}\nnode: {node_v}")

    broken = {}
    for m in native_modules(root):
        err = load_error(m)
        print(f"  [{'ok' if err is None else 'FAIL':>4}] {m.relative_to(root)}"
              + (f"\n         {err}" if err else ""))
        if err or (a.force and a.rebuild):
            broken.setdefault(module_package(m), []).append(m)
    if not broken:
        return 0
    if not a.rebuild:
        print("\nrun with --rebuild to rebuild these")
        return 1

    if os.name == "nt":
        env = dict(os.environ)
    else:
        picked = pick_compiler()
        if picked is None:
            print(f"\nno C++ compiler >= GCC {MIN_GCC} found (Node >= 26 "
                  f"needs C++20). Install one into a conda env: "
                  f"conda install -c conda-forge gxx_linux-64")
            return 1
        env = rebuild_env(*picked)
        print(f"\ncompiler: {picked[1]} (GCC {gcc_major(picked[1])}), "
              f"LDFLAGS={env['LDFLAGS']}")

    npm = shutil.which("npm") or "npm"
    for pkg in broken:
        print(f"rebuilding {pkg.name} ...", flush=True)
        # npm rebuild runs the package's own install script
        # (prebuild-install, falling back to node-gyp) with npm's
        # bundled node-gyp — so it takes a prebuild when one exists.
        r = subprocess.run([npm, "rebuild", pkg.name], cwd=root, env=env)
        if r.returncode != 0:
            print(f"  npm rebuild {pkg.name} failed (exit {r.returncode})")

    still = [m for ms in broken.values() for m in ms if load_error(m)]
    for m in still:
        print(f"  still broken: {m.relative_to(root)}: {load_error(m)}")
    if still:
        return 1
    stats = subprocess.run(["episodic-memory", "stats"], capture_output=True,
                           text=True, timeout=120)
    print("episodic-memory stats: " + ("ok" if stats.returncode == 0
                                       else f"exit {stats.returncode}"))
    return 0 if stats.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
