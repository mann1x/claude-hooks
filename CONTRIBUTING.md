# Contributing to claude-hooks

## Running tests

```bash
pip install -r requirements-dev.txt      # pytest + coverage
python -m pytest tests/ -q               # 549 passed, 16 skipped (~11 s)
coverage run -m pytest tests/            # with branch coverage
coverage report                          # target ≥ 80 %
```

### Test organisation

| Area | Files |
|------|-------|
| Core hooks | `test_handlers.py`, `test_dispatcher.py`, `test_stop_guard.py` |
| Providers | `test_providers.py`, `test_*_integration.py` (skipped without deps) |
| Memory / recall | `test_recall.py`, `test_hyde.py`, `test_embedders.py`, `test_dedup.py`, `test_decay.py` |
| Maintenance skills | `test_instincts.py`, `test_reflect.py`, `test_consolidate.py` |
| Pre-tool-use | `test_pre_tool_use_handler.py`, `test_safety_scan.py`, `test_rtk_rewrite.py` |
| Proxy (P0 – P4) | `test_proxy.py`, `test_proxy_p1.py`, `test_proxy_p3.py`, `test_proxy_coverage.py` |
| Claude-mem ports | `test_claude_mem_ports.py` |
| Scripts | `test_statusline_usage.py`, `test_proxy_stats.py`, `test_status.py` |
| Coverage lift | `test_coverage_phase8.py` |

## Adding a new provider

1. Create `claude_hooks/providers/<name>.py` implementing the `Provider` ABC from `base.py`
2. Add it to `claude_hooks/providers/__init__.py` `REGISTRY`
3. Implement the 4 required methods: `detect`, `verify`, `recall`, `store`
4. Add tests in `tests/test_providers.py`
5. Re-run `python3 install.py` to test auto-detection

## Adding a new hook event

1. Map the Claude Code event name to a handler module in `claude_hooks/dispatcher.py` (`HANDLERS` dict)
2. Create `claude_hooks/hooks/<name>.py` with a `handle(*, event, config, providers)` function
3. Add default config to `DEFAULT_CONFIG` in `claude_hooks/config.py`
4. Add an installer entry in `install.py` so new users get the wiring for free
5. Add integration tests in `tests/test_handlers.py` or a dedicated file

## Coding conventions

- **Stdlib only** for core modules. Optional dependencies (psycopg, sqlite-vec) must be lazy-imported inside methods, gated by `try/except ImportError`.
- **Never block Claude Code.** Every handler must catch all exceptions and return gracefully. Hooks exit 0 on every code path.
- **Python 3.9+.** Use `from __future__ import annotations` in every file for forward-compatible type hints.
- No pip dependencies for the default Qdrant + Memory KG path.
- Scripts in `scripts/` must also exit 0 on every error path — they're wired into statuslines, cron, or systemd where a non-zero exit can break the caller.

## Commit / PR conventions

- Subject line follows the repo's existing style (see `git log --oneline -n 20 main` for examples).
- Longer bodies explain the WHY, mention affected test counts, and link related issues.
- Pre-commit hook runs Caliber to sync agent configs. For doc-only commits, `--no-verify` is acceptable; the maintainers do this routinely for docs.

## PR process

1. Fork the repo
2. Create a feature branch
3. Ensure all tests pass (`python -m pytest tests/ -q`)
4. Aim to keep branch coverage ≥ 80 % overall (`coverage run -m pytest tests/ && coverage report`)
5. Submit a PR with a clear description of what and why
