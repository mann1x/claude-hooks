"""v1.10.4 LSP-engine bug fixes — regression suite.

Covers four bug classes surfaced by the pandorum 2026-05-22 follow-up
session after v1.10.3 shipped:

A. **Toml caching across runs** — :class:`CompileRunner` snapshotted
   its compile command at daemon-startup and never re-read the toml.
   Edits to ``.claude-hooks/lsp-engine.toml`` went un-applied until
   the daemon was killed; on Windows there was no supported way to
   bounce it before v1.10.3, so users were stuck with stale commands
   even across Claude Code restarts. Fix: per-run mtime check at the
   top of ``_run_once`` reloads via :func:`load_engine_config` and
   replaces the in-flight command.

C. **No ``restart`` subcommand** — operator needed a one-shot way to
   ask the daemon "shut down + drop your state so my next edit
   spawns a fresh one with new toml". v1.10.3 shipped ``stop`` and
   ``cleanup`` separately; v1.10.4 composes them into ``restart``.

D. **``pid: null`` while daemon alive** — Windows ``msvcrt.locking``
   blocks reads of the lock file from anywhere except the locking
   process. ``daemon_pid()`` returned ``None`` even when the daemon
   was clearly serving the IPC request that the status CLI was
   making. Fix: daemon's ``status`` RPC includes ``os.getpid()`` so
   the CLI sees the live PID through the IPC channel, not the
   locked file.

E. **``.cmd`` shim cwd resolution** — the Windows shim ``cd /d
   %REPO%`` -ed into the claude-hooks repo before passing
   ``--project .`` to python. ``Path(".").resolve()`` in
   :mod:`__main__` then produced a hash for the **repo**, not the
   user's actual project. Silent and insidious — it tripped the
   v1.10.3 smoke even though the case-norm fix itself was correct.
   Fix: shim exports ``CLAUDE_HOOKS_USER_CWD=%CD%`` before cd-ing;
   :func:`_resolve_user_project` resolves non-absolute paths against
   it.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

HERE = Path(__file__).resolve().parent.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from claude_hooks.lsp_engine.compile import (  # noqa: E402
    CompileOrchestrator,
    CompileRunner,
    CompileSpec,
)
from claude_hooks.lsp_engine.config import (  # noqa: E402
    EngineConfig,
    LspServerSpec,
    SessionLockConfig,
)
from claude_hooks.lsp_engine.daemon import (  # noqa: E402
    Daemon,
    socket_path_for,
)

_FAKE_SERVER = Path(__file__).parent / "lsp_engine_fake_server.py"


def _fake_spec() -> LspServerSpec:
    return LspServerSpec(
        extensions=("py", "fake"),
        command=(sys.executable, str(_FAKE_SERVER)),
        root_dir=".",
    )


def _short_lock_config() -> EngineConfig:
    return EngineConfig(
        session_locks=SessionLockConfig(
            debounce_seconds=0.5,
            query_timeout_ms=200,
        ),
    )


# ──────────────────────────────────────────────────────────────────────
# Fix A — toml hot-reload in CompileRunner
# ──────────────────────────────────────────────────────────────────────


class TestCompileRunnerTomlHotReload(unittest.TestCase):
    """``CompileRunner`` must re-read ``.claude-hooks/lsp-engine.toml``
    when its mtime changes, picking up edited commands without a
    daemon restart."""

    def test_post_init_seeds_command_from_spec(self):
        """The mutable ``_command`` field is initialized to the
        immutable ``spec.command`` so callers that bypass the
        orchestrator (tests, direct construction) get sane behaviour
        even without ever calling ``_maybe_reload_command``."""
        spec = CompileSpec(language="py", command=("foo", "--bar"))
        runner = CompileRunner(spec=spec, project_root=Path("/tmp/p"))
        self.assertEqual(runner._command, ("foo", "--bar"))

    def test_maybe_reload_noop_when_toml_path_none(self):
        """With ``toml_path=None`` (legacy callers / no hot-reload),
        the method returns immediately and leaves ``_command``
        untouched."""
        spec = CompileSpec(language="py", command=("foo",))
        runner = CompileRunner(
            spec=spec, project_root=Path("/tmp/p"), toml_path=None,
        )
        runner._maybe_reload_command()
        self.assertEqual(runner._command, ("foo",))
        self.assertEqual(runner._last_toml_mtime, 0.0)

    def test_maybe_reload_noop_when_mtime_unchanged(self):
        """A toml that exists but hasn't changed since the last poll
        is a no-op. Two back-to-back calls must not re-parse."""
        with TemporaryDirectory() as td:
            toml = Path(td) / "lsp-engine.toml"
            toml.write_text(
                '[compile_aware]\nenabled = true\n'
                '[compile_aware.commands]\npy = ["bare-cmd"]\n',
                encoding="utf-8",
            )
            spec = CompileSpec(language="py", command=("orig",))
            runner = CompileRunner(
                spec=spec, project_root=Path(td), toml_path=toml,
            )
            runner._maybe_reload_command()
            first_mtime = runner._last_toml_mtime
            self.assertGreater(first_mtime, 0)
            self.assertEqual(runner._command, ("bare-cmd",))
            # Second call without a mtime change: should not re-parse.
            runner._maybe_reload_command()
            self.assertEqual(runner._last_toml_mtime, first_mtime)
            self.assertEqual(runner._command, ("bare-cmd",))

    def test_maybe_reload_replaces_command_on_mtime_change(self):
        """The headline fix: write a toml with command A, run the
        reload, write a toml with command B (bumping mtime),
        re-run, assert the runner now holds command B. Same flow
        the live pandorum daemon goes through when the user edits
        the toml after the daemon has been running.
        """
        with TemporaryDirectory() as td:
            toml = Path(td) / "lsp-engine.toml"
            toml.write_text(
                '[compile_aware]\nenabled = true\n'
                '[compile_aware.commands]\ncs = ["msbuild"]\n',
                encoding="utf-8",
            )
            spec = CompileSpec(language="cs", command=("original",))
            runner = CompileRunner(
                spec=spec, project_root=Path(td), toml_path=toml,
            )
            runner._maybe_reload_command()
            self.assertEqual(runner._command, ("msbuild",))

            # Edit the toml to use a full-path msbuild. Bump mtime
            # explicitly so the test doesn't depend on FS resolution.
            # TOML literal-string syntax (single-quote, no escapes)
            # lets us drop the Windows path in verbatim — using
            # Python repr would emit a single-quoted TOML literal
            # with ``\\`` runs that TOML reads as literal double
            # backslashes (basic strings vs literals are different
            # escape modes in the spec).
            time.sleep(0.01)
            full_path = (
                r"C:\Program Files\Microsoft Visual Studio\2022"
                r"\Community\MSBuild\Current\Bin\MSBuild.exe"
            )
            toml.write_text(
                "[compile_aware]\nenabled = true\n"
                "[compile_aware.commands]\n"
                f"cs = ['{full_path}', '/nologo']\n",
                encoding="utf-8",
            )
            future = runner._last_toml_mtime + 5.0
            os.utime(toml, (future, future))

            runner._maybe_reload_command()
            self.assertEqual(runner._command[0], full_path)
            self.assertEqual(runner._command[1], "/nologo")

    def test_maybe_reload_keeps_previous_command_on_invalid_toml(self):
        """A toml edit that produces invalid TOML must not blank the
        command — the previous one keeps working until the user fixes
        the file. The mtime is still advanced so we don't re-parse the
        broken file on every run.
        """
        with TemporaryDirectory() as td:
            toml = Path(td) / "lsp-engine.toml"
            toml.write_text(
                '[compile_aware]\nenabled = true\n'
                '[compile_aware.commands]\npy = ["cmd-a"]\n',
                encoding="utf-8",
            )
            spec = CompileSpec(language="py", command=("orig",))
            runner = CompileRunner(
                spec=spec, project_root=Path(td), toml_path=toml,
            )
            runner._maybe_reload_command()
            self.assertEqual(runner._command, ("cmd-a",))

            # Break the toml.
            time.sleep(0.01)
            toml.write_text("not valid TOML [[[", encoding="utf-8")
            future = runner._last_toml_mtime + 5.0
            os.utime(toml, (future, future))

            runner._maybe_reload_command()
            # Previous command preserved.
            self.assertEqual(runner._command, ("cmd-a",))
            # Mtime advanced so we don't retry the broken file on
            # every call.
            self.assertEqual(runner._last_toml_mtime, future)

    def test_run_once_calls_maybe_reload_command(self):
        """Source-inspection regression guard: ``_run_once`` must
        invoke ``_maybe_reload_command`` so the daemon picks up
        toml edits without a restart. Skipping the call recreates
        the pandorum bug.
        """
        import inspect
        src = inspect.getsource(CompileRunner._run_once)
        self.assertIn("_maybe_reload_command", src, (
            "_run_once no longer invokes _maybe_reload_command — "
            "the pandorum toml-cache bug will recur. "
            "Restore the call at the top of _run_once."
        ))

    def test_run_once_uses_current_command_not_spec_command(self):
        """Source-inspection regression guard: ``_run_once`` reads
        from ``self._command`` (mutable, reload-aware) rather than
        ``self.spec.command`` (frozen, set at startup). Switching
        back to the latter would silently re-introduce the bug.
        """
        import inspect
        src = inspect.getsource(CompileRunner._run_once)
        # The execution path passes ``list(cmd)`` to subprocess.run
        # where ``cmd = self._command``. Pre-fix it was
        # ``list(self.spec.command)`` straight from the frozen spec.
        self.assertIn("cmd = self._command", src, (
            "_run_once no longer reads from self._command. Pre-v1.10.4 "
            "it read from self.spec.command directly, which is frozen "
            "at daemon startup and immune to toml edits."
        ))

    def test_orchestrator_forwards_toml_path_to_runners(self):
        """``CompileOrchestrator.from_engine_config`` must pass
        ``toml_path`` through to every runner; otherwise the
        runners never get the hot-reload trigger."""
        with TemporaryDirectory() as td:
            toml = Path(td) / "lsp-engine.toml"
            orch = CompileOrchestrator.from_engine_config(
                td, {"py": ("foo",)}, toml_path=toml,
            )
            runners = orch.runners()
            self.assertIn("py", runners)
            self.assertEqual(runners["py"].toml_path, toml)


# ──────────────────────────────────────────────────────────────────────
# Fix E — .cmd shim cwd resolution for --project .
# ──────────────────────────────────────────────────────────────────────


class TestResolveUserProject(unittest.TestCase):
    """``_resolve_user_project`` must resolve a non-absolute
    ``--project`` against the caller's pre-cd cwd, not the cwd
    the Windows shim left python in."""

    def test_absolute_path_unchanged(self):
        from claude_hooks.lsp_engine.__main__ import _resolve_user_project
        if os.name == "nt":
            abs_path = r"C:\Users\manni\source\repos\X"
        else:
            abs_path = "/srv/projects/X"
        self.assertEqual(_resolve_user_project(abs_path), abs_path)

    def test_relative_resolves_against_env_var(self):
        """When ``CLAUDE_HOOKS_USER_CWD`` is set (Windows .cmd shim
        path), a relative ``--project`` resolves against that, not
        ``os.getcwd()``. This is the load-bearing case for the .cmd
        shim — without the env var, the bug recurs."""
        from claude_hooks.lsp_engine.__main__ import _resolve_user_project
        fake_cwd = os.path.join(os.sep, "tmp", "fake-user-cwd")
        with patch.dict(os.environ, {"CLAUDE_HOOKS_USER_CWD": fake_cwd}):
            out = _resolve_user_project(".")
            self.assertEqual(out, os.path.normpath(fake_cwd))

    def test_relative_falls_back_to_getcwd(self):
        """Direct ``python -m claude_hooks.lsp_engine`` calls don't
        set the env var; ``os.getcwd()`` is the right fallback."""
        from claude_hooks.lsp_engine.__main__ import _resolve_user_project
        # Make sure env var is absent.
        env = {k: v for k, v in os.environ.items()
               if k != "CLAUDE_HOOKS_USER_CWD"}
        with patch.dict(os.environ, env, clear=True), \
             patch("os.getcwd", return_value=os.path.join(os.sep, "tmp", "x")):
            out = _resolve_user_project(".")
            self.assertEqual(out, os.path.normpath(os.path.join(os.sep, "tmp", "x")))

    def test_relative_subdir_joined_with_env_cwd(self):
        """A relative ``--project`` other than ``.`` (e.g.
        ``./sub`` or ``../other``) joins onto the env-var cwd, then
        normpath collapses any ``..``."""
        from claude_hooks.lsp_engine.__main__ import _resolve_user_project
        fake = os.path.join(os.sep, "tmp", "outer", "inner")
        with patch.dict(os.environ, {"CLAUDE_HOOKS_USER_CWD": fake}):
            up = _resolve_user_project("..")
            self.assertEqual(up, os.path.normpath(os.path.join(fake, "..")))

    def test_main_applies_resolve_user_project(self):
        """Source-inspection guard: ``main()`` must run the
        ``_resolve_user_project`` normalisation on ``args.project``
        before dispatch. Skipping this step reintroduces the bug."""
        import inspect
        from claude_hooks.lsp_engine.__main__ import main
        src = inspect.getsource(main)
        self.assertIn("_resolve_user_project", src, (
            "main() no longer normalises args.project via "
            "_resolve_user_project — relative --project values from "
            "the Windows .cmd shim will silently resolve to the wrong "
            "project hash."
        ))


class TestCmdShimUserCwdExport(unittest.TestCase):
    """The Windows ``.cmd`` shim must capture the caller's ``%CD%``
    into ``CLAUDE_HOOKS_USER_CWD`` *before* ``cd`` -ing into the
    repo root. Source-inspection because we can't exercise the
    ``.cmd`` shim from Linux."""

    def test_cmd_shim_captures_user_cwd(self):
        cmd = HERE / "bin" / "claude-hooks-lsp.cmd"
        text = cmd.read_text(encoding="utf-8")
        # Order matters — the capture has to happen BEFORE the cd
        # that loses the original cwd.
        capture_idx = text.find("CLAUDE_HOOKS_USER_CWD=%CD%")
        cd_idx = text.find('cd /d "%REPO%"')
        self.assertGreater(capture_idx, -1, (
            "claude-hooks-lsp.cmd no longer exports "
            "CLAUDE_HOOKS_USER_CWD — relative --project paths will "
            "silently resolve to the repo dir, not the user's cwd."
        ))
        self.assertGreater(cd_idx, -1)
        self.assertLess(capture_idx, cd_idx, (
            "CLAUDE_HOOKS_USER_CWD is exported AFTER the cd into "
            "the repo — the captured cwd is wrong and the fix is "
            "no-op. Move the set line above the cd."
        ))


# ──────────────────────────────────────────────────────────────────────
# Fix C — restart subcommand
# ──────────────────────────────────────────────────────────────────────


class TestRestartSubcommand(unittest.TestCase):
    """``claude-hooks-lsp restart --project X`` must compose
    ``stop`` + ``cleanup --force``, returning 0 on the happy path
    even when no daemon was running (idempotent)."""

    def test_parser_accepts_restart(self):
        from claude_hooks.lsp_engine.__main__ import _build_parser
        args = _build_parser().parse_args(
            ["restart", "--project", "/tmp/X"],
        )
        self.assertEqual(args.subcommand, "restart")
        self.assertEqual(args.project, "/tmp/X")

    def test_main_dispatches_restart(self):
        """Source-inspection: ``main()`` must dispatch the
        ``restart`` subcommand to ``_run_restart``."""
        import inspect
        from claude_hooks.lsp_engine.__main__ import main
        src = inspect.getsource(main)
        self.assertIn('args.subcommand == "restart"', src)
        self.assertIn("_run_restart(args)", src)

    @unittest.skipIf(os.name == "nt", "Windows parity is Phase 4")
    def test_restart_when_no_daemon_running_succeeds(self):
        """Idempotent path: no daemon to stop, no state dir to
        clean — restart still returns 0 with a clear message."""
        from claude_hooks.lsp_engine.__main__ import _run_restart
        with TemporaryDirectory() as td:
            tdp = Path(td)
            project = tdp / "project"
            project.mkdir()
            state = tdp / "state"
            args = argparse.Namespace(
                project=str(project), state_base=str(state),
            )
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                rc = _run_restart(args)
            self.assertEqual(rc, 0)
            out = json.loads(buf.getvalue().splitlines()[-1])
            self.assertTrue(out["restarted"])

    @unittest.skipIf(os.name == "nt", "Windows parity is Phase 4")
    def test_restart_against_live_daemon(self):
        """Full restart against a real daemon: spin one up, call
        restart, assert the daemon shut down AND the state dir is
        gone (so the next spawn comes up fresh and re-reads toml).
        """
        from claude_hooks.lsp_engine.__main__ import _run_restart
        from claude_hooks.lsp_engine.daemon import project_dir
        from claude_hooks.lsp_engine.ipc import _is_socket_alive

        with TemporaryDirectory() as td:
            tdp = Path(td)
            project = tdp / "project"
            project.mkdir()
            state = tdp / "state"
            daemon = Daemon(
                project_root=project,
                servers=[_fake_spec()],
                engine_config=_short_lock_config(),
                state_base=state,
            )
            daemon.start()
            try:
                sock = socket_path_for(project, base=state)
                self.assertTrue(_is_socket_alive(sock))
                pdir = project_dir(project, base=state)
                self.assertTrue(pdir.exists())

                args = argparse.Namespace(
                    project=str(project), state_base=str(state),
                )
                buf = io.StringIO()
                with patch("sys.stdout", buf):
                    rc = _run_restart(args)
                self.assertEqual(rc, 0)

                # Daemon shut down + state dir removed.
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    if not _is_socket_alive(sock) and not pdir.exists():
                        break
                    time.sleep(0.05)
                self.assertFalse(_is_socket_alive(sock))
                self.assertFalse(pdir.exists())
            finally:
                try:
                    daemon.stop()
                except Exception:
                    pass


# ──────────────────────────────────────────────────────────────────────
# Fix D — pid in alive status + WinError 2 hint
# ──────────────────────────────────────────────────────────────────────


class TestStatusReportsDaemonPid(unittest.TestCase):
    """The daemon's ``status`` RPC must include ``pid: os.getpid()``
    so the CLI surfaces the live PID on Windows where the lock
    file is unreadable from outside the daemon."""

    def test_op_status_source_includes_pid(self):
        """Source-inspection: ``_op_status`` must put ``os.getpid()``
        in its response. Lock-file reads are blocked on Windows by
        ``msvcrt.locking``, so the daemon is the only authority."""
        import inspect
        from claude_hooks.lsp_engine.daemon import Daemon
        src = inspect.getsource(Daemon._op_status)
        self.assertIn("os.getpid()", src, (
            "Daemon._op_status no longer puts os.getpid() in the "
            "status response — Windows callers will see pid:null "
            "even when the daemon is clearly alive."
        ))

    @unittest.skipIf(os.name == "nt", "Windows parity is Phase 4")
    def test_status_response_carries_daemon_pid(self):
        """End-to-end: call the live daemon's status RPC and assert
        the response carries the daemon's actual PID."""
        from claude_hooks.lsp_engine.client import LspEngineClient
        with TemporaryDirectory() as td:
            tdp = Path(td)
            project = tdp / "project"
            project.mkdir()
            state = tdp / "state"
            daemon = Daemon(
                project_root=project,
                servers=[_fake_spec()],
                engine_config=_short_lock_config(),
                state_base=state,
            )
            daemon.start()
            try:
                sock = socket_path_for(project, base=state)
                client = LspEngineClient(sock, session_id="t")
                client.connect()
                client.attach()
                try:
                    status = client.status()
                finally:
                    client.detach()
                    client.close()
                self.assertIn("pid", status)
                # Daemon runs in the same process as the test
                # (Daemon.start() spawns threads, not subprocesses)
                # so getpid() matches.
                self.assertEqual(status["pid"], os.getpid())
                self.assertIn("compile_aware_languages", status)
            finally:
                daemon.stop()

    def test_status_cli_uses_daemon_pid_via_setdefault(self):
        """Source-inspection: ``_run_status`` must prefer the
        daemon-supplied PID over the lock-file PID (which is None on
        Windows when the daemon is alive)."""
        import inspect
        from claude_hooks.lsp_engine.__main__ import _run_status
        src = inspect.getsource(_run_status)
        # The current implementation uses ``setdefault`` to keep
        # daemon-reported PID and only fall back to lock_pid when
        # the daemon response somehow omitted it.
        self.assertIn('info.setdefault("pid", lock_pid)', src, (
            "_run_status no longer prefers the daemon-supplied PID. "
            "Reverting to ``info[\"pid\"] = lock_pid`` would clobber "
            "the daemon's authoritative value with the unreadable "
            "(None) lock-file read on Windows."
        ))


class TestCompileFailedToInvokeMessage(unittest.TestCase):
    """The ``WinError 2 / ENOENT`` log line must point the user at
    ``.claude-hooks/lsp-engine.toml`` so they know where to set an
    absolute path. Pre-v1.10.4 the message was just the raw exception."""

    def test_oserror_log_mentions_toml_and_absolute_path_hint(self):
        import inspect
        from claude_hooks.lsp_engine.compile import CompileRunner
        src = inspect.getsource(CompileRunner._run_once)
        # The hint must mention BOTH the toml path (so the user knows
        # which file to edit) and the absolute-path remedy (so they
        # know what to change to). Anything weaker is just the
        # pre-fix log line in a new font.
        self.assertIn(".claude-hooks/lsp-engine.toml", src, (
            "_run_once OSError handler no longer mentions the toml "
            "path — users will see WinError 2 and not know where to "
            "look to fix it."
        ))
        self.assertIn("absolute", src.lower(), (
            "_run_once OSError handler no longer hints at using an "
            "absolute path — the most common WinError 2 fix on "
            "Windows daemons that run from a stripped-down PATH."
        ))


if __name__ == "__main__":
    unittest.main()
