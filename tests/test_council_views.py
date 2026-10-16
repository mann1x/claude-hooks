"""Plan E3/E4: write-mode lane views — git worktrees, patches, lifecycle.

Host-side only (no sandbox needed, so these run wherever git does): the
base snapshot, the worktree and its mounts, capture to a patch, apply /
discard / expire, the crash sweep at every lifecycle state, the
"Sandbox changes" section, the file-tool routing into a lane's view, and
the enablement and CLI around them. The jailed half — commands inside a
view, ``kill -9`` mid-command — is in ``tests/test_council_shell_escape.py``.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

if not shutil.which("git"):
    pytest.skip("git", allow_module_level=True)

from claude_hooks.sandbox import views as V  # noqa: E402


def _git(repo, *args) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    (r / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (r / ".gitignore").write_text(".claude-hooks/consultants/\n.env\n"
                                  "__pycache__/\nnode_modules/\n")
    (r / ".env").write_text("TOKEN=x\n")
    (r / "node_modules").mkdir()
    (r / "node_modules" / "dep.js").write_text("1\n")
    (r / "__pycache__").mkdir()
    _git(r, "add", "-A")
    _git(r, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    (r / "calc.py").write_text("def add(a, b):\n    return a - b  # wip\n")
    (r / "notes.txt").write_text("untracked user file\n")
    return r


def _views(repo, sid="csl-v") -> V.SessionViews:
    v = V.session_views(sid=sid, cwd=str(repo))
    assert v is not None
    return v


def _worktrees(repo) -> list[str]:
    return [ln for ln in _git(repo, "worktree", "list", "--porcelain").splitlines()
            if ln.startswith("worktree ")]


class TestBaseAndView:

    def test_the_base_is_the_users_tree_as_found(self, repo):
        v = _views(repo)
        base = v.base()
        files = set(_git(repo, "ls-tree", "-r", "--name-only", base).split())
        assert files == {".gitignore", "calc.py", "notes.txt"}
        assert "# wip" in _git(repo, "show", f"{base}:calc.py")
        assert _git(repo, "rev-parse", "refs/claude-hooks/council/csl-v/base"
                    ).strip() == base
        assert "notes.txt" in _git(repo, "status", "--porcelain")   # untouched
        assert v.base() == base                                      # once

    def test_a_huge_untracked_set_is_refused(self, repo, monkeypatch):
        monkeypatch.setattr(V, "MAX_UNTRACKED_BYTES", 4)
        with pytest.raises(V.ViewError, match="untracked files"):
            _views(repo).base()

    def test_ensure_makes_a_worktree_and_its_mounts(self, repo):
        v = _views(repo)
        m = v.ensure("researcher-0")
        rec = v.lanes()["researcher-0"]
        assert rec["state"] == "active" and os.path.isdir(rec["wt"])
        assert (Path(rec["wt"]) / "notes.txt").exists()
        kinds = [(k, d) for k, _, d in m.binds]
        assert kinds[0] == ("rw", str(repo.resolve()))
        dsts = {d for _, _, d in m.binds}
        assert str(repo.resolve() / ".env") in dsts
        assert str(repo.resolve() / "node_modules") in dsts
        assert not any("__pycache__" in d or "consultants" in d for d in dsts)
        assert m.env["GIT_WORK_TREE"] == str(repo.resolve())
        assert m.env["GIT_DIR"].startswith(V.JAIL_GIT + "/worktrees/")
        assert v.ensure("researcher-0").binds == m.binds      # reused
        reg = V.registered()
        assert reg[0]["lanes"] == {"researcher-0": "active"}

    def test_an_ignored_dir_holding_the_store_is_not_mounted(self, repo):
        (repo / ".gitignore").write_text(".claude-hooks/\n.env\n")
        (repo / ".claude-hooks" / "consultants").mkdir(parents=True)
        (repo / ".claude-hooks" / "other.txt").write_text("x\n")
        m = _views(repo).ensure("l")
        dsts = {d for _, _, d in m.binds}
        assert not any(".claude-hooks" in d for d in dsts), dsts
        assert str(repo.resolve() / ".env") in dsts

    def test_status_is_against_the_base_even_after_a_commit(self, repo):
        v = _views(repo)
        v.ensure("l")
        wt = Path(v.lanes()["l"]["wt"])
        (wt / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        (wt / "new.py").write_text("x = 1\n")
        st = v.status("l")
        assert "M calc.py" in st and "A new.py (untracked)" in st
        env = {**os.environ, "GIT_DIR": v.lanes()["l"]["admin"],
               "GIT_WORK_TREE": str(wt)}
        subprocess.run(["git", "-c", "user.name=m", "-c", "user.email=m@m",
                        "commit", "-qam", "fix"], cwd=wt, env=env, check=True)
        assert "M calc.py" in v.status("l")


class TestFreezeApplyDiscard:

    def _changed(self, repo, lane="researcher-0", sid="csl-v"):
        v = _views(repo, sid)
        v.ensure(lane)
        wt = Path(v.lanes()[lane]["wt"])
        (wt / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        (wt / "notes.txt").unlink()
        (wt / "new.py").write_text("x = 1\n")
        return v

    def test_freeze_captures_a_patch_and_removes_the_worktree(self, repo):
        v = self._changed(repo)
        rec = v.freeze("researcher-0")
        assert rec["state"] == "held"
        assert {(f["status"], f["path"]) for f in rec["files"]} == {
            ("M", "calc.py"), ("D", "notes.txt"), ("A", "new.py")}
        assert not os.path.exists(rec["wt"])
        assert len(_worktrees(repo)) == 1
        assert V.registered()[0]["lanes"] == {"researcher-0": "held"}
        assert "Your files are UNCHANGED" in V.summary(v)
        assert "a - b  # wip" in (repo / "calc.py").read_text()

    def test_an_unchanged_view_leaves_nothing(self, repo):
        v = _views(repo)
        v.ensure("l")
        assert v.freeze("l")["state"] == "empty"
        assert len(_worktrees(repo)) == 1
        assert _git(repo, "for-each-ref", "refs/claude-hooks") == ""
        assert V.registered() == []
        assert V.summary(v) == ""

    def test_apply_checks_first_then_applies(self, repo):
        v = self._changed(repo)
        v.freeze("researcher-0")
        assert v.apply("researcher-0", dry_run=True) == "applies cleanly"
        assert "# wip" in (repo / "calc.py").read_text()
        assert v.apply("researcher-0") == "applied"
        assert "a + b" in (repo / "calc.py").read_text()
        assert (repo / "new.py").exists() and not (repo / "notes.txt").exists()
        assert v.lanes()["researcher-0"]["state"] == "applied"
        assert _git(repo, "for-each-ref", "refs/claude-hooks") == ""
        with pytest.raises(V.ViewError, match="no held changes"):
            v.apply("researcher-0")

    def test_a_patch_that_no_longer_fits_is_refused(self, repo):
        v = self._changed(repo)
        v.freeze("researcher-0")
        (repo / "calc.py").write_text("something else entirely\n")
        with pytest.raises(V.ViewError, match="try --3way"):
            v.apply("researcher-0")
        assert (repo / "calc.py").read_text() == "something else entirely\n"

    def test_discard_and_expire(self, repo):
        v = self._changed(repo)
        v.ensure("researcher-1")
        v.freeze_all()
        assert v.discard("researcher-0")["state"] == "discarded"
        rec = v.expire("researcher-1")
        assert rec["state"] == "empty" or rec["state"] == "expired"
        v2 = self._changed(repo, sid="csl-x")
        v2.freeze("researcher-0")
        rec = v2.expire("researcher-0")
        assert rec["state"] == "expired" and "/expired/" in rec["patch"]
        assert os.path.exists(rec["patch"])           # kept, never lost

    def test_discard_an_active_view_removes_its_worktree(self, repo):
        v = _views(repo)
        v.ensure("l")
        v.discard("l")
        assert len(_worktrees(repo)) == 1


class TestCrashAtEveryState:
    """kill -9 at each lifecycle state, then the next engine's sweep:
    nothing is left registered, checked out or referenced but held
    patches."""

    def test_creating(self, repo):
        v = _views(repo)
        v.base()
        d = v._load()
        wt = v.lane_dir("l") / "wt"
        d["lanes"]["l"] = {"state": "creating", "wt": str(wt)}
        v._save(d)
        _git(repo, "worktree", "add", "--detach", "--lock", str(wt), d["base"])
        assert V.sweep(live_sids=set()) == ["freeze csl-v/l"]
        assert not wt.exists() and len(_worktrees(repo)) == 1
        assert v.lanes()["l"]["state"] == "empty"
        assert V.registered() == []
        assert _git(repo, "for-each-ref", "refs/claude-hooks") == ""

    def test_active_dead_council_is_frozen_to_a_patch(self, repo):
        v = _views(repo)
        v.ensure("l")
        (Path(v.lanes()["l"]["wt"]) / "calc.py").write_text("changed\n")
        V.sweep(live_sids=set())
        assert v.lanes()["l"]["state"] == "held"
        assert len(_worktrees(repo)) == 1

    def test_active_with_its_worktree_gone(self, repo):
        v = _views(repo)
        v.ensure("l")
        shutil.rmtree(v.lanes()["l"]["wt"])
        V.sweep(live_sids=set())
        assert v.lanes()["l"]["state"] == "lost"
        assert len(_worktrees(repo)) == 1 and V.registered() == []

    def test_a_resuming_council_keeps_its_view(self, repo):
        v = _views(repo)
        v.ensure("l")
        assert V.sweep(live_sids={"csl-v"}) == []
        assert v.lanes()["l"]["state"] == "active"

    def test_held_past_its_ttl_expires(self, repo):
        v = _views(repo)
        v.ensure("l")
        (Path(v.lanes()["l"]["wt"]) / "calc.py").write_text("changed\n")
        v.freeze("l")
        frozen = v.lanes()["l"]["frozen_at"]
        assert V.sweep(live_sids=set(), now=frozen + 86400) == []
        assert V.sweep(live_sids=set(), held_ttl_days=7,
                       now=frozen + 8 * 86400) == ["expire csl-v/l"]
        assert v.lanes()["l"]["state"] == "expired"

    def test_a_real_kill_9_after_ensure(self, repo):
        code = ("import os, signal, sys; sys.path.insert(0, %r);"
                "from claude_hooks.sandbox import views as V;"
                "V.session_views(sid='csl-k', cwd=%r).ensure('l');"
                "os.kill(os.getpid(), signal.SIGKILL)") % (
                    str(Path(__file__).resolve().parent.parent), str(repo))
        p = subprocess.run([os.sys.executable, "-c", code], env=os.environ)
        assert p.returncode == -9
        assert V.registered()[0]["lanes"] == {"l": "active"}
        V.sweep(live_sids=set())
        assert len(_worktrees(repo)) == 1 and V.registered() == []

    def test_a_repo_that_is_gone_is_forgotten(self, repo):
        _views(repo).ensure("l")
        shutil.rmtree(repo)
        assert V.sweep(live_sids=set())[0].startswith("forget csl-v")
        assert V.registered() == []


# --------------------------------------------------- file-tool routing
def test_a_lanes_file_tools_read_its_view(repo):
    from claude_hooks.tool_registry import BuiltinToolProvider, ToolRegistry
    from claude_hooks.tool_registry.shell_provider import (ShellSettings,
                                                           ShellToolProvider)
    shell = ShellToolProvider(sid="csl-v", settings=ShellSettings(mode="write"),
                              backend=_Ready())
    reg = ToolRegistry([BuiltinToolProvider(), shell])
    v = shell.views(str(repo))
    v.ensure("researcher-0")
    wt = Path(v.lanes()["researcher-0"]["wt"])
    (wt / "calc.py").write_text("in the view\n")
    ex = reg.as_executor()
    for path in ("calc.py", str(repo.resolve() / "calc.py")):
        out = ex("read_file", json.dumps({"path": path}), str(repo),
                 lane="researcher:0")
        assert "in the view" in out and str(wt) not in out
    assert "a - b" in ex("read_file", '{"path": "calc.py"}', str(repo),
                         lane="researcher:1")
    assert "a - b" in ex("read_file", '{"path": "calc.py"}', str(repo))


class _Ready:
    name = "fake"

    def preflight(self, *, refresh=False):
        from claude_hooks.sandbox import Preflight
        return Preflight(backend="fake", ok=True)


# ------------------------------------------------------ enablement + CLI
def test_write_needs_a_git_root(tmp_path, repo, monkeypatch):
    import claude_hooks.sandbox as sb
    from consultants import config as cc
    from consultants.server import shell_enable as se
    monkeypatch.setattr(sb, "backend_for", lambda name="auto": _Ready())

    class Cfg:
        tools = cc.ToolsConfig()
    plain = tmp_path / "plain"
    plain.mkdir()
    err = se.preflight_error(Cfg(), {"mode": "write", "since": "the ask"},
                             cwd=str(plain))
    assert "not in a git work tree" in err
    assert se.preflight_error(Cfg(), {"mode": "write", "since": "the ask"},
                              cwd=str(repo)) is None


def test_the_answer_carries_the_held_changes(repo):
    from consultants.server.shell_enable import answer_with_shell
    v = _views(repo, "csl-a")
    v.ensure("researcher-0")
    (Path(v.lanes()["researcher-0"]["wt"]) / "calc.py").write_text("x\n")
    shell = {"mode": "write", "net": "none", "since": "the ask"}
    out = answer_with_shell("ANSWER", shell, cwd=str(repo), sid="csl-a")
    assert out.startswith("ANSWER\n\n## Sandbox changes")
    assert "M `calc.py`" in out
    assert out.rstrip().endswith("_shell: enabled since the ask (write, net none)_")
    assert answer_with_shell("ANSWER", shell, cwd=str(repo), sid="csl-a") == out
    assert len(_worktrees(repo)) == 1


def test_cli_verbs(repo, capsys):
    from consultants import cli
    v = _views(repo, "csl-c")
    v.ensure("researcher-0")
    (Path(v.lanes()["researcher-0"]["wt"]) / "calc.py").write_text("x\n")
    v.freeze_all()
    p = cli.build_parser()

    def run(*argv):
        a = p.parse_args(list(argv))
        return a.fn(a, "http://unused")
    assert run("changes", "csl-c", "--cwd", str(repo), "--diff") == 0
    out = capsys.readouterr().out
    assert "lane researcher-0: held, 1 file(s)" in out and "+x" in out
    assert run("sandbox", "ls") == 0
    assert "csl-c" in capsys.readouterr().out
    assert run("apply", "csl-c", "--cwd", str(repo), "--lane", "researcher-0",
               "--dry-run") == 0
    assert (repo / "calc.py").read_text() != "x\n"
    assert run("discard", "csl-c", "--cwd", str(repo), "--all") == 0
    assert "discarded" in capsys.readouterr().out
    assert run("sandbox", "gc", "--dry-run") == 0
    with pytest.raises(cli.CLIError):
        run("changes", "csl-none", "--cwd", str(repo))
