@echo off
REM claude-advisor — Windows entry point for the /get-advice skill CLI.
REM See bin/claude-advisor (POSIX) for the design notes.

setlocal enabledelayedexpansion
set HERE=%~dp0
set REPO=%HERE%..

cd /d "%REPO%"

if defined CLAUDE_HOOKS_PY if exist "%CLAUDE_HOOKS_PY%" (
    "%CLAUDE_HOOKS_PY%" -m claude_hooks.get_advice.cli %*
    exit /b !ERRORLEVEL!
)

if exist "%REPO%\.venv\bin\python.exe" (
    "%REPO%\.venv\bin\python.exe" -m claude_hooks.get_advice.cli %*
    exit /b !ERRORLEVEL!
)
if exist "%REPO%\.venv\Scripts\python.exe" (
    "%REPO%\.venv\Scripts\python.exe" -m claude_hooks.get_advice.cli %*
    exit /b !ERRORLEVEL!
)

for %%C in (anaconda3 Anaconda3 miniconda3 Miniconda3) do (
    if exist "%USERPROFILE%\%%C\envs\claude-hooks\python.exe" (
        "%USERPROFILE%\%%C\envs\claude-hooks\python.exe" -m claude_hooks.get_advice.cli %*
        exit /b !ERRORLEVEL!
    )
)

where python >NUL 2>&1
if !ERRORLEVEL!==0 (
    python -m claude_hooks.get_advice.cli %*
    exit /b !ERRORLEVEL!
)

echo claude-advisor: no python found 1>&2
exit /b 1
