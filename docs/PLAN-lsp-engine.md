# Plan: session-scoped LSP engine — non-MCP, real-time, project-aware

**Status:** SPEC. No code. Decisions locked 2026-04-30 (see Decisions
section below). Phase 0 ready when implementation begins.

## Decisions (locked 2026-04-30)

The four decision points raised in this spec have been resolved:

1. **Engine lifecycle: shared per-project, per-session views.** One
   engine process per project (not per session). Sessions attach via
   the IPC socket like `axon-host` does for the code graph. Memory
   stays flat regardless of how many Claude Code sessions are open
   on the same repo. Cost: the engine is now a small daemon with
   lock-file + attach/detach protocol, with session-id scoping for
   `didChange` notifications so two sessions editing the same file
   don't fight.

2. **Compile-aware mode: Phase 3, opt-in only.** Build the
   `cargo check` / `tsc --noEmit` orchestration in Phase 3 per the
   original schedule, but ship it **disabled by default** with a
   per-project config flag. Surfaces the daemon-vs-one-shot design
   pressure early without forcing every user onto the slow path.

3. **Preload: adaptive (code_graph hot set).** SessionStart walks
   the existing `code_graph` to find the top-N most-imported files,
   `didOpen` those eagerly, and lazy-loads the rest on first edit.
   Best ergonomics-per-cost ratio. Hard dependency on the built-in
   code_graph being present (it always is in claude-hooks projects).
   Soft cap: top-200 files by in-degree, configurable.

4. **Config: layered on cclsp.json.** `cclsp.json` is the canonical
   source for LSP commands + extensions — the engine reads it
   directly so a user with cclsp already configured gets the engine
   for free. Engine-specific knobs (preload size, compile commands,
   debounce intervals, opt-in flags) live in
   `.claude-hooks/lsp-engine.toml` (per-project) or the global
   `config/claude-hooks.json` under `hooks.lsp_engine`. No schema
   drift risk if cclsp upstream changes its config shape — we never
   write to that file.

These decisions reshape the architecture sketch and phasing below.

**Goal:** give the PostToolUse hook (and any future hook that wants
semantic info) a sub-50 ms answer to "is this code valid?", "where is
this symbol defined?", "what's the type of this expression?", with
the same correctness as VSCode/Pylance — including the parts the
file alone can't tell you (project deps, cross-file types, build
graph). Today's `claude_hooks/hooks/post_tool_use.py` solves the
*shallow* version (single-file ruff, ~50 ms, no project context).
This spec covers the *deep* version.

The motivating user statement, paraphrased:

> When I edit in VSCode, an LSP gives me real-time feedback. Claude
> Code only finds out about errors by running the code. I want
> something between hooks and an MCP — an engine that loads the
> project, follows my edits, and answers hover/goto-def/lint
> questions in real time when the hook calls it.

## Why not just an LSP-MCP server (cclsp / mcp-language-server)

Both work, both are stateless-per-call: every `tools/call` re-opens
the file, the LSP server (re)indexes if it has to, the response
comes back. For a small project this is fine. For a 200 K-LOC
monorepo where the user is iterating on 20 files in 2 minutes,
each call eats a cold-start tax — the MCP server doesn't keep its
own model of "what's currently in the editor" because it has no
edit notifications, only one-shot file paths. The result is high
p99 latency and inconsistent type info on files Claude has just
edited but not flushed to disk yet.

The session-scoped engine fills the gap by:

1. Starting **once per Claude Code session** (matching SessionStart /
   SessionEnd hooks).
2. Running the LSP servers **persistently** under its own roof,
   sending `textDocument/didOpen` for every project file at boot.
3. Listening to **PostToolUse** events as `textDocument/didChange`
   notifications — so the LSP's in-memory model is always exactly
   what's on disk, with no re-index on each query.
4. Exposing a **lightweight IPC** (UNIX socket on POSIX, named pipe
   on Windows) that the hook talks to in <5 ms.

The MCP-style interface stays available via cclsp for clients that
want it (Claude Desktop, other tools); this engine is specifically
for the hook→engine path, which has different latency and freshness
demands than a general-purpose MCP.

## Architecture sketch

```
┌──────────── Claude Code session ────────────┐
│                                             │
│   SessionStart hook ──spawns──> lsp-engine  │
│                                    │        │
│                                    ▼        │
│                      ┌────────────────────┐ │
│                      │  lsp-engine        │ │
│                      │  (one per session) │ │
│                      │                    │ │
│                      │  per-language LSP  │ │
│                      │  pyright           │ │
│                      │  gopls             │ │
│                      │  rust-analyzer     │ │
│                      │  clangd            │ │
│                      │  omnisharp         │ │
│                      │                    │ │
│                      │  IPC server        │ │
│                      │  (UNIX socket /    │ │
│                      │   named pipe)      │ │
│                      └─────────┬──────────┘ │
│   PostToolUse hook ──didChange─┘             │
│   PostToolUse hook ──diagnostics?────►       │
│                       <──results───          │
│                                              │
│   SessionEnd hook ──teardown──>              │
│                                              │
└──────────────────────────────────────────────┘
```

## Lifecycle

| Phase | Trigger | What happens |
|---|---|---|
| Boot | `SessionStart` hook | Detect project type via the existing claude-hooks anatomy/code_graph + `cclsp.json`-style config. Spawn each needed LSP. Send LSP `initialize` + `initialized`. Walk the project tree, send `didOpen` per file (capped at N files; the rest open lazily on first query). Listen on a session-scoped IPC socket at `~/.claude/lsp-engine/<session_id>.sock`. |
| Edit | `PostToolUse` hook | For Edit/Write/MultiEdit on a tracked extension, send `textDocument/didChange` with the full new content (LSP servers compute the diff). Update the engine's in-memory mtime cache so subsequent `diagnostics?` calls don't race. |
| Query | Any hook | Open client connection, send `{op: "diagnostics" | "hover" | "definition" | ..., file, line?, col?}` JSON line, receive response. p50 target <20 ms, p99 <80 ms. |
| Teardown | `SessionEnd` hook | Send LSP `shutdown` + `exit` to each child, close socket, remove pid + sock files. Crash-safe: a stale sock from a previous session is detected at next SessionStart by pinging it; if dead, the file is unlinked. |

## Per-project / per-folder configuration

Same shape as `cclsp.json` (so users only configure once for both
the MCP path and this engine), but extended:

```jsonc
// .claude-hooks/lsp-engine.toml  (or  cclsp.json)
[engine]
preload_max_files = 500          // cap initial didOpen blast
preload_globs = ["src/**", "lib/**"]
preload_exclude_globs = ["**/.venv/**", "**/node_modules/**"]
ipc_timeout_ms = 80              // hook abandons after this
warmup_on_session_start = true   // off → spawn lazily on first query

[git]
follow_branch_switch = true      // restart engine on `git checkout`
follow_pull = true               // restart on `git pull` (LSP indexes are now stale)

[server.python]
extensions = [".py", ".pyi"]
command = ["pyright-langserver", "--stdio"]
init_options = { "settings.python.analysis.typeCheckingMode" = "basic" }

[server.rust]
extensions = [".rs"]
command = ["rust-analyzer"]
# rust-analyzer needs the workspace to be a Cargo workspace; engine
# detects Cargo.toml and refuses to start otherwise rather than
# erroring on every query.
```

The engine resolves config in this order: `$CWD/.claude-hooks/lsp-engine.toml`
→ `$CWD/cclsp.json` (compatible mode) → `~/.config/claude-hooks/lsp-engine.toml`
→ built-in defaults (the same five servers cclsp wires up today).

## Git awareness

Two specific behaviours, both opt-in but on by default:

1. **Branch switch** — `git checkout other-branch` invalidates LSP
   workspace state (different files, different deps).  Detected by
   the engine via `inotify` on `.git/HEAD`. On change, the engine
   sends every LSP a `workspace/didChangeWorkspaceFolders` cycle
   (close → re-open) so types reflect the new branch.
2. **Pull / reset** — `git pull` or `git reset --hard` rewrites
   tracked files outside our `didChange` stream. Detected by
   `inotify` on `.git/refs/heads/<branch>` mtime change. On change,
   re-walk the tree and send `didChange` for files whose disk
   content drifted from the engine's cached buffer.

Without these, the engine slowly desynchronises from git operations
and the model's diagnostics stop matching reality.

## IPC protocol (newline-delimited JSON)

Lighter than full LSP — the engine takes the LSP's raw responses
and trims them to what hooks actually need.

```jsonc
// Request
{"id": 1, "op": "diagnostics", "file": "src/foo.py"}
{"id": 2, "op": "hover", "file": "src/foo.py", "line": 42, "col": 10}
{"id": 3, "op": "definition", "file": "src/foo.py", "line": 42, "col": 10}
{"id": 4, "op": "lint", "file": "src/foo.py"}                     // ruff-equivalent
{"id": 5, "op": "didChange", "file": "src/foo.py", "content": "..."}

// Response — same shape as LSP, IDs preserved
{"id": 1, "result": [{"range": ..., "severity": 1, "code": "F821",
                       "message": "undefined name 'foo'"}]}
{"id": 5, "result": "ok"}                                          // ack
{"id": 99, "error": {"code": -32000, "message": "engine starting up"}}
```

## Latency targets

The hook can afford ~80 ms p99 between Edit and the next assistant
turn. Engine internals must aim well under that:

| Op | Target p50 | Target p99 | Notes |
|---|---|---|---|
| `didChange` (Python, ~500 LOC file) | 5 ms | 15 ms | LSP processes async; we don't await diagnostic recompute |
| `diagnostics` (cached) | 10 ms | 30 ms | Read-through cache populated by didChange's diagnostic-publish |
| `hover` | 15 ms | 60 ms | LSP round-trip; depends on LS implementation (rust-analyzer slowest) |
| `definition` | 15 ms | 60 ms | Same |
| Cold engine start | 1 s | 3 s | Once per session — never on hook critical path |

## Failure modes

| Failure | Behaviour |
|---|---|
| LSP server crashes mid-session | Engine spawns a new instance, replays `didOpen` for files that had `didChange`s. Hook gets `error: "lsp_unavailable"` for queries during the gap. |
| Engine itself crashes | SessionEnd cleans up; next SessionStart starts fresh. PostToolUse falls back to ruff-only (current behaviour). |
| IPC socket gone | Hook queries return `null`; the hook code falls back to ruff. Engine recovers on next SessionStart. |
| Project too large for `preload_globs` | Engine logs and falls back to lazy didOpen. p99 first-query latency rises; subsequent queries are fast. |
| User runs `git rebase -i` (massive rewrites) | engine sees one HEAD update, walks the tree, sends bulk didChange. Slow path (~1 s). |

## Build / compile-aware mode (optional)

Some answers genuinely need a compile, not just an LSP:

- "does this Rust crate still compile after my Edit?" — needs
  `cargo check`, not `rust-analyzer`'s incremental view (which lags
  on macro-heavy code).
- "do these C++ headers resolve?" — clangd handles this if compile
  commands are present, otherwise needs `cmake --build`.

The engine optionally runs a *background compiler* per project,
gated by the same config:

```jsonc
[server.rust]
compile_check = "cargo check --all-targets"
compile_check_debounce_ms = 1000     // batch rapid edits
```

The compiler runs in its own subprocess, debounced. Output is
parsed into the same diagnostics shape and merged into LSP
diagnostics on `op: diagnostics` queries.

This is **opt-in** — it's the slowest path and most projects don't
need it.

## Open questions

(Q1 / Q4 / preload / config are now locked — see Decisions section
at the top. The remaining genuinely open items:)

1. **How does the engine know when to STOP indexing?** Even with
   adaptive preload, the lazy long-tail path can blow up on 1 M-LOC
   monorepos when the user opens enough files. Need a soft cap +
   per-language memory budget (rust-analyzer uses ~100 MB / 10 K LOC).
   Decided in Phase 0 as a config flag with sane defaults.

2. **Does the engine speak LSP back?** Future Claude Code IDE
   integration might want raw LSP. Worth keeping the LSP surface
   intact internally even if hooks see the trimmed JSON. Easy to
   add later — non-blocking.

3. **Compile-aware mode — daemon or one-shot?** A persistent
   `cargo check --watch` would beat re-spawning, but cargo doesn't
   ship a watch mode. Third-party `cargo-watch` exists but is one
   more dep. Trade-off worth measuring during Phase 3 prototyping;
   ship the simpler one-shot first since the feature is opt-in.

4. **Windows parity.** UNIX sockets aren't available; named pipes
   work. `inotify` isn't either; `ReadDirectoryChangesW` is the
   equivalent. Tests already isolate platform-specific code (see
   memory entry on platform-test isolation), so the architecture
   transfers — just more `if os.name == "nt"` branches.

5. **Multi-session contention on shared engine.** Two sessions
   editing the same file concurrently each send `didChange`. Last
   write wins at the LSP level, but we need a session-tagged edit
   buffer so each session's *next query* sees its own latest
   intent rather than the other session's clobber. Investigate in
   Phase 1.

## How this slots into the existing repo

- New top-level directory: `claude_hooks/lsp_engine/` (mirroring
  `claude_hooks/caliber_proxy/` and `claude_hooks/pgvector_mcp/`
  shape).
- Files: `__main__.py` (CLI entry), `engine.py` (lifecycle), `lsp.py`
  (per-language LSP wrapper), `ipc.py` (socket server), `git_watch.py`
  (inotify HEAD/refs watcher), `config.py` (schema + loader).
- New systemd unit? **No.** This is per-session, spawned by the
  SessionStart hook into `~/.claude/lsp-engine/<session_id>/` and
  reaped by SessionEnd. systemd manages long-lived services; this
  isn't one.
- Hook integration: `claude_hooks/hooks/post_tool_use.py` grows a
  new stage **after** the ruff stage — query the engine for
  diagnostics/hover, append to the same `additionalContext` block.
  Engine unavailable → engine queries silently no-op, ruff still
  runs. **No regression** from current behaviour if the engine
  fails.
- Tests: `tests/test_lsp_engine.py` for the lifecycle / IPC / config
  layers (no real LSP — fake server). `tests/test_lsp_engine_real.py`
  for the smoke layer against real LSPs, gated by which-binary
  detection like `TestRealRuff` does today.

## Phasing

(Reflects locked decisions: shared daemon, adaptive preload, opt-in
compile-aware in Phase 3, layered config.)

1. **Phase 0 — config schema + LSP wrapper** (1-2 days). cclsp.json
   reader, `.claude-hooks/lsp-engine.toml` schema, per-language LSP
   child management. No daemon yet. Tests prove `didOpen → didChange
   → diagnostics` round-trips work against a fake LSP.
2. **Phase 1 — daemon lifecycle + IPC** (2 days, +1 vs original).
   Per-project shared daemon under `~/.claude/lsp-engine/<project>/`,
   lock file, attach/detach protocol, session-id-tagged edit buffer
   (resolves Q5 above). UNIX socket server. SessionStart attach,
   SessionEnd detach (last detach reaps). End-to-end: two sessions
   editing the same file see consistent diagnostics.
3. **Phase 2 — adaptive preload + git awareness** (1.5 days). Walk
   the existing `code_graph` for top-200 hot files, eager-didOpen
   those, lazy on the rest. HEAD watcher + bulk re-didOpen on
   branch switch.
4. **Phase 3 — opt-in compile-check** (1 day, opt-in only).
   `cargo check` / `tsc --noEmit` / etc, gated behind
   `lsp_engine.compile_aware: true`. Debouncing, output merge.
   Default off.
5. **Phase 4 — Windows parity + benchmarks** (1-2 days). Named
   pipes (`\\.\pipe\lsp-engine-<project>`), perf measurement
   against the cclsp baseline + the ruff-only baseline.

Total estimated effort: ~7.5 days of focused work, fits in a
two-week sprint. The +1 day in Phase 1 is the shared-daemon
overhead vs the per-session simpler design we ruled out.

## Decision points — RESOLVED

All four blocking decisions are now locked (see the Decisions
section at the top of this doc). Phase 0 can begin.

Phase 0 entry checklist:
- [ ] Skeleton `claude_hooks/lsp_engine/` package
- [ ] `cclsp.json` reader (canonical source for LSP wiring)
- [ ] `.claude-hooks/lsp-engine.toml` schema + loader (engine knobs)
- [ ] Per-language LSP child management (spawn, didOpen, shutdown)
- [ ] Daemon entry point + lock file (shared per-project mode)
- [ ] Tests: fake LSP server, didOpen → didChange → diagnostics

When you've answered those, Phase 0 can start.
