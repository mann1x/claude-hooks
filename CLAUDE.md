# claude-hooks

A small, cross-platform (Linux + Windows) hook framework for Claude Code that
auto-injects relevant prior knowledge from **Qdrant** (semantic memory) and the
**Memory KG** (knowledge graph) into every conversation, and writes new
findings back at the end of the turn.

The hooks are pluggable: each memory backend is a *provider*, so adding a new
store (Postgres pgvector, Weaviate, sqlite-vec, …) is one file under
`claude_hooks/providers/`, no changes elsewhere.

> Status: **design / proposal**. The repo currently contains only this design
> doc and the bare skeleton. After review, the implementation lands under
> `claude_hooks/` and `bin/`.

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

## File layout

```
claude-hooks/
├── CLAUDE.md                       # this file
├── README.md                       # short human-facing summary
├── .gitignore
├── install.py                      # cross-platform installer
├── bin/
│   ├── claude-hook                 # POSIX entry: exec python3 .../run.py
│   └── claude-hook.cmd             # Windows entry: python .../run.py
├── run.py                          # thin wrapper: sys.path + dispatch
├── claude_hooks/
│   ├── __init__.py
│   ├── dispatcher.py               # dispatches event → handler
│   ├── config.py                   # load/save claude-hooks.json
│   ├── mcp_client.py               # minimal MCP HTTP client
│   ├── detect.py                   # MCP server detection from ~/.claude.json
│   ├── providers/
│   │   ├── __init__.py
│   │   ├── base.py                 # Provider ABC
│   │   ├── qdrant.py               # Qdrant via mcp__qdrant__qdrant-find/store
│   │   └── memory_kg.py            # Memory KG via mcp__memory__search_nodes/...
│   └── hooks/
│       ├── __init__.py
│       ├── user_prompt_submit.py   # recall on prompt
│       ├── session_start.py        # recall + status line on session begin
│       ├── stop.py                 # store on turn end
│       └── pre_tool_use.py         # optional: warn on risky tools
├── config/
│   ├── claude-hooks.json           # local, generated by installer (gitignored)
│   └── claude-hooks.example.json   # example with all knobs
└── tests/
    ├── test_mcp_client.py
    ├── test_detect.py
    └── test_providers.py
```

---

## Hook events used

From the 26 events Claude Code currently exposes, we use 4 by default and
have 1 more available as opt-in:

| Event              | Why                                                              | Default |
|--------------------|------------------------------------------------------------------|---------|
| `SessionStart`     | Inject "you have N memories about this project" status line     | on      |
| `UserPromptSubmit` | Recall from all providers, inject as `additionalContext`         | on      |
| `Stop`             | Summarize the turn, optionally write to providers                | on      |
| `SessionEnd`       | Final flush of any buffered observations                         | on      |
| `PreToolUse`       | Match `Bash`/`Edit` and warn on patterns flagged in past mistakes | off    |

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

The store is **opt-in by default** (`store_mode: "ask"` → emits a
`systemMessage` with a one-line "store this turn? y/n" prompt), but can
be set to `"auto"` for fully autonomous capture or `"off"` to disable.

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

**Language: Python 3.9+** (stdlib only). Justification:

- Already installed on solidPC (`/usr/bin/python3`, 3.9.2) and on every
  modern Windows install (Microsoft Store python or `py` launcher).
- `pwsh` is **not** installed on solidPC and would have to be added; `python`
  is already there. PowerShell-only would have meant a runtime install on
  Linux, which fails the "works without ceremony" goal.
- Single source of truth (`.py`) runs identically on both OSes.
- `urllib.request` + `json` + `subprocess` cover everything we need; no
  pip dependencies, no venv.

The user invokes claude-hooks from PowerShell on Windows the same way
they'd invoke any other Python script — no .ps1 wrappers needed at the
hook level. The repo *does* ship `bin/claude-hook` and `bin/claude-hook.cmd`
shims so the entry in `settings.json` looks identical (`claude-hook
SessionStart`) regardless of OS.

### settings.json entries written by the installer

Linux:
```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [{ "type": "command",
                    "command": "/shared/dev/claude-hooks/bin/claude-hook UserPromptSubmit",
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
                    "command": "C:\\Users\\manni\\dev\\claude-hooks\\bin\\claude-hook.cmd UserPromptSubmit",
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
  "version": 1,
  "providers": {
    "qdrant": {
      "enabled": true,
      "mcp_url": "http://192.168.178.2:32775/mcp",
      "collection": "memory",
      "recall_k": 5,
      "store_mode": "auto"
    },
    "memory_kg": {
      "enabled": true,
      "mcp_url": "http://192.168.178.2:32776/mcp",
      "recall_k": 5,
      "store_mode": "ask"
    }
  },
  "hooks": {
    "user_prompt_submit": { "enabled": true,  "min_prompt_chars": 30 },
    "session_start":      { "enabled": true,  "show_status_line": true },
    "stop":               { "enabled": true,  "store_threshold": "noteworthy" },
    "session_end":        { "enabled": true },
    "pre_tool_use":       { "enabled": false, "warn_on": ["Bash", "Edit"] }
  },
  "logging": {
    "path": "~/.claude/claude-hooks.log",
    "level": "info"
  }
}
```

This file is **gitignored**. Only `claude-hooks.example.json` is committed.

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

## Open questions for review

1. **`store_mode` default** — should `Stop` auto-store noteworthy turns
   silently, or always ask? Auto is more useful but noisier on the model
   side (one extra system message per turn). Suggesting `auto` for qdrant
   and `ask` for memory_kg.

2. **Per-project vs user-global hooks** — current plan installs to
   `~/.claude/settings.json` (user-global, applies to every project). The
   alternative is `.claude/settings.json` per project. Recommending global
   so memories are project-agnostic, but easy to switch.

3. **Recall format** — markdown block vs JSON-in-fence vs plain prose.
   Going with markdown headings + bullet lists; the model parses those
   reliably.

4. **Should `pre_tool_use` ship enabled?** It's the most "magical" hook —
   warns about past mistakes before risky tools run. Useful but adds
   latency to every Bash/Edit call. Default off, opt-in via config.
