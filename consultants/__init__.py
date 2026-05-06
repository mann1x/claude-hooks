"""``consultants`` — agentic-engine consultation service for claude-hooks.

A self-hostable LangGraph + LangServe service that runs a council of
specialist agents (planner / researcher / critic / synthesizer) over
the same Ollama proxy claude-hooks already uses for HyDE, Caliber, and
``/get-advice``. Driven from inside a Claude Code session via the
``/consultants`` skill.

This package is intentionally **separate from ``claude_hooks``** — it
has heavy dependencies (LangChain, LangGraph, FastAPI, uvicorn) that
the stdlib-only claude-hooks core must not pull in. ``install.py``
manages a dedicated venv at ``consultants/.venv`` for it.

Storage and session-index helpers are deliberately stdlib-only so they
can be exercised by the main test suite (``tests/test_consultants_*.py``)
without installing the heavy deps. The engine + server modules import
the heavy deps lazily.
"""

__version__ = "0.1.0"
__all__ = ["__version__"]


# ---- claude_hooks bridge --------------------------------------------- #
# The consultants engine reuses several modules from ``claude_hooks``:
# ``claude_hooks.get_advice.chat_client.ChatClient`` for retry-hardened
# Ollama I/O, ``claude_hooks.agent_loop.runner.run_loop`` for the
# researcher's tool sub-loop, and
# ``claude_hooks.caliber_proxy.{tools,prompt}`` for the researcher's
# tool surface and grounding messages. claude_hooks lives in the *main*
# claude-hooks conda env which is **separate** from the
# claude-hooks-consultants env that this package runs in — so
# ``import claude_hooks`` would fail at runtime without help.
#
# Instead of pip-installing claude_hooks into the consultants env (which
# would force the heavy LangChain stack into the main env's reach when
# claude-hooks updates the shared bits) we add the repo root to
# ``sys.path``. The repo layout is::
#
#     <repo>/
#         claude_hooks/      <- stdlib-only main package
#         consultants/       <- this package
#
# so ``parent.parent`` of this file is the repo root that should be on
# the path. A quick existence check on the sibling ``claude_hooks/``
# directory keeps us from inserting random paths in pathological dev
# layouts (e.g. an editable install from a build dir).
#
# This runs once at first ``import consultants`` and is a no-op if the
# repo is already on the path (e.g. tests or when claude_hooks is
# pip-installed in the same env).

def _bootstrap_claude_hooks_path() -> None:
    import os
    import sys
    from pathlib import Path

    here = Path(__file__).resolve()
    repo_root = here.parent.parent
    if not (repo_root / "claude_hooks" / "__init__.py").exists():
        return  # not a recognisable claude-hooks repo layout
    repo_str = str(repo_root)
    if repo_str in sys.path:
        return
    # Honour an explicit opt-out (e.g. tests that want a clean import
    # surface to validate fail modes).
    if os.environ.get("CONSULTANTS_NO_BOOTSTRAP") == "1":
        return
    sys.path.insert(0, repo_str)


_bootstrap_claude_hooks_path()
del _bootstrap_claude_hooks_path
