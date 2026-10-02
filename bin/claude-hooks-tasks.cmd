@echo off
REM claude-hooks-tasks — Windows entry point for the persistent task list.
REM Unlike most .cmd shims this one must NOT cd into the repo: the task
REM list belongs to the project the caller is in. The repo goes on
REM PYTHONPATH instead.

setlocal enabledelayedexpansion
set "REPO=%~dp0.."
pushd "%REPO%" >nul
set "REPO=%CD%"
popd >nul
if defined PYTHONPATH (set "PYTHONPATH=%REPO%;%PYTHONPATH%") else (set "PYTHONPATH=%REPO%")

if defined CLAUDE_HOOKS_PY if exist "%CLAUDE_HOOKS_PY%" (
    "%CLAUDE_HOOKS_PY%" -m claude_hooks.tasks.cli %*
    exit /b !ERRORLEVEL!
)

for %%C in (anaconda3 Anaconda3 miniconda3 Miniconda3) do (
    if exist "%USERPROFILE%\%%C\envs\claude-hooks\python.exe" (
        "%USERPROFILE%\%%C\envs\claude-hooks\python.exe" -m claude_hooks.tasks.cli %*
        exit /b !ERRORLEVEL!
    )
)

where python >NUL 2>&1
if !ERRORLEVEL!==0 (
    python -m claude_hooks.tasks.cli %*
    exit /b !ERRORLEVEL!
)

echo claude-hooks-tasks: no python found 1>&2
exit /b 1
