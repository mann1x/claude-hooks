# claude-hooks

Cross-platform Claude Code hooks that auto-recall from **Qdrant** + **Memory KG**
on every prompt and write findings back at the end of the turn.

Built so installing it once at the **user level** wires recall + storage
into every Claude Code project automatically — no per-project init.

> Full design and rationale: [`CLAUDE.md`](CLAUDE.md)

---

## What it does

```
user prompt
   │
   ▼
[UserPromptSubmit hook] ──► recall from qdrant + memory_kg ──► inject as additionalContext
   │
   ▼
Claude responds (knowing the prior context, deterministically)
   │
   ▼
[Stop hook] ──► summarize the noteworthy turn ──► store back to providers
```

The model still reads the recalled memory the same way it reads `CLAUDE.md`
or any other prompt context — but now the recall happens **deterministically**,
not "if the model decides to call the search tool". Same on the way out:
every noteworthy turn gets written back without you (or the model) having
to remember.

## Features

- **Stdlib only** for the core (qdrant + memory_kg providers, hook
  dispatcher, installer). No `pip install` to run it.
- **Python 3.9+**, runs identically on Linux, macOS, and Windows.
- **Auto-detection** of MCP servers from `~/.claude.json` — name matching
  + tool-signature probing. Asks if ambiguous, prompts if missing.
- **Plugin model**: each memory backend is one file. Built-in:
  - [`qdrant`](claude_hooks/providers/qdrant.py) — via `mcp-server-qdrant`
  - [`memory_kg`](claude_hooks/providers/memory_kg.py) — via `@modelcontextprotocol/server-memory`
  - [`pgvector`](claude_hooks/providers/pgvector.py) — *experimental scaffold*, optional `psycopg`
  - [`sqlite_vec`](claude_hooks/providers/sqlite_vec.py) — *experimental scaffold*, optional `sqlite-vec`
- **System-wide install**: hooks live in `~/.claude/settings.json`, the
  repo lives once on disk, every project gets recall automatically.
  Per-project opt-out via a `.claude-hooks-disable` marker file.
- **Non-blocking by design**: every hook exits 0 even on failure. Network
  timeouts, broken providers, missing config — none of it ever blocks
  Claude from responding.

## Requirements

- **Python 3.9+** on PATH (`python3` on Linux/macOS, `py` or `python` on
  Windows). Stdlib only — no extra packages for the default Qdrant +
  Memory KG setup.
- **Claude Code** with hooks support (any current version).
- **A Qdrant MCP server** and/or a **Memory KG MCP server** reachable
  over HTTP. The installer auto-detects them from your `~/.claude.json`.
- *(Optional)* For the experimental scaffolds:
  - **pgvector**: Postgres + `CREATE EXTENSION vector;` + `pip install psycopg[binary]`
  - **sqlite-vec**: `pip install sqlite-vec`
  - both also need an embedder — local Ollama with `nomic-embed-text`
    works out of the box.

## Install

```bash
git clone <wherever you stash this>  /shared/dev/claude-hooks
cd /shared/dev/claude-hooks
python3 install.py
```

The installer:

1. Reads `~/.claude.json` and finds your existing MCP servers.
2. Asks you to confirm which one is qdrant, which is memory_kg.
   (For most setups it picks correctly without asking — name match wins.)
3. Verifies them with a real `tools/call`.
4. Writes `config/claude-hooks.json` with the chosen URLs.
5. Backs up `~/.claude/settings.json` and merges in the hook entries.
   Existing user-authored hooks are left alone — only entries tagged
   `_managedBy: "claude-hooks"` are touched on re-runs.

Open a new Claude Code session and the hooks will fire on the next prompt.
Logs go to `~/.claude/claude-hooks.log`.

### Windows

```powershell
git clone <wherever> C:\dev\claude-hooks
cd C:\dev\claude-hooks
python install.py
```

The installer detects Windows, writes the right path
(`bin\claude-hook.cmd`) into `%USERPROFILE%\.claude\settings.json`, and
you're done. The same Python source runs on both OSes — there's no
PowerShell port.

### Flags

```bash
python3 install.py --dry-run         # show changes, don't write
python3 install.py --non-interactive # CI-friendly, fail on prompts
python3 install.py --uninstall       # remove all claude-hooks entries
python3 install.py --probe           # force tool-probe detection
python3 install.py --config <path>   # alternate config file location
```

## Configuration

After install, `config/claude-hooks.json` lives in the repo (gitignored).
Edit it to:

- Disable individual providers (`providers.<name>.enabled: false`)
- Change recall depth (`providers.<name>.recall_k: 10`)
- Change `store_mode` (`auto` / `ask` / `off`)
- Enable the opt-in `pre_tool_use` warning hook (`hooks.pre_tool_use.enabled: true`)
- Tune the `min_prompt_chars` threshold so short prompts skip recall
- Point at a different MCP server URL

Full schema with comments: [`config/claude-hooks.example.json`](config/claude-hooks.example.json).

## Per-project opt-out

Drop a file called `.claude-hooks-disable` in any project root. The
dispatcher walks up from the current cwd looking for the marker; if it
finds one, the hooks no-op silently for that project (and all subdirs).

```bash
touch /srv/sensitive-client-project/.claude-hooks-disable
```

## Adding a new provider

1. Create `claude_hooks/providers/<name>.py` implementing the
   [`Provider`](claude_hooks/providers/base.py) ABC. The contract is four
   methods: `detect`, `verify`, `recall`, `store`. Look at
   [`qdrant.py`](claude_hooks/providers/qdrant.py) for a working example.
2. Add it to `REGISTRY` in
   [`claude_hooks/providers/__init__.py`](claude_hooks/providers/__init__.py).
3. Add a config block to `DEFAULT_CONFIG` in
   [`claude_hooks/config.py`](claude_hooks/config.py).
4. Re-run `python3 install.py`.

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
│                          │                  │  pgvector   │ ──► Postgres   (opt-in)
│                          │                  │  sqlite_vec │ ──► local DB   (opt-in)
│                          │                  └──────┬──────┘      │
│                          ▼                         │             │
│                  additionalContext ◄───────────────┘             │
│                                                                  │
│   assistant turn ends ──► [Stop hook] ──► providers.store(...)   │
└──────────────────────────────────────────────────────────────────┘
```

See [`CLAUDE.md`](CLAUDE.md) for the full architecture, file layout,
hook event reference, and design rationale.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

32 tests covering the MCP client, the providers' parsing logic, the
detector, the dispatcher, and the config loader. Stdlib `unittest` —
no `pytest` dependency.

## License

[MIT](LICENSE) — see the LICENSE file. Use it however you like.

## Inspiration

The hook pattern is borrowed from [openwolf](https://github.com/cytostack/openwolf),
which uses the same idea for project-anatomy tracking and token metering.
claude-hooks does the same thing for memory recall + storage. The two
don't conflict — both can coexist under the same Claude Code config.
