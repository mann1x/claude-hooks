"""The host's toolchains inside the council sandbox (mail #1625: node v12
and no bun in the jail, so no lane could run vitest)."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

if not sys.platform.startswith("linux"):
    pytest.skip("the Linux sandbox", allow_module_level=True)

from claude_hooks.sandbox import toolchains as T  # noqa: E402
from claude_hooks.sandbox.linux_bwrap import sandbox_env  # noqa: E402


def _tool(d, name, body="#!/bin/sh\necho hi\n"):
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(body)
    p.chmod(0o755)
    return p


@pytest.fixture
def host(monkeypatch):
    # Not tmp_path: pytest's base (/tmp/pytest-of-root) is 0700, and the
    # mirror rightly refuses a root others cannot reach. A host's real root
    # (/var/cache/...) is reachable; so is this one, made here.
    root = Path(tempfile.mkdtemp(prefix="tc-test-", dir="/tmp"))
    os.chmod(root, 0o755)
    monkeypatch.setenv("CLAUDE_HOOKS_SANDBOX_TOOLCHAINS", str(root / "mirror"))
    monkeypatch.setattr(T, "_CACHE", {})
    monkeypatch.setattr(T, "_homes", lambda: {str(root / "home")})
    monkeypatch.setattr(T, "HOME_BIN_DIRS", ())
    (root / "home").mkdir()
    yield root
    for dp, dns, _fns in os.walk(root):
        for d in dns:
            os.chmod(os.path.join(dp, d), 0o755)
    shutil.rmtree(root, ignore_errors=True)


def test_a_prefix_others_cannot_reach_is_mirrored(host, monkeypatch):
    private = host / "home" / ".hermes"
    _tool(private / "node" / "bin", "node")
    (private / "node" / "lib").mkdir()
    os.chmod(private, 0o700)
    link = host / "usrlocal"
    link.mkdir()
    os.symlink(private / "node" / "bin" / "node", link / "node")
    monkeypatch.setattr(T, "TOOLS", ("node",))
    tc = T.discover(path=str(link))
    assert len(tc.binds) == 1 and tc.binds[0].startswith(str(host / "mirror"))
    assert tc.bins == [os.path.join(tc.binds[0], "bin")]
    assert os.path.isdir(os.path.join(tc.binds[0], "lib"))
    assert T.reachable_by_others(tc.found["node"])
    # The same source maps to the same mirror; it is not copied again.
    monkeypatch.setattr(T, "_CACHE", {})
    assert T.discover(path=str(link)).binds == tc.binds


def test_a_reachable_bin_dir_is_bound_in_place(host, monkeypatch):
    b = _tool(host / "home" / ".bun" / "bin", "bun").parent
    monkeypatch.setattr(T, "TOOLS", ("bun",))
    tc = T.discover(path=str(b))
    assert tc.binds == [str(b)] and tc.bins == [str(b)]
    assert not (host / "mirror").exists()


def test_system_tools_proxies_and_homes_are_left_alone(host, monkeypatch):
    rustup = _tool(host / "opt" / "rust" / "bin", "rustup")
    proxies = host / "proxies"
    proxies.mkdir()
    os.symlink(rustup, proxies / "deno")
    _tool(host / "home", "go")      # a binary directly in a home
    monkeypatch.setattr(T, "TOOLS", ("deno", "go"))
    tc = T.discover(path=f"{proxies}:{host / 'home'}")
    assert tc.binds == [] and set(tc.skipped) == {"deno", "go"}
    monkeypatch.setattr(T, "TOOLS", ("sh",))
    assert T.discover(path="/bin:/usr/bin").binds == []   # already visible


def test_bin_dirs_go_on_path_as_themselves_and_prefixes_by_their_bin(host):
    from claude_hooks.sandbox import Limits, RunSpec
    b = _tool(host / "home" / ".bun" / "bin", "bun").parent
    pre = host / "mirror" / "node-x"
    _tool(pre / "bin", "node")
    spec = RunSpec(argv=["true"], cwd="/", ro_roots=(), scratch=str(host),
                   timeout_s=1, limits=Limits(), ro_binds=(str(b), str(pre)))
    path = sandbox_env(spec)["PATH"].split(":")
    assert path[:2] == [str(b), str(pre / "bin")]
    assert path.index(str(pre / "bin")) < path.index("/usr/bin")


def test_the_tool_text_names_them_and_the_vite_recipe():
    from claude_hooks.tool_registry.shell_provider import ShellSettings, _spec
    d = _spec(ShellSettings(), {"node": "/x/bin/node"})["function"]["description"]
    assert "first on PATH: node (/x/bin/node)" in d
    assert "--configLoader runner" in d
    assert "first on PATH" not in _spec(ShellSettings())["function"]["description"]


def test_the_mirror_never_changes_a_directory_it_did_not_make(host,
                                                               monkeypatch):
    """2026-10-24: the mirror set every ancestor of its root to 0755, so a
    root under /tmp reset /tmp itself from 1777 and broke every non-root
    service on the host (auditd: this test suite, twice per run)."""
    shared = host / "shared-tmp"           # stands in for /tmp
    shared.mkdir()
    os.chmod(shared, 0o1777)
    root = shared / "council-tcm" / "m"
    monkeypatch.setenv("CLAUDE_HOOKS_SANDBOX_TOOLCHAINS", str(root))
    T._make_reachable(root)
    assert (os.stat(shared).st_mode & 0o7777) == 0o1777
    assert (os.stat(host).st_mode & 0o7777) == 0o755  # the fixture's own
    for made in (root, root.parent):
        assert (os.stat(made).st_mode & 0o7777) == 0o755
    # Through a real mirror too.
    private = host / "home" / ".hermes"
    _tool(private / "node" / "bin", "node")
    os.chmod(private, 0o700)
    monkeypatch.setattr(T, "TOOLS", ("node",))
    tc = T.discover(path=str(private / "node" / "bin"))
    assert tc.binds and tc.binds[0].startswith(str(root))
    assert (os.stat(shared).st_mode & 0o7777) == 0o1777
    assert (os.stat(private).st_mode & 0o7777) == 0o700


def test_an_existing_ancestor_others_cannot_traverse_is_refused(host):
    closed = host / "closed"
    closed.mkdir()
    os.chmod(closed, 0o700)
    with pytest.raises(OSError, match="not traversable"):
        T._make_reachable(closed / "m")
    assert (os.stat(closed).st_mode & 0o7777) == 0o700
