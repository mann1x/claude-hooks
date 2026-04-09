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

REM Prefer conda env python if it exists, then py launcher, then python.
if exist "%USERPROFILE%\anaconda3\envs\claude-hooks\python.exe" (
    "%USERPROFILE%\anaconda3\envs\claude-hooks\python.exe" "%REPO%\run.py" %*
    exit /b 0
)

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
