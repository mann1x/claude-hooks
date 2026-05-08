@echo off
REM claude-consultants — Windows shim for the /consultants engine.
REM Mirrors bin/claude-consultants. Resolves to the dedicated
REM claude-hooks-consultants conda env's python.exe.

setlocal

set "REPO=%~dp0.."
pushd "%REPO%" >nul
set "REPO=%CD%"
popd >nul

set "PY="
if defined CLAUDE_CONSULTANTS_PY (
    if exist "%CLAUDE_CONSULTANTS_PY%" set "PY=%CLAUDE_CONSULTANTS_PY%"
)

if not defined PY (
    for %%C in (
        "%USERPROFILE%\anaconda3\envs\claude-hooks-consultants\python.exe"
        "%USERPROFILE%\anaconda3\envs\claude-hooks-consultants\Scripts\python.exe"
        "%USERPROFILE%\Miniconda3\envs\claude-hooks-consultants\python.exe"
        "%USERPROFILE%\Miniconda3\envs\claude-hooks-consultants\Scripts\python.exe"
        "%USERPROFILE%\Anaconda3\envs\claude-hooks-consultants\python.exe"
    ) do (
        if not defined PY (
            if exist %%C set "PY=%%~C"
        )
    )
)

if not defined PY (
    echo error: claude-hooks-consultants conda env not found. >&2
    echo Run `python install.py` in the claude-hooks repo to set it up. >&2
    exit /b 2
)

"%PY%" -m consultants.cli %*
