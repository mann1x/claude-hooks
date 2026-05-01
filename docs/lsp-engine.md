# LSP Engine — User Guide

The LSP engine is a session-scoped daemon that loads a project's
language servers once, follows your edits in real time, and answers
hover / definition / diagnostics queries through a UNIX-socket IPC.
It complements the always-on `PostToolUse` ruff hook (Python only,
~50 ms) and the on-demand `cclsp` MCP (multi-language, per-call) by
giving Claude Code a third option: **persistent, multi-language,
single-digit-millisecond latency**, with one daemon shared by every
session in the same project.

If you've never installed an LSP for this project before, start
with [`docs/lsp-mcp.md`](lsp-mcp.md) — that covers `pyright` /
`gopls` / `rust-analyzer` / `clangd` / `OmniSharp` and the
`cclsp.json` config the engine reads. The engine is a layer on top
of cclsp's config; if cclsp works, the engine has everything it
needs to start.

For the design rationale and decision log, see
[`docs/PLAN-lsp-engine.md`](PLAN-lsp-engine.md). This page is the
**user manual**.

---

## When to use which

| Layer | Default | Latency | Coverage |
|---|---|---|---|
| `PostToolUse` ruff hook | **on** | ~50 ms | Python only, lints |
| `cclsp` MCP | opt-in | ~100-500 ms / call | All languages, on-demand |
| **LSP engine (this doc)** | opt-in | ~5-15 ms / call | All languages, persistent |

The engine wins for: long sessions on a large project, multi-language
codebases, anything where you'll query the LSP many times per
minute. The other two are simpler — turn the engine on once you
feel the per-call cost of `cclsp` adding up.

---

## Quick start

```bash
# 1. Verify cclsp.json is configured
cclsp --version           # confirm cclsp is on PATH
cat $CCLSP_CONFIG_PATH    # or the default at ~/.config/cclsp/cclsp.json

# 2. (Optional) configure engine knobs
mkdir -p .claude-hooks
$EDITOR .claude-hooks/lsp-engine.toml

# 3. Verify the daemon for this project
python -m claude_hooks.lsp_engine status --project .
# {"running": false, "pid": null, "socket": ".../daemon.sock"}

# 4. Start it (fork-and-detach via the spawn flow). Most users
#    don't run this by hand — the hook integration spawns it on
#    SessionStart. But you can drive it manually for debugging:
python -m claude_hooks.lsp_engine daemon --project . &

# 5. Re-check status — should now show running, with sessions=[],
#    open_files=[], etc.
python -m claude_hooks.lsp_engine status --project .
```

---

## Architecture

Three layers, each opt-in but stacking:

```
┌─────────────────────────────────────────────────────────────┐
│ Claude Code session                                         │
│                                                             │
│   PostToolUse hook ─► LspEngineClient                       │
│                          │                                  │
│                          ▼ UNIX socket                      │
│   ┌──────────────────────────────────────────────────┐      │
│   │ Daemon (one per project, shared across sessions) │      │
│   │                                                  │      │
│   │  ┌────────────────────┐                          │      │
│   │  │ SessionLockManager │ ← per-file affinity       │     │
│   │  └────────────────────┘   (Decision 5)            │     │
│   │                                                  │      │
│   │  ┌────────────────────┐                          │      │
│   │  │ Engine             │ ← multi-LSP routing      │      │
│   │  │  ┌──────┐ ┌──────┐ │                          │      │
│   │  │  │pyright││gopls │ │ … per-language LspClient │      │
│   │  │  └──────┘ └──────┘ │                          │      │
│   │  └────────────────────┘                          │      │
│   │                                                  │      │
│   │  ┌────────────────────┐                          │      │
│   │  │ CompileOrchestrator│ ← opt-in compile-aware   │      │
│   │  │  cargo / tsc / mypy│   (Phase 3)              │      │
│   │  └────────────────────┘                          │      │
│   │                                                  │      │
│   │  ┌────────────────────┐                          │      │
│   │  │ GitWatcher         │ ← bulk re-didOpen on     │      │
│   │  │  HEAD / refs poll  │   branch switch (Phase 2)│      │
│   │  └────────────────────┘                          │      │
│   └──────────────────────────────────────────────────┘      │
└─────────────────────────────────────────────────────────────┘
```

### Daemon lifecycle

- **One per project**, keyed by absolute project root (sha256
  prefix). Two sessions in the same repo share the same daemon;
  sessions in different repos get their own.
- State directory: `~/.claude/lsp-engine/<sha256-prefix>/` containing
  `daemon.sock` (UNIX socket), `daemon.lock` (PID + flock), and
  `project` (the absolute path, for human inspection).
- Spawn flow: `connect_or_spawn(project_root)` from the hook side.
  If the socket isn't live, fork-and-execs `python -m
  claude_hooks.lsp_engine daemon --project <root>` detached and polls
  for the socket to come up. Race-safe via `flock(LOCK_EX | LOCK_NB)`.
- Reaped: when the last session detaches, OR on `SIGTERM` / `SIGINT`,
  OR via the `shutdown` IPC op.

### Per-file session affinity locks

The hardest design point in the engine. Two sessions on the same
daemon can both edit the same file; the LSP only keeps the last
write. Without coordination, session A's next query would see
session B's clobber.

**Resolution:** first session to `did_change` a file owns it for
30 s (`session_locks.debounce_seconds`, configurable). Other
sessions touching the same file get queued — their `did_change`
waits, their queries block briefly (`session_locks.query_timeout_ms`,
default 500 ms) before serving the owner's view as a stale
fallback.

Sessions editing **different files** never contend.
Branch/worktree workflows are unaffected because each worktree has
its own absolute path, so each gets its own daemon.

For the full state-machine spec, see
[`PLAN-lsp-engine.md` Decision 5](PLAN-lsp-engine.md#decisions-locked-2026-04-30).

### Adaptive preload (Phase 2)

On daemon start, a background thread walks
`graphify-out/graph.json` (built by the in-tree `code_graph`
package), ranks modules by `imports`-edge in-degree, and eager-
`did_open`s the top-N (default 200) hot files into their LSPs.

This catches the most-frequently-touched files in the LSP's index
*before* the user's first query, without paying the cost of
preloading the whole project on a 1 M-LOC monorepo. The long tail
loads lazily on first edit.

Soft-fails when `graph.json` is absent — engine still works in
fully-lazy mode.

### Git branch-switch awareness (Phase 2)

A polling watcher (1 Hz, configurable) reads `.git/HEAD` and the
current branch's ref file. On any change — branch switch, pull,
reset, rebase — fires a callback that bulk-`did_change`s every
open file with the on-disk content. The LSP's in-memory copy stays
consistent with the new branch's worktree.

We don't clear lock state on refresh — sessions with queued
`did_change`s continue to drain. If a queued edit is stale relative
to the new branch, that's a "you edited on the wrong branch" issue
surfaced at next query, not something we paper over.

Inactive for non-git projects.

### Compile-aware diagnostics (Phase 3, opt-in)

Disabled by default. When enabled, runs your project's compile
commands (`cargo check`, `tsc --noEmit`, `mypy`, etc) on a
debounced background schedule and merges their diagnostics into
the same `diagnostics` op response the LSPs feed. Diagnostics from
each layer are distinguished by the `source` field
(`pyright` / `cargo` / `tsc` / etc), so consumers can filter.

LSPs answer "is the *file* well-formed?" — they don't run the
build system. Cargo's borrow-checker errors, TypeScript's
project-wide type-narrowing, mypy's full type analysis only land
via the actual compile pass. Pairing the two gives the hook a
complete answer: LSP for fast per-file feedback, compile for the
truth a build would surface.

To configure, see [Compile-aware setup](#compile-aware-setup) below
or run the `/setup-compile-aware` skill, which detects your
project's build tools and proposes the TOML block for you.

---

## Configuration

Two files, layered:

1. **`cclsp.json`** — canonical source for LSP commands and
   extensions. Same file `cclsp` reads. The engine never writes to
   it. See [`docs/lsp-mcp.md`](lsp-mcp.md) for the format.
2. **`.claude-hooks/lsp-engine.toml`** (per-project, optional) —
   engine-specific knobs. TOML so you can leave `# reason: …`
   comments on hand-edited values.

### `.claude-hooks/lsp-engine.toml` reference

All sections are optional; missing keys fall back to the defaults
shown.

```toml
# .claude-hooks/lsp-engine.toml
#
# Engine-specific knobs for claude-hooks' LSP engine. cclsp.json
# is the canonical source for which LSPs run on which extensions —
# this file only controls *engine* behavior on top.

[preload]
# Adaptive preload: walks graphify-out/graph.json, eager-didOpens
# the top-N most-imported files into the LSP. Catches hot files
# before the first query; long tail loads lazily on first edit.
max_files = 200          # cap on eager-opens; 0 = unlimited (not recommended)
use_code_graph = true    # set false to disable preload entirely

[session_locks]
# Per-file affinity locks (Decision 5). First session to did_change
# a file owns it for debounce_seconds; other sessions' edits queue.
debounce_seconds = 30.0
query_timeout_ms = 500   # how long a non-owner's query blocks before
                         # serving the owner's view as a stale fallback

[memory]
# Soft cap per LSP child. rust-analyzer ~100 MB / 10K LOC, so this
# is a guard against runaway monorepos.
max_files_per_lsp = 500

[compile_aware]
# Opt-in compile-aware diagnostics. Default off — the daemon won't
# spawn any compile processes unless `enabled = true` AND at least
# one [compile_aware.commands] entry is configured.
enabled = false

[compile_aware.commands]
# language extension (no leading dot) → command vector to run
# cwd defaults to project root. The orchestrator debounces rapid
# triggers (1.5s default) so a flurry of did_changes coalesces
# into one compile run.
#
# Cargo emits structured diagnostics with --message-format=json;
# the parser auto-detects from the command line.
# rs = ["cargo", "check", "--message-format=json"]
#
# tsc, mypy, gcc, clang use a generic text format and parse via
# the file:line:col: severity: message regex.
# ts = ["tsc", "--noEmit"]
# py = ["mypy", "--strict", "src/"]
```

### `compile_aware.commands` — populating it

This is user-authored. There's **no auto-detection** (no "if
Cargo.toml exists, use cargo check") because:

- Different projects use different build tools (cargo workspace vs
  single crate, tsc vs deno, mypy vs pyright vs pyre).
- Different flags matter per project (`--release`, `--offline`,
  `--strict`, custom tsconfig path).
- Spawning a 30-second `cargo check` on every save without explicit
  consent would be its own bug.

You write the commands explicitly. The skill at
`/setup-compile-aware` is the helper — it scans your project,
proposes a complete block based on detected build tools, and only
writes after you confirm. See [Compile-aware setup](#compile-aware-setup).

### Where the per-project file lives

`.claude-hooks/lsp-engine.toml` at the project root, alongside any
existing `.claude-hooks/` directory. `git add` it — the
configuration is project state, like `pyproject.toml` or `tsconfig.json`.
It is *not* gitignored by default.

---

## Compile-aware setup

If you'd like Claude to propose the `[compile_aware.commands]`
block based on what's in your project:

```
/setup-compile-aware
```

The skill:

1. Walks your project for build-tool markers (`Cargo.toml`,
   `tsconfig.json`, `pyproject.toml`, `go.mod`, `Makefile`, etc).
2. Proposes a complete `[compile_aware.commands]` block with
   `# why:` comments on each entry.
3. Asks for your confirmation before writing anything.
4. If `compile_aware.enabled` is currently false, asks whether
   to flip it.

Re-run any time you add a new language to the project.

---

## CLI commands

```bash
# Run the daemon foreground (used by the spawn flow).
python -m claude_hooks.lsp_engine daemon --project /path/to/project

# Daemon options:
#   --state-base DIR        override ~/.claude/lsp-engine/
#   --cclsp-config PATH     override $CCLSP_CONFIG_PATH
#   --log-level DEBUG       INFO is default

# Inspect a running (or absent) daemon for a project.
python -m claude_hooks.lsp_engine status --project /path/to/project
# When running, prints a JSON blob:
# {
#   "running": true,
#   "pid": 12345,
#   "socket": "/root/.claude/lsp-engine/abc.../daemon.sock",
#   "project": "/path/to/project",
#   "sessions": ["session-A", "session-B"],
#   "open_files": ["file:///.../foo.py", ...],
#   "active_servers": ["pyright-langserver", "gopls"],
#   "held_uris": ["/path/to/project/foo.py"]
# }
```

---

## Daemon ops (over the IPC socket)

These are the IPC ops a hook (or `python -m claude_hooks.lsp_engine
status`) uses. You'd only call them directly when debugging.

| Op | Params | Returns |
|---|---|---|
| `attach` | — | `{ok: true}` (refcount++) |
| `detach` | — | `{ok: true}` (releases all session's locks) |
| `did_open` | `path`, `content` | `{ok: true, opened: bool}` |
| `did_change` | `path`, `content` | `{ok: true, forwarded: bool, queued_behind: str|null}` |
| `did_close` | `path` | `{ok: true, closed: bool}` |
| `diagnostics` | `path`, `timeout_ms?`, `diag_timeout_s?` | `{ok: true, diagnostics: [...], stale: bool}` |
| `status` | — | full status payload |
| `shutdown` | — | graceful daemon stop |

`stale: true` on `diagnostics` means the affinity lock didn't
release within `query_timeout_ms` and we forwarded anyway, serving
the owner's view (Decision 5).

---

## Troubleshooting

### Daemon won't start: `another daemon already holds daemon.lock`

Another instance is running for this project. Find it:

```bash
python -m claude_hooks.lsp_engine status --project .
# Returns the PID under "pid".
```

If that PID is dead but the lock file is still around (rare crash
scenario), `flock` will release on process death — try again. If
the lock genuinely is held by a live but stuck daemon:

```bash
kill <pid>
# Daemon catches SIGTERM and shuts down cleanly.
```

### `forwarded: false, queued_behind: <other-session>`

Another Claude Code session in the same project has the affinity
lock for that file. Either wait `debounce_seconds` for it to expire
(default 30 s with no further activity from them), or have that
session run `detach` (closes their connection / ends their Claude
Code session).

### Compile-aware fires nothing on save

Check that:

1. `compile_aware.enabled = true`
2. `[compile_aware.commands]` has an entry for your file's
   extension.
3. The command vector points at a binary on the daemon's `$PATH`.
4. The status op shows `"active_servers"` for the file's
   language — if the LSP isn't running, the daemon never sees a
   `did_open` to fan out to the orchestrator.

Logs at `~/.claude/lsp-engine/<hash>/daemon.log` (when set up via
`connect_or_spawn(log_path=…)`) or stdout/stderr if you ran the
daemon foreground.

### Diagnostics come back empty for a file you edited

Most likely the file's extension isn't claimed by any server in
`cclsp.json`. The engine returns `[]` (not an error) for
unconfigured extensions — it can't make up an LSP for `.txt`.
Check `cclsp.json`:

```bash
cat $CCLSP_CONFIG_PATH | jq '.servers[].extensions'
```

### The daemon survived my Claude Code crash

By design — the daemon is per-project, not per-session. Other
sessions in the same project are still using it. It reaps when the
last session detaches OR on the next `kill <pid>`. Manual cleanup:

```bash
python -m claude_hooks.lsp_engine status --project .
kill <pid>   # graceful
```

---

## Phase status

The engine ships in phases. Current state:

| Phase | What | Status |
|---|---|---|
| 0 | Config schema + LSP child wrapper | ✅ shipped |
| 1 | Shared per-project daemon + session affinity locks | ✅ shipped |
| 2 | Adaptive preload + git branch-switch awareness | ✅ shipped |
| 3 | Opt-in compile-aware diagnostics | ✅ shipped |
| 4 | Windows parity (named pipes) + benchmarks | not yet |

The POSIX surface is feature-complete. Windows users should fall
back to `cclsp` directly until Phase 4.

---

## Related docs

- [`docs/lsp-mcp.md`](lsp-mcp.md) — installing the underlying LSPs
  and writing `cclsp.json`. **Read this first** if you've never
  configured LSPs.
- [`docs/PLAN-lsp-engine.md`](PLAN-lsp-engine.md) — the design doc
  with locked decisions. Read for the "why" behind a behavior;
  this page covers the "how".
- [`COMPANION_TOOLS.md`](../COMPANION_TOOLS.md) §8 — short pitch
  for `cclsp` as the recommended baseline.
