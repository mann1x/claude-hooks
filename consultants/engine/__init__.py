"""Council engine — LangGraph state machine + on-disk storage helpers.

The storage helpers (``storage``, ``sessions_index``) are stdlib-only
so they can be unit-tested without installing LangChain. ``council``
imports LangGraph and is therefore only importable inside the
consultants venv.
"""
