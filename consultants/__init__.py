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
