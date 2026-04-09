# claude-hooks

Cross-platform Claude Code hook framework that auto-recalls from Qdrant + Memory KG
on every prompt and stores noteworthy turns back.

## Architecture

- **Entry**: `bin/claude-hook` (POSIX) / `bin/claude-hook.cmd` (Windows) reads event JSON from stdin
- **Dispatcher**: `claude_hooks/dispatcher.py` routes events to handler modules
- **Handlers**: `claude_hooks/hooks/` — one per event (user_prompt_submit, session_start, stop, etc.)
- **Providers**: `claude_hooks/providers/` — memory backends (qdrant, memory_kg, pgvector, sqlite_vec)
- **Config**: `config/claude-hooks.json` (gitignored), deep-merged over defaults in `claude_hooks/config.py`

## v0.2 Intelligence Modules

- `recall.py` — shared recall pipeline with HyDE, decay, progressive disclosure
- `hyde.py` — query expansion via local Ollama (gemma4:e2b)
- `decay.py` — attention decay scoring for recalled memories
- `dedup.py` — near-duplicate detection before store
- `instincts.py` — auto-extracts bug-fix patterns as reusable instinct files
- `reflect.py` — synthesizes recent memories into CLAUDE.md rules
- `consolidate.py` — memory compression and pruning
- `openwolf.py` — reads .wolf/ project data (cerebrum, buglog)

## Key Conventions

- **Stdlib only** for core — no pip dependencies. Ollama calls use `urllib.request`.
- **Never block Claude** — every handler catches exceptions and exits 0.
- **Opt-in features** — all v0.2 features default to off, enabled via config.
- **Conda env** — `claude-hooks` (Python 3.11) preferred by the shell shim.

## Testing

```bash
conda activate claude-hooks
python -m pytest tests/ -v
```
