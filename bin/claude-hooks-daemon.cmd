@echo off
REM claude-hooks-daemon — Windows entry point for the long-lived hook executor.
REM See bin/claude-hooks-daemon (POSIX) for the design notes.

setlocal
set HERE=%~dp0
set REPO=%HERE%..

if defined CLAUDE_HOOKS_PY if exist "%CLAUDE_HOOKS_PY%" (
    "%CLAUDE_HOOKS_PY%" -m claude_hooks.daemon %*
    exit /b %ERRORLEVEL%
)

if exist "%REPO%\.venv\bin\python.exe" (
    "%REPO%\.venv\bin\python.exe" -m claude_hooks.daemon %*
    exit /b %ERRORLEVEL%
)
if exist "%REPO%\.venv\Scripts\python.exe" (
    "%REPO%\.venv\Scripts\python.exe" -m claude_hooks.daemon %*
    exit /b %ERRORLEVEL%
)

for %%C in (anaconda3 Anaconda3 miniconda3 Miniconda3) do (
    if exist "%USERPROFILE%\%%C\envs\claude-hooks\python.exe" (
        "%USERPROFILE%\%%C\envs\claude-hooks\python.exe" -m claude_hooks.daemon %*
        exit /b %ERRORLEVEL%
    )
)

where python >NUL 2>&1
if %ERRORLEVEL%==0 (
    python -m claude_hooks.daemon %*
    exit /b %ERRORLEVEL%
)

echo claude-hooks-daemon: no python found 1>&2
exit /b 1
