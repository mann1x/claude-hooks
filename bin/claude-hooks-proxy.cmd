@echo off
REM claude-hooks-proxy — Windows entry point for the local HTTP proxy.

setlocal
set HERE=%~dp0
set REPO=%HERE%..

set CONDA_PY=%USERPROFILE%\anaconda3\envs\claude-hooks\python.exe
if exist "%CONDA_PY%" (
    "%CONDA_PY%" -m claude_hooks.proxy %*
    exit /b %ERRORLEVEL%
)

where python >NUL 2>&1
if %ERRORLEVEL%==0 (
    python -m claude_hooks.proxy %*
    exit /b %ERRORLEVEL%
)

echo claude-hooks-proxy: no python found 1>&2
exit /b 1
