# OpenWolf

@.wolf/OPENWOLF.md

This project uses OpenWolf for context management. Read and follow .wolf/OPENWOLF.md every session. Check .wolf/cerebrum.md before generating code. Check .wolf/anatomy.md before reading files.

# claude-hooks

A small, cross-platform (Linux + Windows) hook framework for Claude Code that
auto-injects relevant prior knowledge from **Qdrant** (semantic memory) and the
**Memory KG** (knowledge graph) into every conversation, and writes new
findings back at the end of the turn.

The hooks are pluggable: each memory backend is a *provider*, so adding a new
store (Postgres pgvector, Weaviate, sqlite-vec, …) is one file under
`claude_hooks/providers/`, no changes elsewhere.

> Status: **v1.0.0** — ~1.5k tests pass (run `pytest --collect-only -q | tail -1`
> for the current count). Installer is functional and idempotent. v0.5+ ships
> a transparent `api.anthropic.com` proxy with SQLite rollups, a read-only
> dashboard (port 38081), and the in-stream `stop_phrase_guard` behavior canary.
> v0.6+ adds an in-process AST code-graph + MCP server. v0.7+ adds the LSP
> engine (per-project session-scoped daemon, Windows parity, sub-ms IPC,
> opt-in compile-aware diagnostics) and the PostToolUse ruff hook.

---

## Why this exists

`~/.claude/CLAUDE.md` already tells Claude *"search Qdrant before diving in,
store findings after"*, but that's a hint — not a guarantee. In practice the
recall step gets skipped when the model decides it isn't relevant, and
storage gets skipped when the turn ends quickly. The result is a memory
system that depends on the model remembering to use itself.

Hooks fix that by making the same calls **deterministically**, on every
turn, before the model even sees the prompt. The model still sees a
"recalled context" block in the prompt and decides what to do with it — but
it can't *forget* to look. Same on the way out: every turn that touched
something noteworthy gets a chance to write back, again deterministically.

This mirrors what `openwolf` does for project anatomy / token tracking
(<https://github.com/cytostack/openwolf>) — same hook pattern, different
payload.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│ Claude Code                                                      │
│                                                                  │
│   user prompt ──► [UserPromptSubmit hook] ──► claude-hooks       │
│                          │                         │             │
│                          │                  ┌──────┴──────┐      │
│                          │                  │ providers/  │      │
│                          │                  │  qdrant     │ ──► Qdrant MCP
│                          │                  │  memory_kg  │ ──► Memory KG MCP
│                          │                  └──────┬──────┘      │
│                          ▼                         │             │
│                  additionalContext ◄───────────────┘             │
│                  (injected into the prompt)                      │
│                                                                  │
│   assistant turn ends ──► [Stop hook] ──► claude-hooks           │
│                                  │                               │
│                                  ▼                               │
│                          providers.store(...)                    │
└──────────────────────────────────────────────────────────────────┘
```

### Components

1. **`bin/claude-hook`** (and `bin/claude-hook.cmd` for Windows) — the single
   entry point invoked from Claude Code's `settings.json`. It receives the
   hook event name as `argv[1]`, reads the event JSON from stdin, and
   dispatches to the matching handler under `claude_hooks/hooks/`.

2. **`claude_hooks/dispatcher.py`** — picks the handler for the event,
   loads enabled providers from config, calls `recall()` or `store()`, and
   writes the appropriate JSON response on stdout.

3. **`claude_hooks/providers/`** — one file per memory backend. Each provider
   implements:

   ```python
   class Provider:
       name: str

       @classmethod
       def detect(cls, claude_config: dict) -> list[ServerCandidate]:
           """Scan ~/.claude.json for MCP servers that look like this provider."""

       @classmethod
       def verify(cls, server: ServerCandidate) -> bool:
           """Probe the server, confirm it has the expected tools."""

       def __init__(self, server: ServerCandidate, options: dict): ...

       def recall(self, query: str, k: int) -> list[str]:
           """Return up to k snippets relevant to the query."""

       def store(self, content: str, metadata: dict) -> None:
           """Persist a new memory."""
   ```

4. **`claude_hooks/mcp_client.py`** — minimal Streamable-HTTP MCP client. Does
   `initialize` → `notifications/initialized` → `tools/call` over HTTP. No
   external dependencies; uses `urllib.request` from the stdlib so the whole
   thing runs on a stock Python install.

5. **`claude_hooks/detect.py`** — reads `~/.claude.json` (Linux) or
   `%USERPROFILE%\.claude.json` (Windows), iterates over `mcpServers` (and
   per-project overrides), and asks each provider to identify candidates.
   The installer uses this to wire up the right server URLs without the user
   having to type them.

6. **`install.py`** — cross-platform installer. Detects MCP servers, asks
   the user to confirm matches, verifies them, writes
   `config/claude-hooks.json` with the chosen URLs, and merges hook entries
   into `~/.claude/settings.json`.

---

## Key directories

- `bin/` — POSIX + Windows entry-point shims:
  - Hook dispatcher: `claude-hook`
  - Daemon: `claude-hooks-daemon`, `claude-hooks-daemon-ctl`
  - Proxy stack: `claude-hooks-proxy`, `claude-hooks-dashboard`, `claude-hooks-rollup`
  - Caliber: `caliber-grounding-proxy`, `caliber-smart`
  - System-wide MCP: `claude-hook-pgvector-mcp`
  - Internal: `_resolve_python.sh` (sourced by every shim to find the right Python)
- `claude_hooks/hooks/` — one handler per event (`session_start`, `user_prompt_submit`, `pre_tool_use`, `post_tool_use`, `stop`, `session_end`)
- `claude_hooks/providers/` — one file per memory backend (`qdrant`, `memory_kg`, `pgvector`, `sqlite_vec`)
- `claude_hooks/` — shared recall + memory modules:
  - **Recall pipeline**: `recall.py`, `hyde.py`, `hyde_cache.py`, `decay.py`, `dedup.py`
  - **Stop pipeline**: `instincts.py`, `reflect.py`, `consolidate.py`, `store_async.py` (Tier 1.3 detached store)
  - **Daemon stack**: `daemon.py`, `daemon_client.py`, `daemon_ctl.py` (Tier 3.8 long-lived hook executor)
  - **Concurrency / utility**: `_parallel.py` (provider fan-out), `mcp_client.py`, `embedders.py`
  - **Companion integrations**: `openwolf.py`, `axon_integration.py`, `gitnexus_integration.py`, `companion_integration.py`
  - **Opt-in advisory**: `stop_guard.py`, `safety_scan.py` + `safety_patterns.py`, `rtk_rewrite.py`
  - **Index management**: `claudemem_reindex.py`
- `claude_hooks/code_graph/` — built-in stdlib `ast`-based code graph (`builder.py`, `impact.py`, `mermaid.py`, `inject.py`, `symbol_lookup.py`, `mcp_server.py`, `clustering.py`, …)
- `claude_hooks/lsp_engine/` — session-scoped LSP daemon (`config.py`, `lsp.py`, `engine.py`, `daemon.py`, `ipc.py`, `locks.py`, `preload.py`, `compile.py`, `git_watch.py`, `client.py`)
- `claude_hooks/proxy/` — opt-in HTTP proxy in front of `api.anthropic.com` (`server.py`, `forwarder.py`, `metadata.py`, `stats_db.py`, `dashboard.py`, `sse.py`, `stop_phrase_guard.py`, `ratelimit_state.py`)
- `claude_hooks/caliber_proxy/` — Caliber grounding proxy (`server.py`, `tools.py`, `prompt.py`, `ollama.py`, `recall.py`)
- `claude_hooks/pgvector_mcp/` — system-wide stdio MCP server exposing pgvector recall + KG ops to any MCP-aware client
- `episodic_server/` — HTTP front-end for [obra/episodic-memory](https://github.com/obra/episodic-memory) (`server.py`, `Dockerfile`, systemd unit)
- `systemd/` — service templates: `claude-hooks-proxy`, `claude-hooks-dashboard`, `claude-hooks-rollup{.service,.timer}`, `claude-hooks-health{.service,.timer}`, `claude-hooks-daemon`, `claude-hooks-pgvector-mcp`, `caliber-grounding-proxy`, `axon-host`
- `config/` — `claude-hooks.json` (gitignored) + `claude-hooks.example.json` + `stop_phrases.yaml` (canary phrases for the in-stream stop_phrase_guard)
- `patches/` — project-specific patches for third-party npm globals (e.g. `apply-caliber-patch.sh`)
- `docs/` — runbooks (`daemon.md`, `proxy.md`, `hyde.md`, `caliber-proxy.md`, `episodic-server.md`, `pgvector-runbook.md`, `deployment.md`, `env-vars.md`, `lsp-engine.md`, `lsp-mcp.md`, `gemma4-tool-use-notes.md`), plans (`PLAN-*.md`), issue drafts (`issue-warmup-token-drain.md`, `cc-xhigh-regression-issue.md`, `openwolf-managedby-issue.md`), and the audit at `doc-audit-2026-05-01.md`
- `scripts/` — operator tooling (`proxy_rollup.py`, `proxy_health_oneliner.py`, `proxy_stats.py`, `bench_recall.py`, `bench_lsp_engine.py`, `migrate_to_pgvector.py`, `weekly_token_usage.py`, `statusline_*.py`, …)
- `tests/` — unittest-based, run with `pytest`

---

## Hook events used

Claude Code currently exposes 28+ hook events. We wire the following:

| Event              | Why                                                              | Default |
|--------------------|------------------------------------------------------------------|---------|
| `SessionStart`     | Inject "you have N memories about this project" status line + code-graph map  | on   |
| `UserPromptSubmit` | Recall from all providers (HyDE-expanded), inject as `additionalContext`      | on   |
| `Stop`             | Summarize the turn, optionally write to providers (auto-noteworthy heuristic) | on   |
| `SessionEnd`       | Final flush of any buffered observations + episodic push                      | on   |
| `PostToolUse`      | After `Edit`/`Write`/`MultiEdit`: run `ruff check` on Python files and inject diagnostics; advise on hand-edited TOMLs (`toml_comment_advisor`) | on |
| `PreToolUse`       | Memory-recall warn on match (`warn_on_tools` / `warn_on_patterns`); opt-in advisory layers below | off (recall) / on (advisory) |

PreToolUse advisory layers (all opt-in via `hooks.pre_tool_use.*`):

| Layer | Config flag | Behavior |
|-------|-------------|----------|
| `safety_scan` | `safety_scan_enabled` | Pattern-scan dangerous Bash commands; logs to `~/.claude/permission-scanner/` |
| `rtk_rewrite` | `rtk_rewrite_enabled` | Shell out to the `rtk` binary to rewrite the command; safety_scan still runs on the rewritten output by default |
| `stop_guard`  | `stop_guard.enabled`  | Block premature stops mid-task by injecting a system reminder |

### What gets injected (UserPromptSubmit)

```json
{
  "hookSpecificOutput": {
    "hookEventName": "UserPromptSubmit",
    "additionalContext": "## Recalled memory\n\n**qdrant** (3 hits):\n- bcache fix from 2026-01: rebuild superblock with `make-bcache --wipe-bcache`\n- ...\n\n**memory_kg** (2 entities):\n- solidPC (server) — runs the *arr stack, RTX 3090\n- ...\n"
  }
}
```

The additionalContext is a single markdown block per provider. The model
sees it as part of the prompt context and decides whether to use it. We
don't *force* it to do anything with the recall — that would be brittle.

### What gets stored (Stop)

The `stop` handler reads the last assistant message from the transcript
file (path is in the hook input as `transcript_path`), runs a tiny
heuristic to decide whether the turn was *noteworthy* (touched files,
ran a fix, etc. — see `claude_hooks/hooks/stop.py:should_store()`), and
if so, calls `provider.store()` with a one-paragraph summary.

The store is **automatic by default** (`store_mode: "auto"` → stores
silently when the turn is noteworthy). Set to `"off"` to disable.

---

## MCP server auto-detection

`claude_hooks/detect.py` walks the user's Claude Code config and asks each
provider whether any of the configured MCP servers match.

### Detection strategy (per provider)

1. **Name match** — server key contains a known keyword
   (`qdrant` for Qdrant, `memory`/`memorykg`/`mem-kg` for Memory KG).
   If exactly one, accept it.

2. **Tool probe** — for every remaining server, run `tools/list` over MCP
   and look for the provider's signature tool names:
   - Qdrant: `qdrant-find` and `qdrant-store`
   - Memory KG: `search_nodes` and `create_entities`
   If exactly one server exposes the signature, accept it.

3. **Ambiguous / none** — fall back to interactive prompt:
   ```
   No Qdrant MCP server detected in ~/.claude.json.
   Enter URL manually (or empty to disable Qdrant):
   ```

4. **Verify** — once chosen, the installer does a real `tools/call` against
   the cheapest tool (`qdrant-find` with empty query, `read_graph` with
   limit 1) and confirms a 200/valid JSON-RPC response.

### Where the config is read from

- Linux: `~/.claude.json` (root `mcpServers` + per-project `projects[*].mcpServers`)
- Windows: `%USERPROFILE%\.claude.json`
- Claude Desktop fallback: `%APPDATA%\Claude\claude_desktop_config.json`
  (only used if `~/.claude.json` is missing)

The installer prefers the Code config because that's where the user's
HTTP MCP servers actually live; Desktop only has stdio entries via the
`mcp-remote` bridge, which point at the same backend URLs anyway.

---

## Cross-platform strategy

**Language: Python 3.9+** (stdlib only for the core). Justification:

- Already present on most Linux systems (`/usr/bin/python3`) and on every
  modern Windows install (Microsoft Store python or `py` launcher).
- PowerShell is not installed everywhere; Python is. PowerShell-only would
  mean a runtime install on many Linux boxes, which fails the "works
  without ceremony" goal.
- Single source of truth (`.py`) runs identically on both OSes.
- `urllib.request` + `json` + `subprocess` cover everything we need; no
  pip dependencies for the core MCP providers.

### Conda environment

A dedicated conda env (`claude-hooks`) isolates test and optional
dependencies from the system Python:

```bash
conda create -n claude-hooks python=3.11 -y
conda activate claude-hooks
pip install -r requirements-dev.txt   # pytest
```

The `bin/claude-hook` shim automatically prefers the conda env's
Python (`$HOME/anaconda3/envs/claude-hooks/bin/python`) when it
exists, falling back to system `python3` otherwise. This means
hooks run in the conda env without any activation step.

Claude Code runs hooks via `/usr/bin/bash` on **all platforms** including
Windows. This means the extensionless POSIX `bin/claude-hook` shim is used
everywhere, with forward slashes in the path. The `.cmd` shim is kept for
manual invocation from `cmd.exe` / PowerShell but is **not** wired into
`settings.json`.

### settings.json entries written by the installer

Linux:
```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [{ "type": "command",
                    "command": "/path/to/claude-hooks/bin/claude-hook UserPromptSubmit",
                    "timeout": 15 }] }
    ],
    "SessionStart": [ ... ],
    "Stop":         [ ... ],
    "SessionEnd":   [ ... ]
  }
}
```

Windows:
```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [{ "type": "command",
                    "command": "C:/Users/you/claude-hooks/bin/claude-hook UserPromptSubmit",
                    "timeout": 15 }] }
    ],
    ...
  }
}
```

The installer **merges** with any existing hooks instead of overwriting,
using a tag (`"_managedBy": "claude-hooks"`) on each entry it owns so a
re-run of the installer cleans up its own old entries without touching
user-authored ones.

---

## Configuration (`config/claude-hooks.json`)

Generated by the installer; safe to edit by hand.

```json
{
  "version": 2,
  "providers": {
    "qdrant": {
      "enabled": true,
      "mcp_url": "http://YOUR-HOST:32775/mcp",
      "collection": "memory",
      "recall_k": 5,
      "store_mode": "auto"
    },
    "memory_kg": {
      "enabled": true,
      "mcp_url": "http://YOUR-HOST:32776/mcp",
      "recall_k": 5,
      "store_mode": "auto"
    }
  },
  "hooks": {
    "user_prompt_submit": { "enabled": true,  "min_prompt_chars": 30 },
    "session_start":      { "enabled": true,  "show_status_line": true },
    "stop":               { "enabled": true,  "store_threshold": "noteworthy" },
    "session_end":        { "enabled": true },
    "pre_tool_use":       { "enabled": false, "warn_on": ["Bash", "Edit"] },
    "stop_guard":         { "enabled": false }
  },
  "logging": {
    "path": "~/.claude/claude-hooks.log",
    "level": "info"
  }
}
```

This file is **gitignored**. Only `claude-hooks.example.json` is committed.

Opt-in PreToolUse extensions live under `hooks.pre_tool_use`:
`safety_scan_enabled` (pattern scanner backed by `claude_hooks/safety_patterns.py`,
logs to `~/.claude/permission-scanner/`) and `rtk_rewrite_enabled`
(shells out to the `rtk` binary >= `rtk_min_version`). When both are on,
`rtk_rewrite` runs first and the safety scanner runs on the rewritten command.
When only `rtk_rewrite_enabled` is on, the safety scanner still runs on
rtk-rewritten commands by default (`rtk_scan_rewrites: true`) to preserve
the settings.json allow-list that rtk's `allow` decision would otherwise
bypass; set `rtk_scan_rewrites: false` to opt out.

---

## Adding a new provider

1. Create `claude_hooks/providers/<name>.py` implementing the `Provider` ABC
   from `base.py`.
2. Add it to `claude_hooks/providers/__init__.py`'s `REGISTRY` dict.
3. Re-run `python3 install.py` — the installer will detect and prompt.
4. Done. No changes to hooks, dispatcher, or settings.json.

The 4 methods a provider must implement (`detect`, `verify`, `recall`,
`store`) are the entire contract.

---

## Safety / failure modes

- **Provider failure ≠ hook failure.** If Qdrant is down, the qdrant
  provider's `recall()` raises, the dispatcher catches, logs to
  `~/.claude/claude-hooks.log`, and continues with the other providers.
  The hook always exits 0 and never blocks the prompt.
- **Network timeout** is bounded per-provider (default 3 s). The hook's
  total budget is `timeout` in settings.json (15 s by default).
- **Bad MCP response** is treated as "no recall" — the hook still emits a
  valid (empty) `additionalContext`.
- **No config file** → all hooks no-op silently. Mirrors openwolf's
  `ensureWolfDir()` pattern.

---

## What this is NOT

- **Not a replacement for `~/.claude/CLAUDE.md` instructions.** Those still
  guide the model on *what to store*. The hooks just guarantee the recall
  call happens and provide a deterministic write path.
- **Not a memory store itself.** Qdrant + Memory KG are the actual stores;
  this is plumbing.
- **Not a token tracker / linter / etc.** That's openwolf's job. If you want
  both, run both — they don't conflict (different hook payloads, both can
  stack under the same event).

---

## Decisions made (post-review)

1. **`store_mode` default**: `auto` for both qdrant and memory_kg. The
   `Stop` hook writes a one-paragraph turn summary to every provider
   whose `store_mode` is `auto`, gated by a "noteworthy" heuristic
   (assistant called Bash/Edit/Write/MultiEdit). Override per-provider
   in config.

2. **Per-project vs user-global**: **user-global**.
   `~/.claude/settings.json` is the install target. Per-project opt-out
   via a `.claude-hooks-disable` marker file in the project root.

3. **Recall format**: markdown headings + bullet lists, exactly the
   shape that openwolf and CLAUDE.md inject. Models parse it reliably.

4. **`pre_tool_use` enabled by default?** **Off**. It's opt-in via
   `hooks.pre_tool_use.enabled: true` in config. Ships disabled so
   first-run latency stays predictable.

5. **Experimental DB-backed scaffolds**: `pgvector` and `sqlite_vec`
   providers exist as scaffolds (registered in REGISTRY but disabled
   by default). They depend on optional packages (`psycopg`, `sqlite_vec`)
   imported lazily so the core stays stdlib-only. They also need an
   embedder — see `claude_hooks/embedders.py` for `OllamaEmbedder` and
   `OpenAiCompatibleEmbedder`. Not yet integration-tested against a
   live Postgres or sqlite-vec install. Useful as a starting point if
   you ever want to drop Qdrant or split memory across stores.

---

## Utilities

- **`extract_plugin.py`** — extracts skills/agents/commands from an installed
  Claude Code plugin into standalone `~/.claude/skills/` etc., then disables
  the plugin. Useful when a plugin has useful skills but its hooks consume
  too much context (e.g. `code-analysis@mag-claude-plugins` injects
  `additionalContext` on every `PreToolUse` event). Cross-platform.

  ```bash
  python3 extract_plugin.py   # extracts code-analysis, disables plugin
  ```

  The extracted skills survive plugin updates. Re-run after a plugin version
  bump to pick up new skills.

## Branch & Release Workflow

As of **v1.0.0** (2026-05-01), claude-hooks uses a two-branch model:

- **`main`** — release branch. Every commit shippable. Tags (`vX.Y.Z`)
  live here. Do NOT push experimental work directly to `main`.
- **`dev`** — working branch. All feature work, fixes, refactors,
  doc changes land here first. Push freely.

**For day-to-day work:** check out `dev`, commit there, push to
`origin/dev`. Cut a release only when the work is stable and the
test suite passes.

**Authoritative version sources** — keep these in sync at every cut:
1. `pyproject.toml` → `version = "X.Y.Z"`
2. `CHANGELOG.md` → top entry `## [X.Y.Z] — YYYY-MM-DD`
3. `CLAUDE.md` → status banner near the top of this file

**Full procedure:** see [`docs/RELEASING.md`](docs/RELEASING.md). It
covers SemVer rules, the cut command sequence, hotfix flow, and what
NOT to do (don't tag from `dev`, don't force-push `main`, don't
delete published tags).

**For a new Claude Code session:** if the user asks to add a
feature, fix a bug, or do exploratory work, ensure you are on `dev`
before committing (`git checkout dev` if needed). Use `main` only
during a release cut.

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
  2. Run: `caliber refresh && git add CLAUDE.md .claude/ .cursor/ .cursorrules .github/copilot-instructions.md .github/instructions/ AGENTS.md CALIBER_LEARNINGS.md .agents/ .opencode/ 2>/dev/null`
  3. After it completes, briefly tell the user what Caliber updated. Then proceed with the commit.

If `caliber` is not found, tell the user: "This project uses Caliber for agent config sync. Run /setup-caliber to get set up."
<!-- /caliber:managed:pre-commit -->

<!-- caliber:managed:learnings -->
## Session Learnings

Read `CALIBER_LEARNINGS.md` for patterns and anti-patterns learned from previous sessions.
These are auto-extracted from real tool usage — treat them as project-specific rules.
<!-- /caliber:managed:learnings -->

<!-- caliber:managed:sync -->
## Context Sync

This project uses [Caliber](https://github.com/caliber-ai-org/ai-setup) to keep AI agent configs in sync across Claude Code, Cursor, Copilot, and Codex.
Configs update automatically before each commit via `caliber refresh`.
If the pre-commit hook is not set up, run `/setup-caliber` to configure everything automatically.
<!-- /caliber:managed:sync -->
