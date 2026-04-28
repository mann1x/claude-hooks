@echo off
REM claude-hooks-daemon-ctl — Windows entry point for daemon control.
REM See bin/claude-hooks-daemon-ctl (POSIX) for the design notes.

setlocal enabledelayedexpansion
set HERE=%~dp0
set REPO=%HERE%..

REM Switch into the repo root so `python -m claude_hooks.daemon_ctl` finds
REM the package via cwd-on-sys.path. Same Task Scheduler / cmd.exe
REM cwd-elsewhere problem the daemon shim solves; reuse the fix.
cd /d "%REPO%"

if defined CLAUDE_HOOKS_PY if exist "%CLAUDE_HOOKS_PY%" (
    "%CLAUDE_HOOKS_PY%" -m claude_hooks.daemon_ctl %*
    exit /b !ERRORLEVEL!
)

if exist "%REPO%\.venv\bin\python.exe" (
    "%REPO%\.venv\bin\python.exe" -m claude_hooks.daemon_ctl %*
    exit /b !ERRORLEVEL!
)
if exist "%REPO%\.venv\Scripts\python.exe" (
    "%REPO%\.venv\Scripts\python.exe" -m claude_hooks.daemon_ctl %*
    exit /b !ERRORLEVEL!
)

for %%C in (anaconda3 Anaconda3 miniconda3 Miniconda3) do (
    if exist "%USERPROFILE%\%%C\envs\claude-hooks\python.exe" (
        "%USERPROFILE%\%%C\envs\claude-hooks\python.exe" -m claude_hooks.daemon_ctl %*
        exit /b !ERRORLEVEL!
    )
)

where python >NUL 2>&1
if !ERRORLEVEL!==0 (
    python -m claude_hooks.daemon_ctl %*
    exit /b !ERRORLEVEL!
)

echo claude-hooks-daemon-ctl: no python found 1>&2
exit /b 1
