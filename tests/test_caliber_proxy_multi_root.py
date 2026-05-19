"""Tests for the multi-root tool sandbox (v1.8 candidate work).

Kept separate from the legacy ``test_caliber_proxy.py`` so that file
keeps documenting the single-cwd contract every existing caller
relies on. This file covers:

* ``resolve_in_roots`` accepts paths under any allowed root, rejects
  paths outside every root.
* The four path-aware tools (``list_files``, ``read_file``, ``glob``,
  ``grep``) honour ``extra_roots`` when called through the closure
  factory.
* ``make_executor(())`` is identical to bare ``execute``.
* The renamed error message lists the full allowed set so the model
  can self-correct on retry.
"""

from __future__ import annotations

import os
import json
import tempfile
import unittest
from pathlib import Path

import pytest

from claude_hooks.caliber_proxy import tools


# ---------- resolve_in_roots ----------

class TestResolveInRoots:

    def test_path_inside_primary_cwd(self, tmp_path: Path):
        (tmp_path / "a.txt").write_text("x")
        out = tools.resolve_in_roots("a.txt", str(tmp_path), ())
        assert out == str((tmp_path / "a.txt").resolve())

    def test_path_inside_extra_root(self, tmp_path: Path):
        cwd = tmp_path / "proj"
        extra = tmp_path / "sister"
        cwd.mkdir()
        extra.mkdir()
        (extra / "x.py").write_text("y")
        out = tools.resolve_in_roots(
            str(extra / "x.py"),
            str(cwd),
            (str(extra.resolve()),),
        )
        assert out == str((extra / "x.py").resolve())

    def test_path_outside_all_roots_rejects(self, tmp_path: Path):
        cwd = tmp_path / "proj"
        extra = tmp_path / "sister"
        outside = tmp_path / "secret"
        for p in (cwd, extra, outside):
            p.mkdir()
        (outside / "x.txt").write_text("z")
        with pytest.raises(ValueError) as exc:
            tools.resolve_in_roots(
                str(outside / "x.txt"),
                str(cwd),
                (str(extra.resolve()),),
            )
        msg = str(exc.value)
        assert "path escapes allowed roots" in msg
        # Error message lists every allowed root so the model can retry.
        assert str(cwd.resolve()) in msg
        assert str(extra.resolve()) in msg

    def test_symlink_pointing_outside_rejects(self, tmp_path: Path):
        cwd = tmp_path / "proj"
        cwd.mkdir()
        outside = tmp_path / "out"
        outside.mkdir()
        (outside / "secret.txt").write_text("nope")
        os.symlink(outside / "secret.txt", cwd / "link.txt")
        with pytest.raises(ValueError):
            tools.resolve_in_roots("link.txt", str(cwd), ())

    def test_relative_in_extra_root_resolves(self, tmp_path: Path):
        # Relative paths still resolve against PRIMARY cwd; the extra
        # roots only widen the absolute-path allowlist.
        cwd = tmp_path / "proj"
        extra = tmp_path / "sister"
        cwd.mkdir()
        extra.mkdir()
        (cwd / "f.txt").write_text("ok")
        out = tools.resolve_in_roots(
            "f.txt", str(cwd), (str(extra.resolve()),),
        )
        assert out == str((cwd / "f.txt").resolve())


# ---------- resolve_in_cwd (legacy shim) ----------

class TestResolveInCwdShim:

    def test_still_rejects_absolute_outside(self, tmp_path: Path):
        with pytest.raises(ValueError):
            tools.resolve_in_cwd("/etc/passwd", str(tmp_path))

    def test_error_message_renamed(self, tmp_path: Path):
        with pytest.raises(ValueError) as exc:
            tools.resolve_in_cwd("/etc/passwd", str(tmp_path))
        assert "path escapes allowed roots" in str(exc.value)


# ---------- list_files / read_file with extra_roots ----------

class TestPathAwareToolsMultiRoot:

    def test_list_files_accepts_extra_root(self, tmp_path: Path):
        cwd = tmp_path / "proj"
        extra = tmp_path / "sister"
        cwd.mkdir()
        extra.mkdir()
        (extra / "a.py").write_text("a")
        (extra / "b.py").write_text("b")
        out = tools.list_files(
            {"path": str(extra.resolve())},
            str(cwd),
            (str(extra.resolve()),),
        )
        assert "a.py" in out
        assert "b.py" in out

    def test_list_files_rejects_outside(self, tmp_path: Path):
        cwd = tmp_path / "proj"
        cwd.mkdir()
        # No extra roots; an absolute path outside cwd rejects.
        out = tools.list_files({"path": "/etc"}, str(cwd), ())
        assert out.startswith("error: path escapes allowed roots")

    def test_read_file_from_extra_root(self, tmp_path: Path):
        cwd = tmp_path / "proj"
        extra = tmp_path / "sister"
        cwd.mkdir()
        extra.mkdir()
        f = extra / "src.py"
        f.write_text("def foo():\n    pass\n")
        out = tools.read_file(
            {"path": str(f.resolve())},
            str(cwd),
            (str(extra.resolve()),),
        )
        # Header includes absolute path (file is OUTSIDE primary cwd).
        assert str(f.resolve()) in out
        assert "def foo" in out

    def test_read_file_inside_cwd_renders_relative(self, tmp_path: Path):
        cwd = tmp_path / "proj"
        cwd.mkdir()
        (cwd / "main.py").write_text("x = 1\n")
        out = tools.read_file({"path": "main.py"}, str(cwd), ())
        # Header is relative to cwd, NOT absolute.
        assert "main.py:" in out
        assert str(cwd.resolve()) not in out.split("\n")[0]

    def test_grep_finds_in_extra_root(self, tmp_path: Path):
        cwd = tmp_path / "proj"
        extra = tmp_path / "sister"
        cwd.mkdir()
        extra.mkdir()
        (extra / "x.py").write_text("def target():\n    return 1\n")
        out = tools.grep(
            {"pattern": "target", "path": str(extra.resolve())},
            str(cwd),
            (str(extra.resolve()),),
        )
        # Match line is absolute (lives outside primary cwd).
        assert str(extra.resolve()) in out
        assert "target" in out

    def test_glob_still_only_walks_primary_cwd(self, tmp_path: Path):
        cwd = tmp_path / "proj"
        extra = tmp_path / "sister"
        cwd.mkdir()
        extra.mkdir()
        (cwd / "in_cwd.py").write_text("x")
        (extra / "in_extra.py").write_text("y")
        out = tools.glob_files(
            {"pattern": "*.py"},
            str(cwd),
            (str(extra.resolve()),),
        )
        # Glob walks cwd only (documented limitation in v1.8). The
        # model uses read_file for cross-root reaches.
        assert "in_cwd.py" in out
        assert "in_extra.py" not in out


# ---------- execute() dispatch with _extra_roots ----------

class TestExecuteDispatch:

    def test_execute_threads_extra_roots(self, tmp_path: Path):
        cwd = tmp_path / "proj"
        extra = tmp_path / "sister"
        cwd.mkdir()
        extra.mkdir()
        (extra / "a.py").write_text("ok")
        out = tools.execute(
            "read_file",
            json.dumps({"path": str((extra / "a.py").resolve())}),
            str(cwd),
            _extra_roots=(str(extra.resolve()),),
        )
        assert "ok" in out

    def test_execute_no_extras_is_legacy(self, tmp_path: Path):
        cwd = tmp_path / "proj"
        cwd.mkdir()
        out = tools.execute(
            "read_file",
            json.dumps({"path": "/etc/hosts"}),
            str(cwd),
        )
        assert out.startswith("error: path escapes allowed roots")

    def test_non_path_tools_ignore_extras(self, tmp_path: Path):
        # survey_project doesn't use extra_roots; passing them must
        # not break the dispatch.
        cwd = tmp_path / "proj"
        cwd.mkdir()
        (cwd / "README.md").write_text("# proj")
        out = tools.execute(
            "survey_project", "{}", str(cwd),
            _extra_roots=("/totally/unused",),
        )
        # Output is the survey (doesn't matter what — just not an error).
        assert not out.startswith("error:")


# ---------- make_executor factory ----------

class TestMakeExecutor:

    def test_empty_returns_bare_execute(self):
        assert tools.make_executor(()) is tools.execute
        assert tools.make_executor(tuple()) is tools.execute

    def test_with_extras_returns_closure(self, tmp_path: Path):
        extra = tmp_path / "sister"
        extra.mkdir()
        exe = tools.make_executor((str(extra),))
        assert exe is not tools.execute

    def test_closure_signature_matches_tool_executor(self, tmp_path: Path):
        # Three positional args, returns string — matches
        # agent_loop.runner.ToolExecutor.
        exe = tools.make_executor((str(tmp_path),))
        out = exe("read_file", json.dumps({"path": "missing.txt"}),
                  str(tmp_path))
        assert isinstance(out, str)

    def test_closure_canonicalises_extra_roots(self, tmp_path: Path):
        # Pass a path with a trailing slash; closure should canonicalise.
        extra = tmp_path / "sister"
        extra.mkdir()
        (extra / "x.txt").write_text("hi")
        exe = tools.make_executor((str(extra) + "/",))
        out = exe(
            "read_file",
            json.dumps({"path": str((extra / "x.txt").resolve())}),
            str(tmp_path),
        )
        assert "hi" in out
