# Contributing to claude-hooks

## Running tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

## Adding a new provider

1. Create `claude_hooks/providers/<name>.py` implementing the `Provider` ABC from `base.py`
2. Add it to `claude_hooks/providers/__init__.py` `REGISTRY`
3. Implement the 4 required methods: `detect`, `verify`, `recall`, `store`
4. Add tests in `tests/test_providers.py`
5. Re-run `python3 install.py` to test auto-detection

## Coding conventions

- **Stdlib only** for core modules. Optional dependencies (psycopg, sqlite-vec) must be lazy-imported inside methods, gated by try/except ImportError.
- **Never block Claude Code**. Every handler must catch all exceptions and return gracefully. Hooks exit 0 on every code path.
- **Python 3.9+**. Use `from __future__ import annotations` in every file for forward-compatible type hints.
- No pip dependencies for the default Qdrant + Memory KG path.

## PR process

1. Fork the repo
2. Create a feature branch
3. Ensure all tests pass (`python -m pytest tests/ -v`)
4. Submit a PR with a clear description of what and why
