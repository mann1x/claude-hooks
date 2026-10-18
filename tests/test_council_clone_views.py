"""Plan E3: write-scope views — a reflink (or capped copy) clone per lane.

Exercises :mod:`claude_hooks.sandbox.clone_views` on the host, without a
sandbox: a "command" here is a direct write into the lane's clone, which
is what a command bound at the scope's real path does. The live run
through the jail is in ``tests/test_council_shell_escape.py``
(``TestScopedView``).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

if not sys.platform.startswith("linux"):
    pytest.skip("the Linux sandbox's views", allow_module_level=True)

from claude_hooks.sandbox import clone_views as C  # noqa: E402
from claude_hooks.sandbox import views as V  # noqa: E402
from claude_hooks.sandbox.views import ViewError  # noqa: E402


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "models"
    (r / "scripts" / "lib").mkdir(parents=True)
    (r / "scripts" / "run.py").write_text("print('v1')\n")
    (r / "scripts" / "lib" / "util.py").write_text("X = 1\n")
    (r / "scripts" / "old").mkdir()
    (r / "scripts" / "old" / "a.txt").write_text("a\n")
    (r / "weights.bin").write_bytes(b"\0" * 4096)
    return r


def _views(root, sid="csl-cv", scopes=("scripts",), **kw):
    return V.session_views(sid=sid, cwd=str(root), scopes=list(scopes), **kw)


def _clone(v, lane="researcher-0"):
    v.ensure(lane)
    return Path(v.lanes()[lane]["clones"][os.path.join(v.repo, "scripts")])


class TestScopes:

    def test_scopes_resolve_under_the_root_without_nesting(self, root):
        got = C.resolve_scopes(str(root), ["scripts", "scripts/lib",
                                           str(root / "scripts")])
        assert got == [os.path.realpath(root / "scripts")]

    @pytest.mark.parametrize("bad,why", [
        ("../elsewhere", "outside"), ("weights.bin", "not a directory"),
        ("nope", "not a directory")])
    def test_bad_scopes_are_refused(self, root, bad, why):
        (root.parent / "elsewhere").mkdir(exist_ok=True)
        with pytest.raises(ViewError, match=why):
            C.resolve_scopes(str(root), [bad])

    def test_the_store_is_not_a_scope(self, root):
        (root / ".claude-hooks" / "consultants").mkdir(parents=True)
        with pytest.raises(ViewError, match="store"):
            C.resolve_scopes(str(root), [".claude-hooks/consultants"])

    def test_a_non_git_root_with_scopes_gets_clone_views(self, root):
        v = _views(root)
        assert isinstance(v, C.CloneViews)
        assert V.session_views(sid="csl-none", cwd=str(root)) is None


class TestLifecycle:

    def test_the_clone_is_bound_at_the_scope_and_matches_it(self, root):
        v = _views(root)
        m = v.ensure("researcher-0")
        clone = _clone(v)
        assert m.binds == [("rw", str(clone), os.path.realpath(root / "scripts"))]
        assert (clone / "run.py").read_text() == "print('v1')\n"
        assert v.status("researcher-0") == []
        assert v.host_path("researcher-0", str(root / "scripts" / "run.py")) \
            == str(clone / "run.py")
        assert v.host_path("researcher-0", str(root / "weights.bin")) \
            == str(root / "weights.bin")
        # A second ensure reuses the clone.
        assert v.ensure("researcher-0").binds == m.binds

    def test_changes_are_held_and_the_users_tree_is_untouched(self, root):
        v = _views(root)
        clone = _clone(v)
        (clone / "run.py").write_text("print('v2')\n")
        (clone / "new.py").write_text("NEW = 1\n")
        (clone / "fresh").mkdir()
        (clone / "lib" / "util.py").unlink()
        subprocess.run(["rm", "-rf", str(clone / "old")], check=True)
        st = v.status("researcher-0")
        assert "M scripts/run.py" in st and "A scripts/new.py" in st
        assert "D scripts/lib/util.py" in st and "A scripts/fresh/" in st
        assert "D scripts/old/a.txt" in st
        rec = v.freeze("researcher-0")
        assert rec["state"] == "held"
        assert not (Path(v.lane_dir("researcher-0")) / "clone").exists()
        paths = {f["path"] for f in rec["files"]}
        assert {"scripts/run.py", "scripts/new.py", "scripts/fresh/"} <= paths
        assert (root / "scripts" / "run.py").read_text() == "print('v1')\n"
        patch = Path(rec["patch"]).read_text()
        assert "-print('v1')" in patch and "+print('v2')" in patch
        assert "as copies of the changed files" in V.summary(v)

    def test_apply_copies_back_and_deletes(self, root):
        v = _views(root)
        clone = _clone(v)
        (clone / "run.py").write_text("print('v2')\n")
        (clone / "new.py").write_text("NEW = 1\n")
        subprocess.run(["rm", "-rf", str(clone / "old")], check=True)
        v.freeze("researcher-0")
        assert v.apply("researcher-0", dry_run=True) == "applies cleanly"
        assert (root / "scripts" / "run.py").read_text() == "print('v1')\n"
        assert v.apply("researcher-0") == "applied"
        assert (root / "scripts" / "run.py").read_text() == "print('v2')\n"
        assert (root / "scripts" / "new.py").read_text() == "NEW = 1\n"
        assert not (root / "scripts" / "old").exists()
        assert v.lanes()["researcher-0"]["state"] == "applied"

    def test_apply_refuses_a_file_the_user_changed_since(self, root):
        v = _views(root)
        clone = _clone(v)
        (clone / "run.py").write_text("print('lane')\n")
        v.freeze("researcher-0")
        time.sleep(0.01)
        (root / "scripts" / "run.py").write_text("print('user, newer')\n")
        with pytest.raises(ViewError, match="newer work"):
            v.apply("researcher-0")
        assert (root / "scripts" / "run.py").read_text() == "print('user, newer')\n"
        with pytest.raises(ViewError, match="3-way"):
            v.apply("researcher-0", three_way=True)

    def test_a_deleted_dir_the_user_added_to_is_kept(self, root):
        v = _views(root)
        clone = _clone(v)
        subprocess.run(["rm", "-rf", str(clone / "old")], check=True)
        v.freeze("researcher-0")
        (root / "scripts" / "old" / "mine.txt").write_text("user's\n")
        out = v.apply("researcher-0")
        assert "kept 1 deleted directory" in out
        assert (root / "scripts" / "old" / "mine.txt").exists()
        assert not (root / "scripts" / "old" / "a.txt").exists()

    def test_a_write_that_restores_mtime_is_still_seen(self, root):
        """``touch -d`` puts mtime back; ctime cannot be put back."""
        v = _views(root)
        clone = _clone(v)
        f = clone / "run.py"
        st = os.stat(f)
        time.sleep(0.01)
        f.write_text("print('v9')\n")    # same size as v1
        os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))
        assert "M scripts/run.py" in v.status("researcher-0")

    def test_nothing_changed_is_empty_and_removed(self, root):
        v = _views(root)
        _clone(v)
        assert v.freeze("researcher-0")["state"] == "empty"
        assert V.summary(v) == ""

    def test_discard_and_expire(self, root):
        v = _views(root)
        (_clone(v, "a") / "run.py").write_text("x\n")
        (_clone(v, "b") / "run.py").write_text("y\n")
        v.freeze_all()
        assert v.discard("a")["state"] == "discarded"
        assert not (Path(v.lane_dir("a")) / "held").exists()
        rec = v.expire("b")
        assert rec["state"] == "expired" and Path(rec["held"]).is_dir()

    def test_views_reload_from_their_state(self, root):
        v = _views(root)
        (_clone(v) / "run.py").write_text("x\n")
        again = V.session_views(sid="csl-cv", cwd=str(root))
        assert isinstance(again, C.CloneViews)
        assert again.scopes == v.scopes
        assert "M scripts/run.py" in again.status("researcher-0")

    def test_sweep_freezes_a_dead_councils_clone(self, root):
        v = _views(root, sid="csl-dead")
        (_clone(v) / "run.py").write_text("x\n")
        assert V.sweep(live_sids=set()) == ["freeze csl-dead/researcher-0"]
        assert v.lanes()["researcher-0"]["state"] == "held"


class TestStoreAndCopy:

    def test_scope_at_the_root_leaves_the_store_out(self, root):
        store = root / ".claude-hooks" / "consultants" / "other"
        store.mkdir(parents=True)
        (store / "secret.txt").write_text("another council's\n")
        v = _views(root, sid="csl-root", scopes=(".",))
        v.ensure("researcher-0")
        rec = v.lanes()["researcher-0"]
        clone = Path(rec["clones"][str(v.repo)])
        assert rec["wt"] == str(clone)          # the coder can use it
        assert (clone / ".claude-hooks" / "consultants").is_dir()
        assert list((clone / ".claude-hooks" / "consultants").iterdir()) == []
        assert v.status("researcher-0") == []

    def test_no_reflink_and_over_the_cap_is_refused(self, root, monkeypatch):
        monkeypatch.setattr(C, "reflink_supported", lambda *a: False)
        v = _views(root, sid="csl-cap", copy_cap_bytes=10)
        with pytest.raises(ViewError, match="cannot reflink"):
            v.ensure("researcher-0")
        assert "researcher-0" not in v.lanes()
        assert not (Path(v.lane_dir("researcher-0")) / "clone").exists()

    def test_no_reflink_under_the_cap_copies(self, root, monkeypatch):
        monkeypatch.setattr(C, "reflink_supported", lambda *a: False)
        v = _views(root, sid="csl-copy")
        v.ensure("researcher-0")
        assert v.lanes()["researcher-0"]["how"] == {
            os.path.realpath(root / "scripts"): "copy"}

    def test_too_many_entries_is_refused(self, root, monkeypatch):
        monkeypatch.setattr(C, "MAX_SCOPE_ENTRIES", 2)
        with pytest.raises(ViewError, match="more than 2 entries"):
            _views(root, sid="csl-big").ensure("researcher-0")


class TestFileToolsReadTheClone:

    def test_relative_and_absolute_paths_move_into_the_view(self, root):
        from claude_hooks.tool_registry.registry import _in_view
        seen = {}

        class Owner:
            def execute(self, tool, raw, cwd):
                seen.update(json.loads(raw), cwd=cwd)
                return f"read {json.loads(raw)['path']}"

        real = str(root / "scripts")
        maps = [(real, "/view/0")]
        out = _in_view(Owner(), "read_file", json.dumps(
            {"path": "scripts/run.py"}), str(root), maps)
        assert seen["path"] == "/view/0/run.py" and seen["cwd"] == str(root)
        assert out == f"read {real}/run.py"
        _in_view(Owner(), "read_file", json.dumps(
            {"path": "weights.bin"}), str(root), maps)
        assert seen["path"] == "weights.bin"            # outside the scope
        # The git views' pair form still works, cwd moved.
        _in_view(Owner(), "read_file", json.dumps({"path": "a.py"}),
                 real, (real, "/view/0"))
        assert seen == {"path": "a.py", "cwd": "/view/0"}


class TestEnable:

    def test_scopes_need_write_mode(self):
        from consultants.server import shell_enable as se
        with pytest.raises(se.ShellRequestError, match="mode 'write'"):
            se.parse({"mode": "read", "scopes": ["x"]})
        with pytest.raises(se.ShellRequestError, match="non-empty list"):
            se.parse({"mode": "write", "scopes": []})
        assert se.parse({"mode": "write", "scopes": ["/a"]}) == {
            "mode": "write", "net": "none", "scopes": ["/a"]}
        assert "scopes" not in se.parse("write")

    def test_preflight_names_the_scope_flag_on_a_non_git_root(self, root):
        from consultants.server import shell_enable as se
        err = se.preflight_error(None, {"mode": "write"}, cwd=str(root))
        assert "--write-scope" in err
        err = se.preflight_error(None, {"mode": "write", "scopes": ["nope"]},
                                 cwd=str(root))
        assert err.startswith("shell write scopes:")

    def test_provenance_and_settings_carry_the_scopes(self):
        from consultants.server import shell_enable as se
        sh = {"mode": "write", "net": "none", "scopes": ["/m/scripts"],
              "since": "the ask"}
        assert "write on /m/scripts" in se.provenance(sh)
        assert se.settings_for(None, sh).write_scopes == ("/m/scripts",)

    def test_the_tool_says_only_the_scope_is_writable(self):
        from claude_hooks.tool_registry.shell_provider import (
            ShellSettings, _spec)
        d = _spec(ShellSettings(mode="write", write_scopes=("/m/scripts",))
                  )["function"]["description"]
        assert "Only /m/scripts is YOUR PRIVATE WRITABLE COPY" in d
        assert "git worktree" not in d

    def test_cli_makes_scopes_absolute(self, tmp_path):
        from consultants import cli
        ns = type("A", (), {"shell": "write", "write_scope": ["scripts"],
                            "cwd": str(tmp_path)})()
        assert cli._shell_body(ns) == {
            "mode": "write", "scopes": [str((tmp_path / "scripts").resolve())]}
        ns.shell = "read"
        with pytest.raises(cli.CLIError):
            cli._shell_body(ns)
