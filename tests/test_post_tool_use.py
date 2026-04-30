"""Tests for the PostToolUse ruff handler.

The handler is the second-cheapest hook in the framework (after
session_start) — it runs synchronously between every Edit/Write and
the next assistant turn. Tests target two layers:

- ``handle()`` — the whole handler with the dispatcher contract:
  reads ``event``/``config``, returns ``hookSpecificOutput`` or None.
  We mock the subprocess call so the test doesn't depend on a real
  ruff binary or on filesystem timing.
- ``_run_ruff()`` / ``_resolve_ruff()`` — direct unit tests of the
  ruff invocation path, including timeout, missing-binary and
  config-error cases. One end-to-end test does spawn the real ruff
  binary against a tmp file with a known issue, gated so it skips
  cleanly when ruff isn't on the host.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from claude_hooks.hooks import post_tool_use


def _event(tool_name: str = "Edit", file_path: str = "x.py",
           cwd: str = "") -> dict:
    return {
        "tool_name": tool_name,
        "tool_input": {"file_path": file_path},
        "cwd": cwd,
    }


def _cfg(**post_tool_use_overrides: Any) -> dict:
    base = {"enabled": True, "ruff_enabled": True}
    base.update(post_tool_use_overrides)
    return {"hooks": {"post_tool_use": base}}


# --------------------------------------------------------------------- #
# handle() — gating
# --------------------------------------------------------------------- #


class TestHandleGating:
    def test_disabled_returns_none(self, tmp_path: Path):
        (tmp_path / "x.py").write_text("import os\n")
        out = post_tool_use.handle(
            event=_event(file_path="x.py", cwd=str(tmp_path)),
            config=_cfg(enabled=False),
            providers=[],
        )
        assert out is None

    def test_non_edit_tool_returns_none(self, tmp_path: Path):
        (tmp_path / "x.py").write_text("import os\n")
        out = post_tool_use.handle(
            event=_event(tool_name="Read", file_path="x.py",
                         cwd=str(tmp_path)),
            config=_cfg(),
            providers=[],
        )
        assert out is None

    def test_missing_file_returns_none(self, tmp_path: Path):
        out = post_tool_use.handle(
            event=_event(file_path="ghost.py", cwd=str(tmp_path)),
            config=_cfg(),
            providers=[],
        )
        assert out is None

    def test_missing_path_in_input_returns_none(self, tmp_path: Path):
        out = post_tool_use.handle(
            event={"tool_name": "Edit", "tool_input": {},
                   "cwd": str(tmp_path)},
            config=_cfg(),
            providers=[],
        )
        assert out is None

    def test_non_python_file_skipped(self, tmp_path: Path):
        (tmp_path / "x.md").write_text("# hi\n")
        # Even with the file present, ruff's extension filter rejects.
        with patch.object(post_tool_use, "_resolve_ruff",
                          return_value="/fake/ruff"):
            with patch.object(post_tool_use.subprocess, "run") as srun:
                out = post_tool_use.handle(
                    event=_event(file_path="x.md", cwd=str(tmp_path)),
                    config=_cfg(),
                    providers=[],
                )
        assert out is None
        # Subprocess should never have been spawned for a .md file.
        srun.assert_not_called()


# --------------------------------------------------------------------- #
# handle() — diagnostic emission
# --------------------------------------------------------------------- #


class TestHandleEmits:
    def test_clean_file_returns_none(self, tmp_path: Path):
        path = tmp_path / "ok.py"
        path.write_text("x = 1\n")
        # Simulate ruff exit 0 / no stdout (clean file).
        proc = subprocess.CompletedProcess(args=[], returncode=0,
                                           stdout="", stderr="")
        with patch.object(post_tool_use, "_resolve_ruff",
                          return_value="/fake/ruff"):
            with patch.object(post_tool_use.subprocess, "run",
                              return_value=proc):
                out = post_tool_use.handle(
                    event=_event(file_path="ok.py", cwd=str(tmp_path)),
                    config=_cfg(),
                    providers=[],
                )
        assert out is None

    def test_dirty_file_emits_additional_context(self, tmp_path: Path):
        path = tmp_path / "bad.py"
        path.write_text("import os  # F401 unused\n")
        diagnostic_stdout = "bad.py:1:8: F401 [*] `os` imported but unused"
        proc = subprocess.CompletedProcess(args=[], returncode=1,
                                           stdout=diagnostic_stdout,
                                           stderr="")
        with patch.object(post_tool_use, "_resolve_ruff",
                          return_value="/fake/ruff"):
            with patch.object(post_tool_use.subprocess, "run",
                              return_value=proc):
                out = post_tool_use.handle(
                    event=_event(file_path="bad.py", cwd=str(tmp_path)),
                    config=_cfg(),
                    providers=[],
                )
        assert out is not None
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "Ruff diagnostics" in ctx
        assert "F401" in ctx
        assert "bad.py" in ctx

    def test_truncates_at_max_diagnostics(self, tmp_path: Path):
        (tmp_path / "noisy.py").write_text("\n")
        many = "\n".join(f"noisy.py:{i}:1: E501 line too long" for i in range(60))
        proc = subprocess.CompletedProcess(args=[], returncode=1,
                                           stdout=many, stderr="")
        with patch.object(post_tool_use, "_resolve_ruff",
                          return_value="/fake/ruff"):
            with patch.object(post_tool_use.subprocess, "run",
                              return_value=proc):
                out = post_tool_use.handle(
                    event=_event(file_path="noisy.py", cwd=str(tmp_path)),
                    config=_cfg(max_diagnostics=10),
                    providers=[],
                )
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "truncated to 10 lines" in ctx
        # The 11th-onward line should be missing.
        assert "noisy.py:11:" not in ctx

    def test_relative_path_resolves_under_cwd(self, tmp_path: Path):
        (tmp_path / "rel.py").write_text("x = 1\n")
        captured: dict = {}
        proc = subprocess.CompletedProcess(args=[], returncode=0,
                                           stdout="", stderr="")

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            return proc

        with patch.object(post_tool_use, "_resolve_ruff",
                          return_value="/fake/ruff"):
            with patch.object(post_tool_use.subprocess, "run",
                              side_effect=fake_run):
                post_tool_use.handle(
                    event=_event(file_path="rel.py", cwd=str(tmp_path)),
                    config=_cfg(),
                    providers=[],
                )
        # Last arg of the cmd should be the absolute path.
        assert captured["cmd"][-1] == str(tmp_path / "rel.py")

    def test_absolute_path_passes_through(self, tmp_path: Path):
        abs_path = tmp_path / "abs.py"
        abs_path.write_text("x = 1\n")
        captured: dict = {}
        proc = subprocess.CompletedProcess(args=[], returncode=0,
                                           stdout="", stderr="")

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            return proc

        with patch.object(post_tool_use, "_resolve_ruff",
                          return_value="/fake/ruff"):
            with patch.object(post_tool_use.subprocess, "run",
                              side_effect=fake_run):
                post_tool_use.handle(
                    event=_event(file_path=str(abs_path),
                                 cwd=str(tmp_path)),
                    config=_cfg(),
                    providers=[],
                )
        assert captured["cmd"][-1] == str(abs_path)


class TestPathDisplay:
    def test_header_uses_project_cwd_for_relpath(self, tmp_path: Path):
        # Simulate the daemon scenario: process cwd is "/" but the
        # event cwd is the user's project. _shorten_path must use
        # the event cwd so the header shows ``foo.py`` not
        # ``../../tmp/.../foo.py``.
        path = tmp_path / "foo.py"
        path.write_text("import os\n")
        proc = subprocess.CompletedProcess(args=[], returncode=1,
                                           stdout=f"{path}:1:8: F401 unused",
                                           stderr="")
        with patch.object(post_tool_use, "_resolve_ruff",
                          return_value="/fake/ruff"):
            with patch.object(post_tool_use.subprocess, "run",
                              return_value=proc):
                with patch.object(post_tool_use.os, "getcwd",
                                  return_value="/"):
                    out = post_tool_use.handle(
                        event=_event(file_path=str(path),
                                     cwd=str(tmp_path)),
                        config=_cfg(),
                        providers=[],
                    )
        ctx = out["hookSpecificOutput"]["additionalContext"]
        # Header line: ``## Ruff diagnostics — `foo.py``` — without the
        # daemon-relative ``../../tmp/...`` prefix.
        first_line = ctx.split("\n", 1)[0]
        assert "`foo.py`" in first_line

    def test_shorten_path_falls_back_to_abs_when_outside_project(self):
        out = post_tool_use._shorten_path("/var/log/x.py",
                                          project_cwd="/home/user/proj")
        assert out == "/var/log/x.py"

    def test_shorten_path_relative_when_inside_project(self):
        out = post_tool_use._shorten_path("/proj/sub/x.py",
                                          project_cwd="/proj")
        assert out == "sub/x.py"


# --------------------------------------------------------------------- #
# Failure paths — should never crash, never emit
# --------------------------------------------------------------------- #


class TestFailureSilenced:
    def test_ruff_missing_returns_none(self, tmp_path: Path):
        (tmp_path / "x.py").write_text("x = 1\n")
        with patch.object(post_tool_use, "_resolve_ruff", return_value=None):
            out = post_tool_use.handle(
                event=_event(file_path="x.py", cwd=str(tmp_path)),
                config=_cfg(),
                providers=[],
            )
        assert out is None

    def test_timeout_returns_none(self, tmp_path: Path):
        (tmp_path / "x.py").write_text("x = 1\n")

        def boom(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="ruff", timeout=0.1)

        with patch.object(post_tool_use, "_resolve_ruff",
                          return_value="/fake/ruff"):
            with patch.object(post_tool_use.subprocess, "run",
                              side_effect=boom):
                out = post_tool_use.handle(
                    event=_event(file_path="x.py", cwd=str(tmp_path)),
                    config=_cfg(),
                    providers=[],
                )
        assert out is None

    def test_oserror_returns_none(self, tmp_path: Path):
        (tmp_path / "x.py").write_text("x = 1\n")
        with patch.object(post_tool_use, "_resolve_ruff",
                          return_value="/fake/ruff"):
            with patch.object(post_tool_use.subprocess, "run",
                              side_effect=OSError("nope")):
                out = post_tool_use.handle(
                    event=_event(file_path="x.py", cwd=str(tmp_path)),
                    config=_cfg(),
                    providers=[],
                )
        assert out is None

    def test_nonzero_with_no_stdout_returns_none(self, tmp_path: Path):
        # ruff config error or similar — stderr but no stdout. Don't
        # pollute the model context with internal mess.
        (tmp_path / "x.py").write_text("x = 1\n")
        proc = subprocess.CompletedProcess(args=[], returncode=2,
                                           stdout="", stderr="bad config")
        with patch.object(post_tool_use, "_resolve_ruff",
                          return_value="/fake/ruff"):
            with patch.object(post_tool_use.subprocess, "run",
                              return_value=proc):
                out = post_tool_use.handle(
                    event=_event(file_path="x.py", cwd=str(tmp_path)),
                    config=_cfg(),
                    providers=[],
                )
        assert out is None


# --------------------------------------------------------------------- #
# _resolve_ruff()
# --------------------------------------------------------------------- #


class TestResolveRuff:
    def test_pinned_path_used_when_executable(self, tmp_path: Path):
        # Make a fake exe in tmp_path.
        fake = tmp_path / "myruff"
        fake.write_text("#!/bin/sh\necho ok\n")
        fake.chmod(0o755)
        cfg = {"ruff_path": str(fake)}
        assert post_tool_use._resolve_ruff(cfg) == str(fake)

    def test_pinned_path_missing_returns_none(self, tmp_path: Path):
        cfg = {"ruff_path": str(tmp_path / "nope")}
        # Even if PATH has ruff, an explicit pinned-but-missing path
        # should fail loudly (warning + None) rather than silently fall
        # back to PATH and surprise the user.
        assert post_tool_use._resolve_ruff(cfg) is None

    def test_path_lookup_when_unpinned(self, monkeypatch, tmp_path: Path):
        fake = tmp_path / "ruff"
        fake.write_text("#!/bin/sh\necho ok\n")
        fake.chmod(0o755)
        # Make ``which("ruff")`` find our fake one and the conda-env
        # fallback miss (point sys.executable at a dir that has no ruff).
        empty_dir = tmp_path / "noruff_env"
        empty_dir.mkdir()
        empty_python = empty_dir / "python"
        empty_python.write_text("")
        empty_python.chmod(0o755)
        monkeypatch.setattr(post_tool_use.os.sys, "executable",
                            str(empty_python))
        monkeypatch.setattr(post_tool_use.shutil, "which",
                            lambda name: str(fake) if name == "ruff" else None)
        assert post_tool_use._resolve_ruff({}) == str(fake)


# --------------------------------------------------------------------- #
# End-to-end against the real ruff binary (skipped when unavailable)
# --------------------------------------------------------------------- #


@pytest.mark.skipif(
    shutil.which("ruff") is None
    and not os.path.isfile(
        os.path.join(
            os.path.dirname(os.path.realpath(os.sys.executable)), "ruff",
        ),
    ),
    reason="ruff binary not available on this host",
)
class TestRealRuff:
    def test_real_ruff_catches_unused_import(self, tmp_path: Path):
        path = tmp_path / "real.py"
        path.write_text("import os\n")  # F401 unused
        out = post_tool_use.handle(
            event=_event(file_path="real.py", cwd=str(tmp_path)),
            config=_cfg(),
            providers=[],
        )
        assert out is not None
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "F401" in ctx

    def test_real_ruff_clean_file_returns_none(self, tmp_path: Path):
        path = tmp_path / "real.py"
        path.write_text("x = 1\n")
        out = post_tool_use.handle(
            event=_event(file_path="real.py", cwd=str(tmp_path)),
            config=_cfg(),
            providers=[],
        )
        assert out is None
