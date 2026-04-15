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

## Optional PreToolUse / Stop Hooks (opt-in)

- `stop_guard.py` — blocks ownership-dodging / session-quitting phrases on `Stop` events
- `safety_scan.py` + `safety_patterns.py` — `PreToolUse` scanner that asks before running dangerous Bash patterns (matches anywhere, not just prefix)
- `rtk_rewrite.py` — shells out to `rtk` (>=0.23.0) to rewrite verbose `find`/`grep`/`git log` commands; safety scan runs on the rewritten command

All three default to `enabled: false` and no-op silently when their binary or pattern is missing.

## Proxy / Stats

- `claude_hooks/proxy/` — local HTTP proxy + observability (see `docs/proxy.md`)
- `claude_hooks/proxy/metadata.py` — request-body parser: model, session, warmup detection, plus S2 extensions (agent classification, CC version, effort, thinking type, beta features, account UUID)
- `claude_hooks/proxy/sse.py` — SSE stream parser: extracts usage, stop_reason, and S3 thinking-depth metrics (signature bytes, delta count, output tokens, content-block/delta type histograms)
- `claude_hooks/proxy/forwarder.py` — httpx[http2] forwarder; strips `accept-encoding` and pins `identity` so SSE bytes arrive uncompressed for `SseTail` parsing
- `claude_hooks/proxy/stats_db.py` — SQLite rollup for proxy JSONL logs (schema v3: S2 columns + `agent_rollup` table + S3 thinking-metric totals on `daily_rollup`)
- `claude_hooks/proxy/dashboard.py` — read-only stats dashboard on port `38081`; stdlib-only `ThreadingHTTPServer` with JSON API + embedded HTML view
- `scripts/proxy_rollup.py` — CLI to ingest JSONL into `stats.db` and rebuild rollups
- `bin/claude-hooks-rollup` — POSIX shim for `scripts/proxy_rollup.py` (prefers conda env python)
- `systemd/claude-hooks-rollup.timer` — runs rollup every 5 min via systemd
- `systemd/claude-hooks-dashboard.service` — runs the stats dashboard as a systemd service
- systemd unit files under `systemd/` are templates: `__REPO_PATH__` and `__HOME__` are substituted by `install.py` at install time

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

<!-- caliber:managed:pre-commit -->
## Before Committing

**IMPORTANT:** Before every git commit, you MUST ensure Caliber syncs agent configs with the latest code changes.

First, check if the pre-commit hook is already installed:
```bash
grep -q "caliber" .git/hooks/pre-commit 2>/dev/null && echo "hook-active" || echo "no-hook"
```

- If **hook-active**: the hook handles sync automatically — just commit normally. Tell the user: "Caliber will sync your agent configs automatically via the pre-commit hook."
- If **no-hook**: run Caliber manually before committing:
  1. Tell the user: "Caliber: Syncing agent configs with your latest changes..."
  2. Run: `/usr/local/bin/caliber refresh && git add CLAUDE.md .claude/ .cursor/ .cursorrules .github/copilot-instructions.md .github/instructions/ AGENTS.md CALIBER_LEARNINGS.md .agents/ .opencode/ 2>/dev/null`
  3. After it completes, briefly tell the user what Caliber updated. Then proceed with the commit.

If `/usr/local/bin/caliber` is not found, read `.agents/skills/setup-caliber/SKILL.md` and follow its instructions to install Caliber.
<!-- /caliber:managed:pre-commit -->

<!-- caliber:managed:learnings -->
## Session Learnings

Read `CALIBER_LEARNINGS.md` for patterns and anti-patterns learned from previous sessions.
These are auto-extracted from real tool usage — treat them as project-specific rules.
<!-- /caliber:managed:learnings -->

<!-- caliber:managed:sync -->
## Context Sync

This project uses [Caliber](https://github.com/caliber-ai-org/ai-setup) to keep AI agent configs in sync across Claude Code, Cursor, Copilot, and Codex.
Configs update automatically before each commit via `/usr/local/bin/caliber refresh`.
If the pre-commit hook is not set up, read `.agents/skills/setup-caliber/SKILL.md` and follow the setup instructions.
<!-- /caliber:managed:sync -->
