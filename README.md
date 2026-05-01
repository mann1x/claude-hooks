# claude-hooks

Cross-platform Claude Code hooks that auto-recall from **Qdrant** + **Memory KG**
on every prompt and write findings back at the end of the turn.

Install once at the **user level** and every Claude Code session gets
deterministic memory recall + storage — no per-project init, no model
forgetting. v0.4.0 adds episodic-memory sync, cross-platform plugin
management, and plugin extraction utilities.

---

## Quickstart

```bash
git clone https://github.com/mann1x/claude-hooks.git
cd claude-hooks
python3 install.py
```

The installer auto-detects your MCP servers, creates the config, and wires
hooks into `~/.claude/settings.json`. Open a new Claude Code session and
you'll see:

> _Started with claude-hooks recall enabled (2 provider(s): Qdrant, Memory KG)._

Check `~/.claude/claude-hooks.log` to confirm hooks are firing.

For the full playbook — LAN-shared proxy setup, systemd unit, statusline
wiring, monitoring, uninstall — see [`docs/deployment.md`](docs/deployment.md).

---

## What it does

```
user prompt
   |
   v
[UserPromptSubmit hook] --> HyDE expand --> recall from providers --> decay rank --> inject
   |
   v
Claude responds (knowing the prior context, deterministically)
   |
   v
[Stop hook] --> classify --> dedup check --> store --> extract instincts
   |
   v
[SessionStart on compact] --> full recall re-injection (memory recovery)
```

## Features

### Core (v0.1)
- **Stdlib only** for the core (Qdrant + Memory KG providers, hooks, dispatcher) — no `pip install` needed. Optional features (pgvector, sqlite-vec, code-graph, MCP server, clustering) pull in their own deps via the `[code-graph]` / `[clustering]` / `[mcp-server]` extras.
- **Python 3.9+**, runs identically on Linux, macOS, and Windows.
- **Auto-detection** of MCP servers from `~/.claude.json`
- **Plugin model**: each memory backend is one file (qdrant, memory_kg, pgvector, sqlite_vec)
- **OpenWolf integration**: injects Do-Not-Repeat and recent bugs from `.wolf/` projects
- **Non-blocking**: every hook exits 0 even on failure

### Intelligence (v0.2)
- **HyDE query expansion** -- generates a hypothetical answer via Ollama before
  searching Qdrant, dramatically improving recall quality. Falls back to raw
  prompt if Ollama is unavailable.
- **Attention decay** -- memories that haven't been recalled recently fade;
  frequently useful ones strengthen. Tracks history in a JSON file.
- **Memory dedup** -- before storing, checks for near-duplicates using text
  similarity. Prevents Qdrant from accumulating redundant entries.
- **Observation classification** -- tags stored memories as `fix`, `preference`,
  `decision`, `gotcha`, or `general` for better downstream filtering.
- **Compact recall** -- when Claude Code compacts context, the SessionStart hook
  re-injects full recalled memory so the model recovers what it lost.
- **Instinct extraction** -- when a bug-fix pattern is detected (error -> edit),
  auto-extracts it as a reusable markdown instinct file under `~/.claude/instincts/`.
- **Progressive disclosure** -- optional: inject only the first line of each memory
  with a char-count hint, cutting injected context by ~3-5x.
- **`/reflect` synthesis** -- CLI command that analyzes recent memories for
  recurring patterns and generates CLAUDE.md rules. Uses Ollama.
- **Autonomous consolidation** -- CLI command to find duplicates, compress old
  memories, and prune stale ones. Uses Ollama.

### Proxy / observability (v0.5+)

- **Local HTTP proxy** in front of `api.anthropic.com` ([`docs/proxy.md`](docs/proxy.md))
  that Claude Code hooks can't see on their own. Opt-in via
  `config/claude-hooks.json`. **`install.py` orchestrates the per-OS
  service** — pick "Use the API proxy?" → either `[1]` install locally
  (systemd unit on Linux, `LaunchAgent` on macOS, UAC-elevated scheduled
  task on Windows) or `[2]` point at an existing proxy on the LAN
  (writes `ANTHROPIC_BASE_URL` into `~/.claude/settings.json` for you).
- **Warmup short-circuit** (`proxy.block_warmup: true`) — drops the
  subagent-Warmup token drain ([`anthropics/claude-code#47922`](https://github.com/anthropics/claude-code/issues/47922))
  without the all-or-nothing side-effects of
  `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS`. Returns a spec-compliant stub
  (JSON or SSE) so CC never sees an error. The proxy recognises **two
  distinct drain patterns** and blocks both under the same flag:

  | Pattern | Signature (`claude_hooks/proxy/metadata.py`) | What it is |
  |---|---|---|
  | **CLI "Warmup"** | `first_user_text == "Warmup"` | The keepalive Claude Code sends every few turns to keep the context hot. Cheap per-request but runs thousands of times per day. Classic token drain. |
  | **SDK-CLI subagent priming** | `cc_entrypoint == "sdk-cli"` AND `agent_type == "subagent"` AND `num_messages == 1` | The Agent SDK's priming message when a subagent boots. Single user turn, no "Warmup" literal, so it looks like a real prompt to a naïve filter — but it's the same "init the context" intent, just from the SDK-CLI entrypoint. Historically slipped past the old `first_user_text` check and amplified 300M+ cache reads/day on subagent-heavy workflows. |

  Both map to `is_warmup=True` in request metadata and are blocked
  identically when `block_warmup` is on. The dashboard's
  *warmups_blocked* counter aggregates them; `scripts/proxy_stats.py
  --show-sidechain` breaks them out.

  > **Update 2026-04-28** — the literal `"Warmup"` priming call no
  > longer appears in proxy logs starting with Claude Code **2.1.121**
  > (60 blocks on 04-27 → 0 on 04-28 across 1,300+ requests, on a host
  > with no proxy config change). The detector is unchanged; the
  > traffic itself is absent. We're keeping `block_warmup: true` on
  > as a safety net in case the pattern returns. See
  > [`docs/issue-warmup-token-drain.md`](docs/issue-warmup-token-drain.md#update--2026-04-28-warmup-traffic-disappeared)
  > for the per-day evidence and the upstream issue thread.
- **Live weekly-limit %** — proxy captures Anthropic's
  `anthropic-ratelimit-unified-*` headers into a rolling state file;
  `scripts/statusline_usage.py` reads it for a compact statusline
  segment, `scripts/weekly_token_usage.py --current-usage-pct`
  auto-populates from the same file.
- **Structured observations** (port from thedotmack/claude-mem) —
  `hooks.stop.summary_format: "xml"` stores memories as
  `<observation><type><title><files_modified>…` so downstream recall
  can filter by type without prose parsing.
- **Metadata-gated rerank** — `hooks.user_prompt_submit.metadata_filter`
  filters candidates by cwd / type / age / tags before vector rerank.
- **Caliber grounding proxy** ([`docs/caliber-proxy.md`](docs/caliber-proxy.md))
  — local OpenAI-compat HTTP server that augments [caliber](https://github.com/caliber-ai-org/ai-setup)
  with project grounding so `caliber init`/`refresh` cite real `path:line`
  references instead of hallucinated ones. Paired with `bin/caliber-smart`
  as a drop-in `caliber` wrapper that falls back to claude-cli when the
  proxy is down.

  > ⚠️ The shipped `bin/caliber-grounding-proxy` defaults
  > `CALIBER_GROUNDING_UPSTREAM=http://192.168.178.2:11433/v1` — the
  > author's home-LAN Ollama proxy. **Override via the systemd drop-in
  > or shell environment** for your install (see the linked doc).

### Code graph (v0.6+)

A built-in, file-based code-structure graph (`graphify-out/graph.json`
+ `GRAPH_REPORT.md`) auto-built per project. Stdlib-only Python `ast`
extractor; opt-in `[code-graph]` extra adds tree-sitter parsing for
JS/TS/Go/Rust/Java/Ruby. SessionStart injects a 2-3 KB structural
summary; per-Grep `code_graph_lookup_enabled` adds one-line "X is at
file:line, N callers" hints when the pattern looks like an identifier.

CLI subcommands (`python -m claude_hooks.code_graph ...`):

| Command | What |
|---|---|
| `build` | Walk the tree, extract symbols + calls + imports, write graph.json + GRAPH_REPORT.md |
| `info` | Print the graph's stats (file/node/edge counts, by-language) |
| `impact <symbol>` | Transitive callers + callees of a symbol (blast radius before refactoring) |
| `changes [--base REF]` | Blast-radius report for the current `git diff` (pre-commit / PR sanity check) |
| `trace <entrypoint>` | Forward call-chain trace from an entry function ("how does X flow through the system?") |
| `mermaid [--center SYM]` | Render a Mermaid module-map or local subgraph diagram |
| `clusters` | Detect functional communities in the call graph (Louvain when `[clustering]` extra installed; file-based fallback otherwise) |
| `companions` | Show detection state for axon + gitnexus + the local code graph |

Optional extras:

- **`pip install claude-hooks[code-graph]`** — `tree-sitter-language-pack` for multi-language parsing.
- **`pip install claude-hooks[clustering]`** — `python-louvain` + `networkx` for Leiden-style community detection.
- **`pip install claude-hooks[mcp-server]`** — `mcp[cli]` to spin up an MCP server (`python -m claude_hooks.code_graph.mcp_server`) exposing the lookup/impact/changes/trace/mermaid/companions tools to any MCP client (Claude Code, Cursor, etc.).

### Companion code-graph engines

When you want richer queries than the built-in `code_graph` provides,
claude-hooks integrates with two heavier engines as **opt-in companion
tools** (silent no-op when absent):

- **[axon](https://github.com/harshkedia177/axon)** (RECOMMENDED for Python/JS/TS) — `pip install axoniq`,
  KuzuDB-backed, dead-code detection, file watcher, 7 MCP tools.
- **[gitnexus](https://github.com/abhigyanpatwari/GitNexus)** (ALTERNATIVE for 14 languages or multi-repo) — `npm i -g gitnexus`,
  LadybugDB-backed, hybrid BM25+vector+RRF search, multi-repo `group_*` tools, 16 MCP tools.

claude-hooks detects either via filesystem checks (binary on PATH +
per-project marker dir + global registry), appends a `mcp__axon__*` /
`mcp__gitnexus__*` hint to the SessionStart inject, and spawns the
appropriate `analyze` on Stop when the turn modified files. Both can
coexist; both reindex paths fire when their respective marker dirs
are present. See [`COMPANION_TOOLS.md`](COMPANION_TOOLS.md) §6-7 for
the install + comparison matrix.

The built-in `code_graph` always runs as the floor; the companions
upgrade specific dimensions (live MCP queries, dead-code detection,
multi-language coverage) when present.

### IDE-style feedback loop (v0.7+)

Closes the "I didn't notice the import error until I ran the code"
gap. Two complementary layers:

- **`PostToolUse` ruff hook** (built-in, on by default) — runs `ruff
  check` on every Python file Claude Code edits with `Edit` / `Write` /
  `MultiEdit`. Diagnostics are injected as
  `hookSpecificOutput.additionalContext` so the model sees them in the
  very next prompt — before claiming the change is done. ~50 ms cold,
  catches undefined names, unused imports, syntax errors, etc. Config
  under `hooks.post_tool_use` in `config/claude-hooks.json`.
- **cclsp** (recommended companion, opt-in) — multi-language LSP
  wrapper that fronts pyright / gopls / rust-analyzer / clangd /
  OmniSharp via a single MCP server. Gives Claude Code on-demand
  hover, go-to-definition, find-references, and type diagnostics
  across Python / Go / Rust / C/C++ / C#. See [`docs/lsp-mcp.md`](docs/lsp-mcp.md)
  for the install + Linux/Windows config. Pairs with the ruff hook:
  ruff is the cheap synchronous Python layer, cclsp is the
  multi-language on-demand layer.
- **LSP engine** (opt-in) — a session-scoped daemon that loads
  language servers once per project, follows edits in real time
  via UNIX-socket IPC, and answers diagnostics queries in
  single-digit milliseconds. Per-file session-affinity locks
  serialise multi-session edits cleanly; adaptive preload of the
  code-graph hot set warms the LSP index before the first query;
  a polling git watcher bulk-refreshes open files on branch
  switch; opt-in compile-aware mode merges `cargo check` /
  `tsc --noEmit` / `mypy` diagnostics on top of the LSP layer.
  See [`docs/lsp-engine.md`](docs/lsp-engine.md) for the user
  guide and [`docs/PLAN-lsp-engine.md`](docs/PLAN-lsp-engine.md)
  for the design rationale. Run `/setup-compile-aware` for a
  guided proposal of the per-language compile commands.

### Scripts

| Script | What |
|---|---|
| `scripts/status.py` | At-a-glance dashboard: systemd state, current rate-limit %, today's Warmup-blocked count. `--json` for scripting. |
| `scripts/weekly_token_usage.py` | Per-day token breakdown against a custom weekly-reset window (default Fri 10:00 CEST). Auto-populates `%Limit` from the proxy. `--show-sidechain` reveals the Warmup share. |
| `scripts/proxy_stats.py` | Ad-hoc proxy-log summaries (per-day requests, Warmup-blocked savings, synthetic-rate-limit detection, per-model counts). `--json` for scripting. |
| `scripts/statusline_usage.py` | Compact statusline segment showing live 5h / 7d %. Safe-by-design (never crashes the caller). |
| `scripts/openwolfstatus.{py,sh,bat}` | OpenWolf status utility. |

## Requirements

- **Python 3.9+**. The recall/store core is **stdlib-only**; only the proxy
  and the optional DB-backed providers (pgvector, sqlite-vec) need wheels.
- **Claude Code** with hooks support.
- **At least one memory backend** — pick from the table below. Multiple can
  run simultaneously; the dispatcher fans out recall in parallel.
- *(Optional)* **Ollama** for HyDE, /reflect, /consolidate, and the embedder
  side of the pgvector / sqlite-vec providers.

### Memory backends — pick at install time

| Backend | Setup | Extra deps | Strengths |
|---|---|---|---|
| **Qdrant** (HTTP MCP) | Run `mcp-server-qdrant` (we ship a patched version under `vendor/mcp-qdrant/`) | none | mature vector search; the historical default |
| **Memory KG** (HTTP MCP) | Run `mcp-memory` (npm `@modelcontextprotocol/server-memory`) | none | typed entity graph + observation keyword search |
| **Postgres + pgvector** | Local docker stack — see [`docs/pgvector-runbook.md`](docs/pgvector-runbook.md). `install.py` handles DSN probe, schema init, embedder pull, and registers a system-wide `pgvector-mcp` stdio server in `~/.claude.json` so other MCP clients (Cursor/Codex/OpenWebUI) can use the same store. | `pip install -r requirements-pgvector.txt` | single SQL backend that replaces both Qdrant + Memory KG; hybrid recall (vector + BM25 RRF); native KG entities/relations/observations |
| **sqlite-vec** | Standalone SQLite file at `~/.claude/claude-hooks-memory.db` | `pip install -r requirements-sqlite-vec.txt` | zero-server, single-file, low-footprint |

### Conda env + dependency files

The installer creates a `claude-hooks` conda env (Python 3.11) by default
and pip-installs the requirements files relevant to your enabled
backends. Manual install for reference:

```bash
conda create -n claude-hooks python=3.11 -y
conda activate claude-hooks

pip install -r requirements.txt                          # core (httpx[http2])
pip install -r requirements-pgvector.txt                 # if pgvector enabled
pip install -r requirements-sqlite-vec.txt               # if sqlite-vec enabled
pip install -r requirements-dev.txt                      # tests + coverage
```

The `bin/claude-hook` shim auto-detects this env (POSIX layout, Windows
`Scripts/python.exe`, MSYS2 hybrid) and falls back to system `python3`,
so no activation step is needed at hook runtime.

## Install

```bash
git clone https://github.com/mann1x/claude-hooks.git
cd claude-hooks
python3 install.py
```

The installer will:
1. Detect if you have a conda env and offer to create one (optional -- system Python works fine)
2. Scan `~/.claude.json` for MCP servers matching Qdrant and Memory KG
3. Verify each server with a real MCP call
4. Write `config/claude-hooks.json` with your server URLs
5. Merge hook entries into `~/.claude/settings.json` (idempotent, tagged `_managedBy`)
6. Asks **"Use the API proxy?"**. On yes:
   - **`[1]` Local install** — pip-installs `httpx[http2]>=0.27` into
     the chosen Python env, then drops the per-OS service:
     - **Linux** — `claude-hooks-proxy.service` + `rollup.service` +
       `rollup.timer` + `dashboard.service` in `/etc/systemd/system/`,
       `daemon-reload` + `enable --now`.
     - **macOS** — `~/Library/LaunchAgents/com.claude-hooks.proxy.plist`
       (`KeepAlive=true`, `RunAtLoad=true`), loaded via `launchctl`.
     - **Windows** — UAC-elevated logon-triggered scheduled task
       `claude-hooks-proxy` (pythonw + `run_proxy.py` to avoid a
       persistent cmd window).
     Optionally writes `ANTHROPIC_BASE_URL=http://127.0.0.1:38080` into
     `~/.claude/settings.json` (LAN listen hosts auto-translate to loopback
     on the client side).
   - **`[2]` Remote URL** — prompts for the proxy URL of an existing host
     on the LAN (e.g. `http://192.168.178.2:38080`) and writes
     `ANTHROPIC_BASE_URL` into `~/.claude/settings.json`. No local service.
   - Idempotent on re-run: already-installed services are detected and
     left alone unless you confirm reinstall.

### Installer flags

```bash
python3 install.py --dry-run         # show changes, don't write
python3 install.py --non-interactive # CI-friendly, fail on prompts
python3 install.py --uninstall       # remove all claude-hooks entries
python3 install.py --probe           # force tool-probe detection
```

### Verify it works

After install, open a new Claude Code session. You should see the
`SessionStart` status line. Then check the log:

```bash
tail -20 ~/.claude/claude-hooks.log
```

You should see `recall` entries for each provider on every prompt.

## Configuration

After install, `config/claude-hooks.json` lives in the repo (gitignored).
Full schema with all options: [`config/claude-hooks.example.json`](config/claude-hooks.example.json).

### v0.2 features (all opt-in via config)

| Feature | Config key | Default | What it does |
|---------|-----------|---------|-------------|
| HyDE query expansion | `hooks.user_prompt_submit.hyde_enabled` | `false` | Generates a hypothetical answer via Ollama to improve search recall |
| Attention decay | `hooks.user_prompt_submit.decay_enabled` | `false` | Fades old memories, strengthens frequently useful ones. `halflife_days` = how fast (14 = gentle, 7 = aggressive) |
| Progressive disclosure | `hooks.user_prompt_submit.progressive` | `false` | Shows only first line + char count per memory, ~3-5x less context |
| Memory dedup | `providers.qdrant.dedup_threshold` | `0.0` | Text similarity threshold before storing. Set to `0.85` to skip near-duplicates |
| Observation classification | `hooks.stop.classify_observations` | `true` | Tags memories as fix/preference/decision/gotcha/general |
| Compact recall | `hooks.session_start.compact_recall` | `true` | Re-injects memories after context compaction so nothing is lost |
| Instinct extraction | `hooks.stop.extract_instincts` | `false` | Auto-creates markdown "instinct" files from bug-fix patterns |
| /reflect synthesis | `reflect.enabled` | `true` | Requires Ollama. Analyzes memory patterns and generates CLAUDE.md rules |
| Consolidation | `consolidate.enabled` | `false` | Requires Ollama. Deduplicates, compresses, and prunes old memories |
| Auto-consolidation | `consolidate.trigger` | `"manual"` | `"session_start"` runs `consolidate()` automatically every `min_sessions_between_runs` (default 10) sessions. CLI invocation always works regardless. |
| PreToolUse memory warn | `hooks.pre_tool_use.warn_on_tools` / `warn_on_patterns` | `["Bash","Edit","Write"]` / `["rm ","DROP TABLE","git reset --hard"]` | Match a tool + a substring in its args; recall against that command and inject as advisory `additionalContext`. Never blocks. |
| PreToolUse file-read gate | `hooks.pre_tool_use.file_read_gate` / `file_read_gate_tools` | `false` / `["Read","Edit","MultiEdit"]` | Port 5 from thedotmack/claude-mem. When `Read`/`Edit`/`MultiEdit` touches a path with prior memories, inject those memories regardless of `warn_on_patterns`. |
| Detached store | `hooks.stop.detach_store` | `false` | Fork the dedup-and-store fan-out into a detached subprocess so Stop returns immediately. ~200–500 ms saved per noteworthy turn. See [`docs/daemon.md`](docs/daemon.md#latency-tiers-and-detach_store). |
| Daemon (long-lived hook executor) | `hooks.daemon.enabled` (auto via installer) | platform-dependent | Single Python process owns providers + config across hook invocations. Each hook answers in milliseconds instead of 100–300 ms. See [`docs/daemon.md`](docs/daemon.md). |

### HyDE model

Default: `gemma4:e2b` with `qwen3:4b` fallback. Any small Ollama model
works -- it just needs to produce a short hypothetical answer for search
expansion. If Ollama is down, HyDE degrades gracefully to the raw prompt.

## Commands Reference

### Slash commands (inside Claude Code)

These are available as skills after running the installer. Type the
command in the Claude Code prompt.

| Command | Requires | Description |
|---------|----------|-------------|
| `/reflect` | Ollama | Analyze recent memories for recurring patterns, generate CLAUDE.md rules |
| `/consolidate` | Ollama | Find duplicate memories, compress old entries, prune stale ones |
| `/wrapup` | -- | Produce a restore-ready session state summary before compacting / pausing |
| `/episodic <query>` | episodic-server | Search past Claude Code conversations by semantic query |
| `/save-learning` | -- | Save a user instruction/preference as a persistent learning |
| `/find-skills` | caliber | Search the public skill registry for community skills |
| `/setup-caliber` | caliber | Set up Caliber pre-commit hooks for config drift detection |

### CLI commands (outside Claude Code)

Run these from your terminal in the claude-hooks repo directory.

```bash
# Memory analysis
python -m claude_hooks.reflect              # generate CLAUDE.md rules from memory patterns
python -m claude_hooks.reflect --dry-run    # preview without writing

python -m claude_hooks.consolidate          # deduplicate and compress old memories
python -m claude_hooks.consolidate --dry-run

# Installer
python3 install.py                          # interactive install
python3 install.py --dry-run                # show changes, don't write
python3 install.py --non-interactive        # CI-friendly, no prompts
python3 install.py --uninstall              # remove all claude-hooks entries
python3 install.py --probe                  # force MCP tool-probe detection
python3 install.py --episodic-server        # configure as episodic-memory server
python3 install.py --episodic-client URL    # configure as episodic-memory client

# Episodic server (on the server host)
python3 episodic_server/server.py --host 0.0.0.0 --port 11435
systemctl status episodic-server            # if installed as systemd service
journalctl -u episodic-server -f            # follow server logs

# Episodic API (from any host)
curl "http://SERVER:11435/search?q=bcache&limit=5"   # search conversations
curl http://SERVER:11435/health                       # health check
curl http://SERVER:11435/stats                        # index statistics
curl -X POST http://SERVER:11435/sync                 # trigger re-index
```

## Per-project opt-out

```bash
touch your-project/.claude-hooks-disable
```

Any directory with this marker file (or any ancestor) will skip all hooks.
The filename can be changed via the top-level `disable_marker_filename`
config key (default `.claude-hooks-disable`) if you need a different
sentinel name for your organisation.

## Uninstall

```bash
python3 install.py --uninstall
```

This removes the 4 hook entries tagged `_managedBy: "claude-hooks"` from
`~/.claude/settings.json`. Your other hooks and settings are left intact.

## Adding a new provider

1. Create `claude_hooks/providers/<name>.py` implementing the `Provider` ABC
2. Add it to `claude_hooks/providers/__init__.py` `REGISTRY`
3. Re-run `python3 install.py`

The 4 methods a provider implements (`detect`, `verify`, `recall`, `store`)
are the entire contract. Providers may optionally override `batch_recall`
and `batch_store` for backends with native bulk operations — the default
implementation parallelises single-shot calls.

## Pgvector backend (optional)

For users who'd rather run a single Postgres-backed memory store than
Qdrant + Memory KG, claude-hooks ships an opt-in pgvector provider plus
a docker stack and a migration script.

The full walkthrough lives at **[`docs/pgvector-runbook.md`](docs/pgvector-runbook.md)**:
docker compose at `/shared/config/mcp-pgvector/`, idempotent migration
+ delta sync via `scripts/migrate_to_pgvector.py`, a benchmark harness
at `scripts/bench_recall.py`, and the design rationale at
[`docs/PLAN-pgvector-migration.md`](docs/PLAN-pgvector-migration.md).

Bench-driven default embedder pick (since 2026-04-28) is
`qwen3-embedding:0.6b` (1024 dim, native 32k ctx). It replaces the
earlier `nomic-embed-text` default after a head-to-head bench showed
tighter cosine distances on niche queries and full 32k context that
eliminates the silent 8k truncation cliff on long Stop summaries.
Speed cost is real (~85 ms p50 embed vs ~38 ms for nomic) but total
recall stays under 100 ms end-to-end on HNSW. **Tables are
model-namespaced** (`memories_<short>`) because the embedding dim is
part of the column type — see the runbook's *Swapping the embedding
model* section if you want to change it.

Pgvector runs alongside Qdrant + Memory KG until you decide to retire
them — there's no flag day.

## Plugin Extraction

Some Claude Code plugins inject `additionalContext` on every `PreToolUse`
event, which accumulates context rapidly and can cause premature compaction.
The `extract_plugin.py` utility extracts the useful parts (skills, agents,
commands) as standalone files and disables the plugin's hooks:

```bash
python3 extract_plugin.py
```

This currently targets `code-analysis@mag-claude-plugins`, which intercepts
every Grep, Glob, Bash, Read, and Task call with claudemem enrichment.
After extraction, all skills (`/code-analysis--investigate`,
`/code-analysis--deep-analysis`, etc.) remain available on-demand — only the
automatic per-tool-call injection is removed.

Re-run after a plugin version bump to pick up new skills.

## Vendored MCP servers

### `vendor/mcp-qdrant` — patched `mcp-server-qdrant` with score threshold

Upstream [`mcp-server-qdrant`](https://github.com/qdrant/mcp-server-qdrant)
always returns `QDRANT_SEARCH_LIMIT` results on every `qdrant-find` call, no
matter how weak the cosine similarity. On a realistic memory store this
injects low-confidence noise into your prompt context on every turn.

`vendor/mcp-qdrant/` contains a Dockerfile + idempotent build-time patch that
adds a `QDRANT_SCORE_THRESHOLD` env var, forwarding Qdrant's native
`score_threshold` into the MCP server. Set it to e.g. `0.40` and anything
below that similarity is dropped before reaching `claude-hooks`.

Same image, same endpoints as upstream — just one extra env var. See
[`vendor/mcp-qdrant/README.md`](vendor/mcp-qdrant/README.md) for the full
build/run instructions and how to pick a threshold for your embedding model.

## Optional PreToolUse / Stop hooks (opt-in)

Three optional hooks are bundled but disabled by default. Enable them
individually in `config/claude-hooks.json` after reading the doc for
each one.

### `stop_guard` — force the assistant to keep working

Scans the last assistant message on `Stop` events for
ownership-dodging phrases ("pre-existing issue", "known limitation"),
session-quitting phrases ("good stopping point", "continue in the
next session"), and permission-seeking mid-task ("should I continue?").
If matched, returns `decision: block` with a correction so the
assistant resumes working instead of stopping. Respects
`stop_hook_active` to avoid infinite loops.

```json
"hooks": {
  "stop_guard": { "enabled": true }
}
```

Patterns are opinionated defaults (derived from rtfpessoa's CLAUDE.md
golden rules). Override with your own
`patterns: [{"pattern": "regex", "correction": "message"}, ...]` in
config. Source: [`claude_hooks/stop_guard.py`](claude_hooks/stop_guard.py).

**User-intent wrap-up escape**: by default the guard skips its check
when the last user message contains a wrap-up marker (e.g. "wrap up",
"compact the context", "save state", "continue another time",
"/wrapup"). This lets `/wrapup` and similar explicit hand-off requests
finish cleanly without being blocked. Disable with
`skip_on_user_wrap_up: false`, or extend the marker list via
`user_wrap_up_markers: ["…", …]`.

**Meta-context escape**: by default the guard skips its check when the
match is only inside a quoted span (`"…"`, `'…'`, `` `…` ``) or the
message contains a meta-marker phrase like "trigger phrase",
"would trigger", "stop_guard", "testing the hook", etc. This avoids
false positives when the assistant is documenting, testing, or quoting
the guard's rules. Turn off with `skip_meta_context: false`, or
extend the marker list via `meta_markers: ["…", …]`.

### `safety_scan` — ask-before-running on dangerous commands

PreToolUse scanner that matches dangerous patterns **anywhere** in a
Bash command (after pipes, chains, `find -exec`, subshells), not just
as a prefix. Emits `permissionDecision: "ask"` on match so the user
always makes the call; never auto-denies. Complements the
prefix-based allow-list in `~/.claude/settings.json`.

```json
"hooks": {
  "pre_tool_use": {
    "safety_scan_enabled": true,
    "safety_log_retention_days": 90
  }
}
```

Default pattern list covers `sudo`, `rm -rf`, `mkfs`, `dd`,
`curl | sh`, destructive git operations, `npm install -g`,
`DROP TABLE`, and more. See
[`claude_hooks/safety_patterns.py`](claude_hooks/safety_patterns.py).
Matches are logged as JSONL under `~/.claude/permission-scanner/`
with daily rotation (90-day retention by default).

### `rtk_rewrite` — transparent command rewrite for token savings

PreToolUse hook that shells out to [`rtk`](https://github.com/rtk-ai/rtk)
(a Rust CLI) to rewrite verbose `find` / `grep` / `git log` / `du`
style commands into terser rtk equivalents. rtk-ai claims 60-90%
token savings on matching commands.

```json
"hooks": {
  "pre_tool_use": {
    "rtk_rewrite_enabled": true,
    "rtk_min_version": "0.23.0"
  }
}
```

Requires the `rtk` binary (>= 0.23.0) on `PATH`. Install from
https://github.com/rtk-ai/rtk (Homebrew, curl installer, or download
the Windows zip). If `rtk` is missing or too old, the hook silently
passes the command through — safe to enable on partially-deployed
fleets. **Name collision warning**: there's an unrelated "Rust Type
Kit" crate also named `rtk` on crates.io — uninstall it first
(`rm $(which rtk)` if `rtk --version` shows `0.1.x` without a
`rewrite` subcommand). Source:
[`claude_hooks/rtk_rewrite.py`](claude_hooks/rtk_rewrite.py).

**Safety interaction with rtk** — when rtk produces a rewrite, the
hook emits `permissionDecision: "allow"`, which **bypasses** the
prefix allow-list in `~/.claude/settings.json`. To keep that safety
net, `rtk_scan_rewrites: true` (default) runs the scanner on
rtk-rewritten commands even when `safety_scan_enabled: false`:

- `rtk_rewrite_enabled=true, safety_scan_enabled=false, rtk_scan_rewrites=true` (default): rtk rewrites `ls && rm -rf /tmp` → scanner catches `rm -rf` → "ask".
- `rtk_rewrite_enabled=true, safety_scan_enabled=false, rtk_scan_rewrites=false`: same input → "allow" (user opted out of the safety net).
- `rtk_rewrite_enabled=true, safety_scan_enabled=true`: scanner runs on every Bash command, rewritten or not.

## Other configurable features

### HyDE query expansion (UserPromptSubmit recall)

HyDE (Hypothetical Document Embeddings) rewrites your prompt into a
hypothetical answer before vector search, which usually lands better in
"answer space" than the raw question. Enabled via
`hooks.user_prompt_submit.hyde_enabled: true`. Tunables:

| Key | Default | Purpose |
|-----|---------|---------|
| `hyde_model` | `gemma4:e2b` | Primary Ollama model |
| `hyde_fallback_model` | `gemma4:e4b` | Fallback if primary fails |
| `hyde_url` | `http://localhost:11434/api/generate` | Ollama endpoint |
| `hyde_timeout` | `30.0` | Per-call timeout (seconds) |
| `hyde_max_tokens` | `150` | Output length cap for the hypothetical answer |
| `hyde_keep_alive` | `"15m"` | Ollama `keep_alive` — keeps the model resident between calls so cold-start doesn't hit every prompt |
| `hyde_grounded` | `true` | Two-phase grounded pipeline: query Qdrant raw first, then feed top hits to the LLM as grounding before generating the expansion. Prevents hallucinated domain terms poisoning the search. |
| `hyde_ground_k` | `3` | How many raw hits to use as grounding context |
| `hyde_ground_max_chars` | `1500` | Cap on the grounding context size |

If raw recall finds nothing (garbage query), grounded mode short-circuits
and skips HyDE entirely — cheaper than an ungrounded hallucinated
expansion.

**Precedence with `metadata_filter`** — when both are enabled, the
metadata filter applies first: each provider returns
`recall_k * over_fetch_factor` candidates, the filter trims by
cwd/type/age/tags, and only the survivors form HyDE's grounding pool.
So a too-narrow filter will silently disable grounded HyDE (zero raw
hits ⇒ no grounding ⇒ HyDE skipped). Loosen the filter before
suspecting HyDE quality. See [`docs/hyde.md`](docs/hyde.md) for the
full pipeline.

### Per-provider `dedup_threshold`

On `Stop`, providers that expose cosine similarity (qdrant, pgvector,
sqlite_vec) can skip storing a turn summary if an existing entry is
above the given cosine threshold. Set on the provider entry:

```json
"providers": {
  "qdrant": { "dedup_threshold": 0.85 }
}
```

`0.0` disables (the default for most providers). `0.85` is a sensible
floor for "don't bother, we already have this." Higher = stricter.

The threshold is a **cosine similarity** (range 0.0–1.0, higher = more
similar), computed via the provider's own embedding model on the
truncated summary (first 500 chars). Don't confuse with `1 - distance`
in some pgvector queries — claude-hooks normalises providers to
similarity-space internally so `dedup_threshold` always means "skip
storing if any existing memory has cosine ≥ this value."

### `classify_observations` and instincts extraction (Stop hook)

The Stop hook tags each stored memory with an `observation_type`
(`fix`, `decision`, `preference`, `gotcha`, `general`) so downstream
tooling can filter. Toggle with `hooks.stop.classify_observations`.

`hooks.stop.extract_instincts` (opt-in) additionally runs a lightweight
heuristic to pull persistent rules from the assistant's message and
write them to `hooks.stop.instincts_dir` (default `~/.claude/instincts`)
as a sidecar you can promote to CLAUDE.md manually. Experimental.

### `summary_format`: markdown vs XML

`hooks.stop.summary_format` controls the layout of stored memories:

- `"markdown"` (default) — backward-compatible plain-text bullet list.
  What every Qdrant corpus written before v0.5 contains.
- `"xml"` — structured `<observation>` block (port from
  thedotmack/claude-mem). Each field is addressable, so downstream
  recall can filter by type without prose parsing:

  ```xml
  <observation ts="2026-04-29T12:34:56Z">
    <type>fix</type>
    <title>bcache make-bcache --wipe-bcache rebuild</title>
    <subtitle>/srv/dev-disk-by-label-opt/dev/claude-hooks</subtitle>
    <cwd>/srv/dev-disk-by-label-opt/dev/claude-hooks</cwd>
    <prompt>...truncated 600 chars...</prompt>
    <result>...truncated 1200 chars...</result>
    <files_modified>
      <file>/etc/fstab</file>
      <file>/etc/bcache.conf</file>
    </files_modified>
    <files_read>...</files_read>
    <commands>
      <command>make-bcache --wipe-bcache /dev/sda3</command>
    </commands>
  </observation>
  ```

  When `summary_format: "xml"` is on, the Stop hook also reads the
  `<type>` tag back to seed `metadata.observation_type` directly
  (skipping the heuristic classifier when the model has already
  declared it).

Mixing formats inside one corpus works but mucks up search ranking —
pick one per corpus and stick with it. The format the entry was
written with is recorded in `metadata.summary_format` so you can
filter or re-write later.

### Claudemem auto-reindex

[`claudemem`](https://github.com/MadAppGang/claudemem) is a semantic
code-search tool with its own AST-aware index. Upstream ships a git
post-commit hook (`claudemem hooks install`), but that doesn't cover
**uncommitted mid-session edits**. This hook plugs the gap:

- **Stop event**: if the turn ran any `Edit`/`Write`/`MultiEdit`/
  `NotebookEdit`, spawn `claudemem index --quiet` detached so the
  hook adds no latency.
- **SessionStart event**: if the index is older than `staleness_minutes`
  AND any source file is newer than the index, reindex.

All triggers silently no-op if `claudemem` isn't on PATH or the project
has no `.claudemem/` directory — safe to leave enabled on partially-
configured fleets.

Config (`hooks.claudemem_reindex`):

| Key | Default | Purpose |
|-----|---------|---------|
| `enabled` | `true` | Master toggle |
| `check_on_stop` | `true` | Reindex on turns that touched files |
| `check_on_session_start` | `true` | Staleness check when a new session opens |
| `staleness_minutes` | `10` | Cooldown — reindex at most every N min |
| `max_files_to_scan` | `2000` | Cap on the stale-scan walk (set higher for monorepos) |
| `ignored_dirs` | `[]` | Extra dir names to skip (appended to built-in: `.git`, `.claudemem`, `node_modules`, `.venv`, `__pycache__`, `.wolf`, `.caliber`, `dist`, `build`, …) |
| `lock_min_age_seconds` | `60` | Cooldown on the `.claudemem-reindex.lock` file to prevent pile-ups |

For **commit-time** reindexing on every project, run:
```bash
claudemem hooks install   # in each git repo
```

## Credits

The three optional hooks above are Python ports of the Bash hooks in
[rtfpessoa/code-factory](https://github.com/rtfpessoa/code-factory):

- `stop_guard` ← [`hooks/stop-phrase-guard.sh`](https://github.com/rtfpessoa/code-factory/blob/main/hooks/stop-phrase-guard.sh)
- `safety_scan` ← [`hooks/command-safety-scanner.sh`](https://github.com/rtfpessoa/code-factory/blob/main/hooks/command-safety-scanner.sh)
- `rtk_rewrite` ← [`hooks/rtk-rewrite.sh`](https://github.com/rtfpessoa/code-factory/blob/main/hooks/rtk-rewrite.sh)

Design changes for claude-hooks: pure-Python implementation (no bash /
jq dependency), pattern lists surfaced as config, integration between
`rtk_rewrite` and `safety_scan` so rewrites are still scanned before
auto-approval. See
[`docs/PLAN-code-factory-integration.md`](docs/PLAN-code-factory-integration.md)
for the full integration plan.

## Scripts

### `scripts/install-caliber-hook.sh` — fast caliber pre-commit hook

Caliber's default pre-commit hook runs `caliber refresh` and
`caliber learn finalize` synchronously — each calls an LLM, and on
event-heavy sessions the combined wait can block `git commit` for
**20 minutes or more**. This installer replaces that hook with a
portable non-blocking version:

- backgrounds `caliber refresh` so commits return instantly (refreshed
  docs land in the next commit)
- drops `caliber learn finalize` (the SessionEnd Claude Code hook
  already runs the `--auto` version, and 240-event LLM passes on the
  commit path were hitting caliber's internal 600 s timeout)
- bounds the inner Claude CLI call at 60 s via
  `CALIBER_CLAUDE_CLI_TIMEOUT_MS`
- wraps the refresh in GNU `timeout 30` when available (skipped on
  Windows Git Bash, where `timeout.exe` is a `sleep` with different
  semantics)

Measured on a 240-event session: `~20 min` → `~0.7 s` per commit.

```bash
sh scripts/install-caliber-hook.sh         # install / update
sh scripts/install-caliber-hook.sh --dry   # preview
```

Existing `.git/hooks/pre-commit` is backed up to
`.git/hooks/pre-commit.bak-<timestamp>` before being replaced. Re-run
on every machine that clones the repo — git doesn't version-control
`.git/hooks/` so the install can't be automatic.

### `scripts/openwolfstatus` — OpenWolf dashboard status

Shows all registered OpenWolf projects, their dashboard/daemon port
assignments, and PM2 process status. Warns if the PM2 state hasn't been
saved (i.e. new daemons won't survive a reboot).

```bash
# Linux
./scripts/openwolfstatus.sh

# Windows
scripts\openwolfstatus.bat
```

### PM2 auto-start on boot

OpenWolf daemons run under PM2. After starting or changing daemons, run
`pm2 save` to persist the process list. Then set up auto-start:

**Linux (systemd):**

```bash
pm2 startup          # generates and enables a systemd service (pm2-<user>)
pm2 save             # saves current process list for resurrection
```

This creates `/etc/systemd/system/pm2-<user>.service` which runs
`pm2 resurrect` on boot.

**Windows:**

```bash
npm install -g pm2-windows-startup
pm2-startup install  # adds a registry entry for auto-start on login
pm2 save             # saves current process list
```

This adds a `PM2` entry under
`HKCU\Software\Microsoft\Windows\CurrentVersion\Run` that launches
`pm2 resurrect` at login.

> **Important:** Every time you add or remove an OpenWolf daemon, run
> `pm2 save` again. Without it, the new daemon won't be restored after
> a reboot.

## Tests

```bash
pip install -r requirements-dev.txt   # pytest + coverage
python -m pytest tests/ -q            # 524 passed, 16 skipped (≈10 s)
```

Branch coverage gate (target ≥ 80 %):

```bash
coverage run -m pytest tests/
coverage report
# Phase 8 (test_coverage_phase8.py) brought branch coverage on
# claude_hooks/ from ~81 % to ~92 %. Re-run `coverage report` for
# the current figure — the number drifts as new modules ship.
```

### Test-file map

| File | Module under test | Tests | Notes |
|------|-------------------|-------|-------|
| `tests/conftest.py` | shared fixtures | — | `fake_provider`, `base_config`, `fake_transcript`, `transcript_file`, `tmp_claude_home` |
| `tests/mocks/ollama.py` | Ollama HTTP stubs | — | `mock_ollama_generate`, `mock_ollama_embeddings` |
| `tests/test_fixtures.py` | fixture smoke tests | 17 | Sanity-checks every fixture and mock |
| `tests/test_config.py` | `config.py` | 6 | merge, paths, project-disabled marker |
| `tests/test_dedup.py` | `dedup.py` | 11 | similarity, should_store, dedup w/ failing provider |
| `tests/test_decay.py` | `decay.py` | 23 | hash, recency / frequency boost, prune, atomic state I/O |
| `tests/test_embedders.py` | `embedders.py` | 14 | factory, Ollama / OpenAI clients, error paths |
| `tests/test_hyde.py` | `hyde.py` | 18 | grounded expansion, fallback model, `_format_context` cap |
| `tests/test_recall.py` | `recall.py` | 20 | full pipeline, dedup, OpenWolf injection, HyDE skip |
| `tests/test_handlers.py` | hook handlers | 28 | UserPromptSubmit / SessionStart / SessionEnd / Stop store half |
| `tests/test_pre_tool_use_handler.py` | `hooks/pre_tool_use.py` | 11 | safety + rtk integration |
| `tests/test_safety_scan.py` | `safety_scan.py` | 17 | dangerous-command detection, allow-list |
| `tests/test_rtk_rewrite.py` | `rtk_rewrite.py` | 12 | rewrite, version probe, opt-in policy |
| `tests/test_stop_guard.py` | `stop_guard.py` | 23 | default patterns, meta-context escape, **user-intent wrap-up escape** |
| `tests/test_instincts.py` | `instincts.py` | 13 | bug-fix detection, save / merge w/ frontmatter |
| `tests/test_reflect.py` | `reflect.py` | 12 | guards, Ollama failure, dedup across providers, append idempotency |
| `tests/test_consolidate.py` | `consolidate.py` | 16 | merge candidates, compress, state file, `should_run` cooldown |
| `tests/test_claudemem_reindex.py` | `claudemem_reindex.py` | 15 | lock cooldown, staleness scan, async spawn |
| `tests/test_openwolf.py` | `openwolf.py` | 9 | wolf-dir detection, anatomy / cerebrum read |
| `tests/test_dispatcher.py` | `dispatcher.py` | 6 | event routing |
| `tests/test_detect.py` | `detect.py` | 6 | MCP server discovery in `~/.claude.json` |
| `tests/test_mcp_client.py` | `mcp_client.py` | 6 | initialize → tools/call round-trip |
| `tests/test_providers.py` | provider registry | 9 | Qdrant / Memory KG signatures |
| `tests/test_pgvector_integration.py` | `providers/pgvector.py` | 8 (skipped w/o psycopg) | live Postgres |
| `tests/test_sqlite_vec_integration.py` | `providers/sqlite_vec.py` | 8 (skipped w/o sqlite-vec) | live sqlite-vec |
| `tests/test_coverage_phase8.py` | dispatcher / pre_tool_use / stop / providers / recall / safety_scan / claudemem_reindex | 120 | Phase 8 — error paths, edge cases, module-import failures (lifts coverage from 81 % → 92 %) |
| `tests/test_proxy.py` | `claude_hooks/proxy/` (P0) | 17 | Pass-through, JSONL logging, Warmup + synthetic detection |
| `tests/test_proxy_p1.py` | SSE tail + rate-limit state + weekly auto-populate | 22 | P1 observability half |
| `tests/test_proxy_p3.py` | `block_warmup` short-circuit | 7 | Stub builders + upstream-not-called invariant |
| `tests/test_statusline_usage.py` | `scripts/statusline_usage.py` | 16 | P4 segment rendering, stale detection, CLI safety |
| `tests/test_proxy_stats.py` | `scripts/proxy_stats.py` | 9 | Aggregation, per-model, JSON output, since/until window |
| `tests/test_claude_mem_ports.py` | ports 1-5 from thedotmack/claude-mem | 37 | XML summary, metadata filter, tag strip, composite hash, file-read gate |

> Before merging: run `python -m pytest tests/` (0 failures) and
> `coverage report` (≥ 80 %). Both are part of the conda-env workflow
> documented at the top of this section.

## Recommended Companion Tools

See [COMPANION_TOOLS.md](COMPANION_TOOLS.md) for detailed install
instructions, descriptions, and importance rankings for tools that
complement claude-hooks.

## License

[MIT](LICENSE)

## Inspiration

- [openwolf](https://github.com/cytostack/openwolf) -- project-anatomy tracking
- [claude-mem](https://github.com/thedotmack/claude-mem) -- progressive disclosure
- [vestige](https://github.com/samvallad33/vestige) -- HyDE query expansion
- [claude-cognitive](https://github.com/GMaN1911/claude-cognitive) -- attention decay
- [everything-claude-code](https://github.com/affaan-m/everything-claude-code) -- instincts
- [claude-diary](https://github.com/rlancemartin/claude-diary) -- /reflect synthesis
- [mnemex](https://github.com/MadAppGang/mnemex) -- semantic code search
- [caliber](https://github.com/caliber-ai-org/ai-setup) -- config drift detection
- [episodic-memory](https://github.com/obra/episodic-memory) -- transcript search
