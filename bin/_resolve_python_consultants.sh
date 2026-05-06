# claude-hooks-consultants Python resolver — sourced by
# bin/claude-consultants. Resolves to the **dedicated**
# ``claude-hooks-consultants`` conda env's Python — and ONLY that
# env. No fallback to system Python or the main ``claude-hooks``
# env, because the consultants stack (LangGraph + LangServe +
# friends) is intentionally isolated. See
# memory/feedback_conda_first_design.md for the rationale.
#
# Override with ``CLAUDE_CONSULTANTS_PY`` to pin a specific
# interpreter — the probe is skipped entirely.
#
# Caller contract:
#   - Reads ``PY`` after sourcing.
#   - ``PY`` is empty when no interpreter was found; the caller
#     prints a clear error message pointing at ``python install.py``
#     and exits non-zero.

if [ -n "${CLAUDE_CONSULTANTS_PY:-}" ] && [ -x "$CLAUDE_CONSULTANTS_PY" ]; then
    PY="$CLAUDE_CONSULTANTS_PY"
    return 0 2>/dev/null || exit 0
fi

PY=""
for _ch_cand in \
    "$HOME/anaconda3/envs/claude-hooks-consultants/bin/python" \
    "$HOME/anaconda3/envs/claude-hooks-consultants/bin/python.exe" \
    "$HOME/anaconda3/envs/claude-hooks-consultants/Scripts/python.exe" \
    "$HOME/anaconda3/envs/claude-hooks-consultants/python.exe" \
    "$HOME/miniconda3/envs/claude-hooks-consultants/bin/python" \
    "$HOME/miniconda3/envs/claude-hooks-consultants/bin/python.exe" \
    "$HOME/miniconda3/envs/claude-hooks-consultants/Scripts/python.exe" \
    "$HOME/miniconda3/envs/claude-hooks-consultants/python.exe" \
    "$HOME/Miniconda3/envs/claude-hooks-consultants/python.exe" \
    "$HOME/Anaconda3/envs/claude-hooks-consultants/python.exe"
do
    if [ -x "$_ch_cand" ]; then
        PY="$_ch_cand"
        break
    fi
done
unset _ch_cand
