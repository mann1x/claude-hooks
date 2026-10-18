"""The ``council-sandbox`` launcher: pin, install, verify (plan E2).

The launcher (``native/council-sandbox``, Rust, no dependencies) is a
static binary the engine runs as root, so what is installed must be
exactly what the repo pins:

* :data:`PIN` (``launcher_pin.json``) names the version, the toolchain,
  a digest of the source and the sha256 of the binary for each
  architecture. The build is reproducible from the pinned toolchain, so
  a host may install its *own* build when it hashes to the pin, and
  otherwise the release asset, checked against the same hash.
* :func:`assert_static` reads the ELF: no interpreter, no dynamic
  segment. The fork's launcher needed glibc 2.34 and never started on
  bullseye's 2.31; a static binary has no loader to be wrong about.
* :func:`verify` is what the engine checks before using one: a regular
  file owned by us, writable by nobody else (nor its directory), static,
  and matching the pin. Anything else means no launcher — the backend
  falls back to bwrap or says why there is no shell.

``scripts/build_council_sandbox.py`` builds and re-pins;
``scripts/deploy.py`` (``step_sandbox``) calls :func:`install`;
``scripts/verify_deploy.py`` (``check_sandbox``) runs
:func:`escape_smoke` through the installed launcher.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import stat
import struct
import subprocess
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

NAME = "council-sandbox"
PIN = Path(__file__).with_name("launcher_pin.json")
#: The crate, in a repo checkout (absent from a wheel).
SOURCE = Path(__file__).resolve().parents[2] / "native" / NAME
#: Files whose bytes make the binary; the digest covers exactly these.
SOURCE_FILES = ("Cargo.toml", "Cargo.lock", "rust-toolchain.toml",
                ".cargo/config.toml")
RELEASE_URL = ("https://github.com/mann1x/claude-hooks/releases/download/"
               "council-sandbox-v{version}/council-sandbox-{arch}")
ENV_PATH = "CLAUDE_HOOKS_SANDBOX_LAUNCHER"
ARCHES = {"x86_64": "x86_64", "amd64": "x86_64",
          "aarch64": "aarch64", "arm64": "aarch64"}


def arch(machine: Optional[str] = None) -> Optional[str]:
    return ARCHES.get((machine or platform.machine()).lower())


def target(a: str) -> str:
    return f"{a}-unknown-linux-musl"


def load_pin(path: Path = PIN) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def installed_path() -> Path:
    """Where the launcher lives on this host (``$CLAUDE_HOOKS_SANDBOX_LAUNCHER``
    overrides it). Small and per-host, so under ``~/.claude``."""
    env = os.environ.get(ENV_PATH)
    if env:
        return Path(env)
    return Path.home() / ".claude" / "claude-hooks-sandbox" / NAME


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def source_digest(src: Path = SOURCE) -> str:
    """sha256 over the crate's inputs (paths and bytes, sorted), so a
    source change that was not re-pinned is caught by the test suite."""
    files = [src / f for f in SOURCE_FILES]
    files += sorted((src / "src").rglob("*.rs"))
    h = hashlib.sha256()
    for f in files:
        rel = f.relative_to(src).as_posix()
        h.update(rel.encode() + b"\0")
        h.update(f.read_bytes() + b"\0")
    return h.hexdigest()


# --------------------------------------------------------------------- #
# ELF
# --------------------------------------------------------------------- #
PT_DYNAMIC, PT_INTERP = 2, 3
ET_EXEC = 2
E_MACHINE = {62: "x86_64", 183: "aarch64"}


def elf_info(p: Path) -> dict:
    """``{arch, type, interp, dynamic}`` of a 64-bit little-endian ELF."""
    with open(p, "rb") as f:
        hdr = f.read(64)
        if len(hdr) < 64 or hdr[:4] != b"\x7fELF":
            raise ValueError("not an ELF file")
        if hdr[4] != 2 or hdr[5] != 1:
            raise ValueError("not a 64-bit little-endian ELF")
        e_type, e_machine = struct.unpack_from("<HH", hdr, 16)
        e_phoff, = struct.unpack_from("<Q", hdr, 32)
        e_phentsize, e_phnum = struct.unpack_from("<HH", hdr, 54)
        f.seek(e_phoff)
        ph = f.read(e_phentsize * e_phnum)
    types = {struct.unpack_from("<I", ph, i * e_phentsize)[0]
             for i in range(e_phnum)}
    return {"arch": E_MACHINE.get(e_machine, f"machine {e_machine}"),
            "type": e_type, "interp": PT_INTERP in types,
            "dynamic": PT_DYNAMIC in types}


def assert_static(p: Path, want_arch: Optional[str] = None) -> None:
    """Raise ``ValueError`` unless ``p`` is a static, non-PIE executable
    (for ``want_arch`` when given)."""
    info = elf_info(p)
    if info["interp"]:
        raise ValueError("has an ELF interpreter (dynamically linked)")
    if info["dynamic"]:
        raise ValueError("has a dynamic segment (static-pie or shared libs)")
    if info["type"] != ET_EXEC:
        raise ValueError(f"ELF type {info['type']} is not a plain executable")
    if want_arch and info["arch"] != want_arch:
        raise ValueError(f"built for {info['arch']}, this host is {want_arch}")


# --------------------------------------------------------------------- #
# verify / build / install
# --------------------------------------------------------------------- #
@dataclass
class Check:
    ok: bool
    path: Path
    reason: str = ""
    sha256: str = ""


def _owner_ok(st: os.stat_result) -> bool:
    return st.st_uid in (0, os.geteuid())


def verify(path: Optional[Path] = None, *, pin: Optional[dict] = None) -> Check:
    """Whether ``path`` is the pinned launcher and safe to run as us."""
    path = Path(path or installed_path())
    a = arch()
    if a is None:
        return Check(False, path, f"no launcher for {platform.machine()}")
    try:
        pin = pin or load_pin()
    except (OSError, ValueError) as e:
        return Check(False, path, f"pin unreadable: {e}")
    want = (pin.get("assets") or {}).get(a)
    if not want:
        return Check(False, path, f"no {a} asset pinned")
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return Check(False, path, "not installed")
    except OSError as e:
        return Check(False, path, f"unreadable: {e}")
    if not stat.S_ISREG(st.st_mode):
        return Check(False, path, "not a regular file")
    if not _owner_ok(st) or st.st_mode & 0o022:
        return Check(False, path, "owned by another user or writable by "
                     "group/others (the engine runs it as root)")
    try:
        dst = os.stat(path.parent)
    except OSError as e:
        return Check(False, path, f"directory unreadable: {e}")
    if not _owner_ok(dst) or dst.st_mode & 0o022:
        return Check(False, path, f"{path.parent} is writable by "
                     "group/others; the binary could be swapped")
    digest = sha256_file(path)
    if digest != want:
        return Check(False, path, f"sha256 {digest[:12]}… is not the pinned "
                     f"{want[:12]}… (v{pin.get('version')})", digest)
    try:
        assert_static(path, a)
    except (OSError, ValueError) as e:
        return Check(False, path, f"not static: {e}", digest)
    return Check(True, path, f"v{pin.get('version')} pinned", digest)


def cargo() -> Optional[str]:
    c = shutil.which("cargo")
    if c:
        return c
    home = Path.home() / ".cargo" / "bin" / "cargo"
    return str(home) if home.is_file() else None


def build(a: str, *, src: Path = SOURCE, timeout: float = 900) -> Path:
    """Build the release binary for ``a``; raise ``RuntimeError`` on
    failure. The pinned toolchain comes from ``rust-toolchain.toml``."""
    c = cargo()
    if not c:
        raise RuntimeError("cargo not found")
    if not (src / "Cargo.toml").is_file():
        raise RuntimeError(f"no launcher source at {src}")
    r = subprocess.run([c, "build", "--release", "--locked", "--target",
                        target(a)], cwd=src, capture_output=True, text=True,
                       timeout=timeout)
    if r.returncode != 0:
        tail = (r.stderr or r.stdout).strip().splitlines()[-3:]
        raise RuntimeError("cargo build failed: " + " | ".join(tail))
    out = src / "target" / target(a) / "release" / NAME
    assert_static(out, a)
    return out


def _download(url: str, dst: Path, timeout: float = 60) -> None:
    with urllib.request.urlopen(url, timeout=timeout) as r, open(dst, "wb") as f:
        shutil.copyfileobj(r, f)


def _place(src: Path, path: Path, want: str) -> None:
    """Copy ``src`` to ``path`` atomically, checking the hash on the copy."""
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o755)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{NAME}.")
    try:
        with os.fdopen(fd, "wb") as out, open(src, "rb") as inp:
            shutil.copyfileobj(inp, out)
        if sha256_file(Path(tmp)) != want:
            raise RuntimeError("copy does not match the pin")
        os.chmod(tmp, 0o755)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def install(*, dry: bool = False, note: Callable[[str], None] = print,
            path: Optional[Path] = None, pin: Optional[dict] = None,
            fetch: Callable[[str, Path], None] = _download,
            builder: Callable[[str], Path] = build) -> str:
    """Make the installed launcher the pinned one.

    Returns ``current`` / ``installed`` (done), ``unavailable`` (nothing
    pinned for this arch, or neither a matching local build nor the asset
    could be had — the shell falls back to bwrap) or ``failed`` (an
    artifact was found and did not match: never installed).
    """
    path = Path(path or installed_path())
    pin = pin or load_pin()
    a = arch()
    want = (pin.get("assets") or {}).get(a or "")
    if not want:
        note(f"{NAME}: no asset pinned for {platform.machine()}")
        return "unavailable"
    now = verify(path, pin=pin)
    if now.ok:
        note(f"{NAME} v{pin['version']} current ({path})")
        return "current"
    note(f"{NAME}: installed copy {now.reason}")
    if dry:
        note(f"[dry-run] would install {NAME} v{pin['version']} to {path}")
        return "current"
    mismatch = ""
    try:
        built = builder(a)
        got = sha256_file(built)
        if got == want:
            _place(built, path, want)
            note(f"{NAME} v{pin['version']} installed from a local build "
                 f"matching the pin ({path})")
            return "installed" if verify(path, pin=pin).ok else "failed"
        mismatch = (f"local build sha256 {got[:12]}… is not the pin "
                    f"{want[:12]}… (toolchain other than "
                    f"{pin.get('toolchain')}?)")
        note(f"{NAME}: {mismatch}; trying the release asset")
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as e:
        note(f"{NAME}: no local build ({e}); trying the release asset")
    url = RELEASE_URL.format(version=pin["version"], arch=a)
    with tempfile.TemporaryDirectory(prefix="council-sandbox-") as d:
        dl = Path(d) / NAME
        try:
            fetch(url, dl)
        except Exception as e:  # network, 404: nothing to install
            note(f"{NAME}: release asset unavailable ({url}: {e})")
            return "unavailable"
        got = sha256_file(dl)
        if got != want:
            note(f"{NAME}: release asset sha256 {got[:12]}… does NOT match "
                 f"the pin {want[:12]}… — not installed")
            return "failed"
        try:
            assert_static(dl, a)
        except ValueError as e:
            note(f"{NAME}: release asset is not static ({e}) — not installed")
            return "failed"
        _place(dl, path, want)
    note(f"{NAME} v{pin['version']} installed from the release asset ({path})")
    return "installed" if verify(path, pin=pin).ok else "failed"


# --------------------------------------------------------------------- #
# escape smoke (verify_deploy)
# --------------------------------------------------------------------- #
#: name -> shell test that must succeed inside the jail.
SMOKE = (
    ("not root", '[ "$(id -u)" != 0 ]'),
    ("no /root", "[ ! -e /root ]"),
    ("root fs read-only", "! touch /x 2>/dev/null"),
    ("/usr read-only", "! touch /usr/.council-x 2>/dev/null"),
    ("/etc filtered", "[ ! -e /etc/shadow ]"),
    ("loopback only", "[ \"$(grep -c : /proc/net/dev)\" = 1 ]"),
    # The caller's pid must be invisible; the jail's own pipeline (pid 1,
    # sh, the $( ) subshell, ls, grep) is five processes already.
    ("own pid namespace", "[ ! -e /proc/{caller} ] && "
     "[ \"$(ls /proc | grep -c '^[0-9]')\" -lt 10 ]"),
    ("no new namespaces", "! unshare -U true 2>/dev/null"),
    ("no capabilities", "grep -q '^CapEff:[[:space:]]*0*$' /proc/self/status"),
    ("no_new_privs", "grep -q '^NoNewPrivs:[[:space:]]*1' /proc/self/status"),
    ("scratch writable", "touch /scratch/.smoke && rm /scratch/.smoke"),
)


def escape_smoke(backend) -> list[str]:
    """Run :data:`SMOKE` through ``backend``; return what failed
    (empty = every escape attempt was refused)."""
    from claude_hooks.sandbox.linux_bwrap import NOBODY, is_root
    from claude_hooks.sandbox.spec import Limits, RunSpec
    script = "\n".join(
        f'if {t.replace("{caller}", str(os.getpid()))}; then echo "ok {n}"; '
        f'else echo "FAIL {n}"; fi' for n, t in SMOKE)
    scratch = tempfile.mkdtemp(prefix="council-smoke-")
    try:
        if is_root():
            os.chown(scratch, NOBODY, NOBODY)
        r = backend.run(RunSpec(argv=["/bin/sh", "-c", script], cwd="/",
                                ro_roots=(), scratch=scratch, timeout_s=60,
                                limits=Limits(memory_mib=256, pids=64),
                                name="smoke"))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if r.killed_by != "none" or r.exit_code != 0:
        return [f"smoke did not run: {r.killed_by} {r.setup_error or r.stderr[:200]}"]
    seen = {ln.split(" ", 1)[1]: ln.split(" ", 1)[0]
            for ln in r.stdout.splitlines() if " " in ln}
    return [n for n, _ in SMOKE if seen.get(n) != "ok"]


__all__ = ["NAME", "PIN", "SOURCE", "arch", "assert_static", "build",
           "elf_info", "escape_smoke", "install", "installed_path",
           "load_pin", "sha256_file", "source_digest", "verify", "Check"]
