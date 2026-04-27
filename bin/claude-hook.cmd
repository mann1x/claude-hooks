@echo off
REM claude-hook.cmd - Windows entry point for claude-hooks
REM
REM Usage:
REM     claude-hook <EventName>
REM
REM Reads the hook event JSON from stdin, dispatches to the matching
REM handler, and writes any response to stdout. Always exits 0 so a
REM broken hook never blocks Claude Code.

setlocal

set HERE=%~dp0
set REPO=%HERE%..

REM CLAUDE_HOOKS_PY override wins.
if defined CLAUDE_HOOKS_PY if exist "%CLAUDE_HOOKS_PY%" (
    "%CLAUDE_HOOKS_PY%" "%REPO%\run.py" %*
    exit /b 0
)

REM Repo-local venv (MSYS2 hybrid layout: bin/python.exe).
if exist "%REPO%\.venv\bin\python.exe" (
    "%REPO%\.venv\bin\python.exe" "%REPO%\run.py" %*
    exit /b 0
)
REM Repo-local venv (native Windows layout: Scripts/python.exe).
if exist "%REPO%\.venv\Scripts\python.exe" (
    "%REPO%\.venv\Scripts\python.exe" "%REPO%\run.py" %*
    exit /b 0
)

REM Conda env — Windows layout puts python.exe directly in the env
REM root. Try both anaconda3 and miniconda3 (case-insensitive on NTFS,
REM but list both spellings for clarity).
for %%C in (anaconda3 Anaconda3 miniconda3 Miniconda3) do (
    if exist "%USERPROFILE%\%%C\envs\claude-hooks\python.exe" (
        "%USERPROFILE%\%%C\envs\claude-hooks\python.exe" "%REPO%\run.py" %*
        exit /b 0
    )
)

REM System python (py launcher first, then python on PATH).
where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    py -3 "%REPO%\run.py" %*
    exit /b 0
)

where python >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    python "%REPO%\run.py" %*
    exit /b 0
)

REM No python found - exit silently.
exit /b 0
