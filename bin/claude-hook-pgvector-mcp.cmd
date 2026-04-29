@echo off
REM claude-hook-pgvector-mcp.cmd - Windows entry point for the pgvector
REM MCP stdio server. Spawned by Claude Code as an mcpServers entry —
REM JSON-RPC on stdin/stdout for the lifetime of the session.

setlocal

set HERE=%~dp0
set REPO=%HERE%..

if defined CLAUDE_HOOKS_PY if exist "%CLAUDE_HOOKS_PY%" (
    cd /d "%REPO%"
    "%CLAUDE_HOOKS_PY%" -m claude_hooks.pgvector_mcp %*
    exit /b 0
)

if exist "%REPO%\.venv\bin\python.exe" (
    cd /d "%REPO%"
    "%REPO%\.venv\bin\python.exe" -m claude_hooks.pgvector_mcp %*
    exit /b 0
)
if exist "%REPO%\.venv\Scripts\python.exe" (
    cd /d "%REPO%"
    "%REPO%\.venv\Scripts\python.exe" -m claude_hooks.pgvector_mcp %*
    exit /b 0
)

for %%C in (anaconda3 Anaconda3 miniconda3 Miniconda3) do (
    if exist "%USERPROFILE%\%%C\envs\claude-hooks\python.exe" (
        cd /d "%REPO%"
        "%USERPROFILE%\%%C\envs\claude-hooks\python.exe" -m claude_hooks.pgvector_mcp %*
        exit /b 0
    )
)

where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    cd /d "%REPO%"
    py -3 -m claude_hooks.pgvector_mcp %*
    exit /b 0
)

where python >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    cd /d "%REPO%"
    python -m claude_hooks.pgvector_mcp %*
    exit /b 0
)

exit /b 0
