@echo off
REM claude-hook.cmd - Windows entry point for claude-hooks
REM
REM Usage:
REM     claude-hook <EventName>
REM
REM Reads the hook event JSON from stdin, dispatches to the matching
REM handler, and writes any response to stdout.

setlocal

set HERE=%~dp0
set REPO=%HERE%..

REM Prefer the py launcher (handles multiple installs cleanly), fall back
REM to python on PATH.
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
