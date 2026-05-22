@echo off
REM claude-hooks-lsp — Windows entry point for the LSP engine CLI.
REM See bin/claude-hooks-lsp (POSIX) for the design notes.

setlocal enabledelayedexpansion
set HERE=%~dp0
set REPO=%HERE%..

REM v1.10.4: capture the caller's cwd BEFORE cd-ing into the repo so
REM `claude-hooks-lsp <subcmd> --project .` resolves the relative path
REM against the directory the user was actually in. Pre-fix the shim
REM cd'd into the repo first, then python ran `Path(args.project)
REM .resolve()` against the *repo's* cwd — so `--project .` silently
REM produced a hash for the claude-hooks repo, not the user's project.
REM `__main__.py` reads this var and resolves a non-absolute --project
REM against it, falling back to os.getcwd() when unset (POSIX-shim path).
set "CLAUDE_HOOKS_USER_CWD=%CD%"

REM Switch into the repo root so `python -m claude_hooks.lsp_engine` finds
REM the package via cwd-on-sys.path. The POSIX shim achieves the same via
REM PYTHONPATH exported in _resolve_python.sh; on Windows the cd-into-repo
REM approach is what every other .cmd shim already uses, and it works
REM without pip-installing the package.
cd /d "%REPO%"

REM v1.10.3: also export PYTHONPATH so nested Python invocations
REM (e.g. a daemon spawn from `claude-hooks-lsp status` that triggers
REM `connect_or_spawn` → Popen with a fresh env, or anything launched
REM via PowerShell ``Start-Process`` that bypasses the shim's cwd)
REM still import ``claude_hooks`` cleanly. Without this, downstream
REM Python invocations from outside the shim's cwd hit
REM ``ModuleNotFoundError: No module named 'claude_hooks'``. The
REM ``setlocal`` at the top scopes this export to the shim's lifetime
REM — the caller's PYTHONPATH is restored on exit.
if defined PYTHONPATH (
    set "PYTHONPATH=%REPO%;%PYTHONPATH%"
) else (
    set "PYTHONPATH=%REPO%"
)

if defined CLAUDE_HOOKS_PY if exist "%CLAUDE_HOOKS_PY%" (
    "%CLAUDE_HOOKS_PY%" -m claude_hooks.lsp_engine %*
    exit /b !ERRORLEVEL!
)

if exist "%REPO%\.venv\bin\python.exe" (
    "%REPO%\.venv\bin\python.exe" -m claude_hooks.lsp_engine %*
    exit /b !ERRORLEVEL!
)
if exist "%REPO%\.venv\Scripts\python.exe" (
    "%REPO%\.venv\Scripts\python.exe" -m claude_hooks.lsp_engine %*
    exit /b !ERRORLEVEL!
)

for %%C in (anaconda3 Anaconda3 miniconda3 Miniconda3) do (
    if exist "%USERPROFILE%\%%C\envs\claude-hooks\python.exe" (
        "%USERPROFILE%\%%C\envs\claude-hooks\python.exe" -m claude_hooks.lsp_engine %*
        exit /b !ERRORLEVEL!
    )
)

REM Fall back to whatever python.exe is first on PATH.
python -m claude_hooks.lsp_engine %*
exit /b !ERRORLEVEL!
