@echo off
REM claude-hooks-lsp — Windows entry point for the LSP engine CLI.
REM See bin/claude-hooks-lsp (POSIX) for the design notes.

setlocal enabledelayedexpansion
set HERE=%~dp0
set REPO=%HERE%..

REM Switch into the repo root so `python -m claude_hooks.lsp_engine` finds
REM the package via cwd-on-sys.path. The POSIX shim achieves the same via
REM PYTHONPATH exported in _resolve_python.sh; on Windows the cd-into-repo
REM approach is what every other .cmd shim already uses, and it works
REM without pip-installing the package.
cd /d "%REPO%"

if defined CLAUDE_HOOKS_PY if exist "%CLAUDE_HOOKS_PY%" (
    "%CLAUDE_HOOKS_PY%" -m claude_hooks.lsp_engine %*
    exit /b !ERRORLEVEL!
)

if exist "%REPO%\.venv\bin\python.exe" (
    "%REPO%\.venv\bin\python.exe" -m claude_hooks.lsp_engine %*
    exit /b !ERRORLEVEL!
)
if exist "%REPO%\.venv\Scripts\python.exe" (
    "%REPO%\.venv\Scripts\python.exe" -m claude_hooks.lsp_engine %*
    exit /b !ERRORLEVEL!
)

for %%C in (anaconda3 Anaconda3 miniconda3 Miniconda3) do (
    if exist "%USERPROFILE%\%%C\envs\claude-hooks\python.exe" (
        "%USERPROFILE%\%%C\envs\claude-hooks\python.exe" -m claude_hooks.lsp_engine %*
        exit /b !ERRORLEVEL!
    )
)

REM Fall back to whatever python.exe is first on PATH.
python -m claude_hooks.lsp_engine %*
exit /b !ERRORLEVEL!
