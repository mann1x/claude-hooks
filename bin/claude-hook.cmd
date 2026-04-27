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

REM Repo-local venv (Windows layout).
if exist "%REPO%\.venv\Scripts\python.exe" (
    "%REPO%\.venv\Scripts\python.exe" "%REPO%\run.py" %*
    exit /b 0
)

REM Conda env (anaconda3 / miniconda3).
if exist "%USERPROFILE%\anaconda3\envs\claude-hooks\python.exe" (
    "%USERPROFILE%\anaconda3\envs\claude-hooks\python.exe" "%REPO%\run.py" %*
    exit /b 0
)
if exist "%USERPROFILE%\miniconda3\envs\claude-hooks\python.exe" (
    "%USERPROFILE%\miniconda3\envs\claude-hooks\python.exe" "%REPO%\run.py" %*
    exit /b 0
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
