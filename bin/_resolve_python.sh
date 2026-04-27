# claude-hooks Python resolver — sourced by every bin/* shim.
#
# Sets ``PY`` to the first runnable Python interpreter found, in this
# preference order:
#
#   1. Repo-local venv  ``$REPO/.venv/bin/python`` (POSIX layout)
#   2. Repo-local venv  ``$REPO/.venv/Scripts/python.exe`` (Windows
#      layout, sourced from MSYS / Git-Bash where ``-x`` works)
#   3. Conda env        ``$HOME/{anaconda3,miniconda3}/envs/claude-hooks/bin/python``
#   4. Conda env Win    ``…/envs/claude-hooks/Scripts/python.exe``
#   5. System ``python3`` / ``python`` from ``$PATH``
#
# Override with ``CLAUDE_HOOKS_PY`` to pin a specific interpreter — the
# probe is skipped entirely.
#
# Caller contract:
#   - Sets ``REPO`` before sourcing (absolute path to repo root).
#   - Reads ``PY`` after sourcing. ``PY`` is empty when no interpreter
#     was found; the caller decides what to do (silent exit for hooks,
#     loud error for CLIs).

if [ -n "${CLAUDE_HOOKS_PY:-}" ] && [ -x "$CLAUDE_HOOKS_PY" ]; then
    PY="$CLAUDE_HOOKS_PY"
    return 0 2>/dev/null || exit 0
fi

PY=""
for _ch_cand in \
    "${REPO:-}/.venv/bin/python" \
    "${REPO:-}/.venv/Scripts/python.exe" \
    "$HOME/anaconda3/envs/claude-hooks/bin/python" \
    "$HOME/anaconda3/envs/claude-hooks/Scripts/python.exe" \
    "$HOME/miniconda3/envs/claude-hooks/bin/python" \
    "$HOME/miniconda3/envs/claude-hooks/Scripts/python.exe"
do
    if [ -x "$_ch_cand" ]; then
        PY="$_ch_cand"
        break
    fi
done
unset _ch_cand

if [ -z "$PY" ]; then
    if command -v python3 >/dev/null 2>&1; then
        PY=python3
    elif command -v python >/dev/null 2>&1; then
        PY=python
    fi
fi
