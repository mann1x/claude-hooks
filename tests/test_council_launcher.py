"""The council-sandbox launcher: pin, ELF check, verify, install (plan E2).

Unit tests run anywhere (synthetic ELF files, fake fetch / build). The
live class at the bottom runs the real binary when a copy that verifies
against the pin is built or installed; the escape suite in
``test_council_shell_escape.py`` runs every attempt through it too.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from claude_hooks.sandbox import launcher
from claude_hooks.sandbox.launcher import (assert_static, elf_info, install,
                                           verify)
from claude_hooks.sandbox.spec import RunSpec

REPO = Path(__file__).resolve().parent.parent


def _elf(path: Path, *, etype=2, machine=62, phdrs=(1,)) -> Path:
    """A minimal 64-bit LE ELF header + program headers of ``phdrs`` types."""
    hdr = bytearray(64)
    hdr[:4] = b"\x7fELF"
    hdr[4], hdr[5], hdr[6] = 2, 1, 1
    struct.pack_into("<HHI", hdr, 16, etype, machine, 1)
    struct.pack_into("<Q", hdr, 32, 64)
    struct.pack_into("<HHH", hdr, 52, 64, 56, len(phdrs))
    ph = b"".join(struct.pack("<I", t) + bytes(52) for t in phdrs)
    path.write_bytes(bytes(hdr) + ph + b"payload")
    os.chmod(path, 0o755)
    return path


def _pin_for(p: Path, arch="x86_64") -> dict:
    return {"version": "9.9.9", "toolchain": "1.89.0", "source_sha256": "x",
            "assets": {arch: hashlib.sha256(p.read_bytes()).hexdigest()}}


@pytest.fixture
def host_x86(monkeypatch):
    monkeypatch.setattr(launcher.platform, "machine", lambda: "x86_64")


# ------------------------------------------------------------------ pin
class TestPin:
    def test_the_pin_covers_the_current_source(self):
        """Editing the launcher without re-pinning must fail here — the
        pin is what deploy and the engine trust."""
        if not (launcher.SOURCE / "Cargo.toml").is_file():
            pytest.skip("no launcher source in this checkout")
        assert launcher.load_pin()["source_sha256"] == launcher.source_digest(), \
            "run scripts/build_council_sandbox.py --pin"

    def test_the_pin_names_both_arches_and_the_toolchain(self):
        pin = launcher.load_pin()
        assert set(pin["assets"]) == {"x86_64", "aarch64"}
        assert all(re.fullmatch(r"[0-9a-f]{64}", v) for v in pin["assets"].values())
        chan = re.search(r'channel\s*=\s*"([^"]+)"',
                         (launcher.SOURCE / "rust-toolchain.toml").read_text())
        assert chan and pin["toolchain"] == chan.group(1)

    def test_the_pin_ships_with_the_package(self):
        assert '"claude_hooks.sandbox" = ["launcher_pin.json"]' in \
            (REPO / "pyproject.toml").read_text()

    def test_the_crate_has_no_dependencies(self):
        """The engine runs it as root: everything it is made of is here."""
        lock = (launcher.SOURCE / "Cargo.lock").read_text()
        assert lock.count("[[package]]") == 1


# ------------------------------------------------------------------ ELF
class TestElf:
    def test_a_plain_static_executable_passes(self, tmp_path):
        assert_static(_elf(tmp_path / "b"), "x86_64")

    @pytest.mark.parametrize("kw,why", [
        ({"phdrs": (1, 3)}, "interpreter"),
        ({"phdrs": (1, 2)}, "dynamic segment"),
        ({"etype": 3}, "not a plain executable"),
        ({"machine": 183}, "built for aarch64"),
    ])
    def test_what_is_not_static_is_refused(self, tmp_path, kw, why):
        with pytest.raises(ValueError, match=why):
            assert_static(_elf(tmp_path / "b", **kw), "x86_64")

    def test_not_an_elf(self, tmp_path):
        (tmp_path / "s").write_text("#!/bin/sh\n")
        with pytest.raises(ValueError, match="not an ELF"):
            elf_info(tmp_path / "s")

    def test_the_real_build_is_static(self):
        a = launcher.arch()
        out = launcher.SOURCE / "target" / launcher.target(a or "") / "release" / launcher.NAME
        if not out.is_file():
            pytest.skip("launcher not built")
        assert_static(out, a)


# --------------------------------------------------------------- verify
class TestVerify:
    def test_the_pinned_binary_verifies(self, tmp_path, host_x86):
        os.chmod(tmp_path, 0o755)
        b = _elf(tmp_path / "cs")
        chk = verify(b, pin=_pin_for(b))
        assert chk.ok, chk.reason

    def test_not_installed(self, tmp_path, host_x86):
        chk = verify(tmp_path / "nope", pin=_pin_for(_elf(tmp_path / "x")))
        assert not chk.ok and chk.reason == "not installed"

    def test_a_hash_mismatch_is_refused(self, tmp_path, host_x86):
        os.chmod(tmp_path, 0o755)
        b = _elf(tmp_path / "cs")
        pin = _pin_for(b)
        b.write_bytes(b.read_bytes() + b"tampered")
        chk = verify(b, pin=pin)
        assert not chk.ok and "is not the pinned" in chk.reason

    def test_a_group_writable_binary_is_refused(self, tmp_path, host_x86):
        os.chmod(tmp_path, 0o755)
        b = _elf(tmp_path / "cs")
        os.chmod(b, 0o775)
        assert "writable by group" in verify(b, pin=_pin_for(b)).reason

    def test_a_writable_directory_is_refused(self, tmp_path, host_x86):
        """Whoever can write the directory can swap the binary."""
        b = _elf(tmp_path / "cs")
        os.chmod(tmp_path, 0o777)
        try:
            assert "could be swapped" in verify(b, pin=_pin_for(b)).reason
        finally:
            os.chmod(tmp_path, 0o755)

    def test_a_symlink_is_refused(self, tmp_path, host_x86):
        os.chmod(tmp_path, 0o755)
        b = _elf(tmp_path / "cs")
        (tmp_path / "link").symlink_to(b)
        assert "not a regular file" in verify(tmp_path / "link", pin=_pin_for(b)).reason

    def test_a_dynamic_binary_matching_the_pin_is_still_refused(self, tmp_path, host_x86):
        os.chmod(tmp_path, 0o755)
        b = _elf(tmp_path / "cs", phdrs=(1, 3))
        assert "not static" in verify(b, pin=_pin_for(b)).reason

    def test_an_unpinned_arch(self, tmp_path, monkeypatch):
        monkeypatch.setattr(launcher.platform, "machine", lambda: "riscv64")
        assert not verify(tmp_path / "x", pin={"assets": {}}).ok


# -------------------------------------------------------------- install
class TestInstall:
    def _setup(self, tmp_path):
        os.chmod(tmp_path, 0o755)
        good = _elf(tmp_path / "good")
        return good, _pin_for(good), tmp_path / "inst" / launcher.NAME

    def test_a_matching_local_build_is_installed(self, tmp_path, host_x86):
        good, pin, dst = self._setup(tmp_path)
        notes = []
        st = install(path=dst, pin=pin, note=notes.append,
                     builder=lambda a: good,
                     fetch=lambda u, d: pytest.fail("must not download"))
        assert st == "installed" and verify(dst, pin=pin).ok
        assert dst.read_bytes() == good.read_bytes()
        assert install(path=dst, pin=pin, note=notes.append) == "current"

    def test_a_mismatched_build_falls_back_to_the_asset(self, tmp_path, host_x86):
        good, pin, dst = self._setup(tmp_path)
        other = _elf(tmp_path / "other", phdrs=(1, 1))
        urls = []

        def fetch(url, d):
            urls.append(url)
            d.write_bytes(good.read_bytes())
        notes = []
        st = install(path=dst, pin=pin, note=notes.append,
                     builder=lambda a: other, fetch=fetch)
        assert st == "installed"
        assert urls == ["https://github.com/mann1x/claude-hooks/releases/download/"
                        "council-sandbox-v9.9.9/council-sandbox-x86_64"]
        assert any("is not the pin" in n for n in notes)

    def test_an_asset_not_matching_the_pin_is_never_installed(self, tmp_path, host_x86):
        good, pin, dst = self._setup(tmp_path)

        def no_build(a):
            raise RuntimeError("cargo not found")
        st = install(path=dst, pin=pin, note=lambda m: None, builder=no_build,
                     fetch=lambda u, d: d.write_bytes(b"\x7fELF evil"))
        assert st == "failed" and not dst.exists()

    def test_nothing_obtainable_is_unavailable(self, tmp_path, host_x86):
        good, pin, dst = self._setup(tmp_path)

        def no_build(a):
            raise RuntimeError("cargo not found")

        def offline(u, d):
            raise OSError("network unreachable")
        assert install(path=dst, pin=pin, note=lambda m: None, builder=no_build,
                       fetch=offline) == "unavailable"
        assert not dst.exists()

    def test_dry_run_changes_nothing(self, tmp_path, host_x86):
        good, pin, dst = self._setup(tmp_path)
        assert install(dry=True, path=dst, pin=pin, note=lambda m: None,
                       builder=lambda a: pytest.fail("built in a dry run")) == "current"
        assert not dst.exists()

    def test_a_stale_copy_is_replaced(self, tmp_path, host_x86):
        good, pin, dst = self._setup(tmp_path)
        dst.parent.mkdir()
        os.chmod(dst.parent, 0o755)
        _elf(dst, phdrs=(1, 1, 1))
        assert install(path=dst, pin=pin, note=lambda m: None,
                       builder=lambda a: good) == "installed"
        assert verify(dst, pin=pin).ok


# ---------------------------------------------------------------- chain
def _spec(**kw) -> RunSpec:
    base = dict(argv=["true"], cwd="/", ro_roots=(), scratch="/tmp/s", timeout_s=5)
    base.update(kw)
    return RunSpec(**base)


class TestChain:
    def test_the_launcher_replaces_prlimit_setpriv_and_bwrap(self):
        from claude_hooks.sandbox.linux_bwrap import bwrap_args, chain
        spec = _spec()
        argv = chain(spec, 5, unit="u", root=True, launcher="/opt/cs")
        assert "prlimit" not in argv and "setpriv" not in argv and "bwrap" not in argv
        i = argv.index("/opt/cs")
        assert argv[i + 1:i + 3] == ["--drop-to", "65534:65534"]
        assert "core=0:0" in argv and "nofile=4096:4096" in argv
        # one mount plan for both backends
        assert argv[-len(bwrap_args(spec, 5)) + 1:] == bwrap_args(spec, 5)[1:]

    def test_no_drop_when_not_root(self):
        from claude_hooks.sandbox.linux_bwrap import chain
        argv = chain(_spec(), None, unit="u", root=False, launcher="/opt/cs")
        assert "--drop-to" not in argv and argv[1] == "--user"

    def test_auto_takes_the_launcher_only_when_it_verifies(self, monkeypatch):
        if not sys.platform.startswith("linux"):
            pytest.skip("linux backends")
        import claude_hooks.sandbox as sb
        from claude_hooks.sandbox.linux_bwrap import BwrapBackend, LauncherBackend
        monkeypatch.setattr(launcher, "verify",
                            lambda *a, **k: launcher.Check(True, Path("/x")))
        assert isinstance(sb.backend_for("auto"), LauncherBackend)
        monkeypatch.setattr(launcher, "verify",
                            lambda *a, **k: launcher.Check(False, Path("/x"), "no"))
        be = sb.backend_for("auto")
        assert isinstance(be, BwrapBackend) and not isinstance(be, LauncherBackend)
        assert isinstance(sb.backend_for("launcher"), LauncherBackend)

    def test_a_launcher_that_does_not_verify_means_no_shell(self, monkeypatch):
        from claude_hooks.sandbox.linux_bwrap import LauncherBackend
        monkeypatch.setattr(launcher, "verify",
                            lambda *a, **k: launcher.Check(False, Path("/x"), "sha256 bad"))
        pre = LauncherBackend("/x").preflight(refresh=True)
        assert not pre.ok and pre.missing[0] == "launcher"
        assert "sha256 bad" in pre.reason()

    def test_its_setup_failure_is_not_the_commands_exit(self, tmp_path):
        if not sys.platform.startswith("linux"):
            pytest.skip("linux backends")
        from claude_hooks.sandbox.linux_bwrap import LauncherBackend
        fake = tmp_path / "cs"
        fake.write_text("#!/bin/sh\necho 'council-sandbox: bind source /x is "
                        "not reachable' >&2\nexit 1\n")
        os.chmod(fake, 0o755)
        r = LauncherBackend(str(fake))._run(_spec(scratch=str(tmp_path)), use_scope=False)
        assert r.killed_by == "setup" and r.exit_code is None
        assert "not reachable" in r.setup_error

    def test_the_provider_asks_the_backend_for_the_uid(self):
        """Keyed on the backend's name, the launcher's scratch stayed
        root-owned and every write to /scratch was refused."""
        from claude_hooks.tool_registry.shell_provider import (ShellSettings,
                                                               ShellToolProvider)
        p = ShellToolProvider(sid="s", settings=ShellSettings())

        class Be:
            name = "anything"

            def sandbox_uid(self):
                return 4242
        p._backend = Be()
        assert p._sandbox_uid() == 4242
        p._backend = type("NoUid", (), {"name": "bwrap"})()
        assert p._sandbox_uid() is None


# ----------------------------------------------------------------- live
def _binary():
    # Mutation checks only: run an unpinned build (never set otherwise).
    if os.environ.get("COUNCIL_SANDBOX_UNPINNED"):
        return os.environ["COUNCIL_SANDBOX_UNPINNED"]
    a = launcher.arch()
    built = launcher.SOURCE / "target" / launcher.target(a or "") / "release" / launcher.NAME
    for c in (launcher.installed_path(), built):
        if verify(c).ok:
            return str(c)
    return None


BIN = _binary() if sys.platform.startswith("linux") else None
ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
DROP = ["--drop-to", "65534:65534"] if ROOT else []
BASE = ["--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin",
        "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
        "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--remount-ro", "/"]


def _plan(*extra):
    """BASE with ``extra`` ops placed before its final ``--remount-ro /``."""
    return [*BASE[:-2], *extra, *BASE[-2:]]


def _cs(*args, **kw):
    return subprocess.run([BIN, *args], capture_output=True, text=True,
                          timeout=30, **kw)


@pytest.mark.skipif(BIN is None, reason="no council-sandbox that verifies "
                    "against the pin (scripts/build_council_sandbox.py)")
class TestLiveLauncher:
    def test_version(self):
        r = _cs("--version")
        assert r.returncode == 0
        assert r.stdout.strip() == f"council-sandbox {launcher.load_pin()['version']}"

    @pytest.mark.skipif(not ROOT, reason="root only")
    def test_root_without_a_drop_is_refused(self):
        r = _cs(*BASE, "--", "/bin/true")
        assert r.returncode == 1
        assert r.stderr.startswith("council-sandbox: refusing to run a command as root")

    def test_exit_codes_and_signals_come_back(self):
        assert _cs(*DROP, *BASE, "--", "/bin/sh", "-c", "exit 7").returncode == 7
        assert _cs(*DROP, *BASE, "--", "/bin/sh", "-c", "kill -9 $$").returncode == 137

    def test_a_missing_command_is_127_not_a_setup_failure(self):
        r = _cs(*DROP, *BASE, "--", "/nonexistent")
        assert r.returncode == 127 and "exec /nonexistent" in r.stderr

    def test_a_missing_bind_source_is_a_setup_failure(self):
        r = _cs(*DROP, *_plan("--ro-bind", "/nonexistent-src", "/x"), "--", "/bin/true")
        assert r.returncode == 1
        assert r.stderr.startswith("council-sandbox: bind source /nonexistent-src")

    def test_an_absolute_symlink_source_resolves_on_the_host(self):
        """resolv.conf is often a link to /run/…; canonicalize() would
        have followed it against the private tmpfs and lost it."""
        import shutil
        import tempfile
        d = Path(tempfile.mkdtemp(prefix="cs-link-", dir="/tmp"))
        try:
            os.chmod(d, 0o755)
            (d / "real").write_text("host-file\n")
            os.chmod(d / "real", 0o644)
            (d / "link").symlink_to(d / "real")
            r = _cs(*DROP, *_plan("--ro-bind", str(d / "link"), "/etc/x"), "--",
                    "/bin/cat", "/etc/x")
            assert r.stdout == "host-file\n", r.stderr
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_nothing_is_mounted_in_the_host_namespace(self):
        before = Path("/proc/self/mountinfo").read_text()
        r = _cs(*DROP, *_plan("--tmpfs", "/scratch"), "--", "/bin/sh", "-c",
                "touch /scratch/x && echo MADE")
        assert r.stdout == "MADE\n", r.stderr
        assert Path("/proc/self/mountinfo").read_text() == before

    def test_the_jail_is_what_the_design_says(self):
        r = _cs(*DROP, *BASE, "--", "/bin/sh", "-c",
                "hostname; echo $$; grep -E '^(CapEff|CapBnd|NoNewPrivs)' "
                "/proc/self/status; grep -c : /proc/net/dev; ls /")
        lines = r.stdout.strip().splitlines()
        assert lines[0] == "council"
        assert lines[1] == "2"                       # pid 1 is the launcher
        status = dict(ln.split(":\t") for ln in lines[2:5])
        assert status == {"CapEff": "0000000000000000",
                          "CapBnd": "0000000000000000", "NoNewPrivs": "1"}
        assert lines[5] == "1"                       # only lo
        assert sorted(lines[6:]) == ["bin", "dev", "lib", "lib64", "proc",
                                     "tmp", "usr"]

    def test_unknown_options_are_refused(self):
        r = _cs("--share-net", "--", "/bin/true")
        assert r.returncode == 1 and "unknown option" in r.stderr


def test_json_pin_is_valid_json():
    json.loads(launcher.PIN.read_text())
