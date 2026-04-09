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
[UserPromptSubmit hook] ──► HyDE expand ──► recall from providers ──► decay rank ──► inject
   │
   ▼
Claude responds (knowing the prior context, deterministically)
   │
   ▼
[Stop hook] ──► classify ──► dedup check ──► store ──► extract instincts
   │
   ▼
[SessionStart on compact] ──► full recall re-injection (memory recovery)
```

## Features

### Core (v0.1)
- **Stdlib only** for the core. No `pip install` to run it.
- **Python 3.9+**, runs identically on Linux, macOS, and Windows.
- **Auto-detection** of MCP servers from `~/.claude.json`
- **Plugin model**: each memory backend is one file (qdrant, memory_kg, pgvector, sqlite_vec)
- **OpenWolf integration**: injects Do-Not-Repeat and recent bugs from `.wolf/` projects
- **Non-blocking**: every hook exits 0 even on failure

### Intelligence (v0.2)
- **HyDE query expansion** — generates a hypothetical answer via Ollama before
  searching Qdrant, dramatically improving recall quality. Falls back to raw
  prompt if Ollama is unavailable.
- **Attention decay** — memories that haven't been recalled recently fade;
  frequently useful ones strengthen. Tracks history in a JSON file.
- **Memory dedup** — before storing, checks for near-duplicates using text
  similarity. Prevents Qdrant from accumulating redundant entries.
- **Observation classification** — tags stored memories as `fix`, `preference`,
  `decision`, `gotcha`, or `general` for better downstream filtering.
- **Compact recall** — when Claude Code compacts context, the SessionStart hook
  re-injects full recalled memory so the model recovers what it lost.
- **Instinct extraction** — when a bug-fix pattern is detected (error → edit),
  auto-extracts it as a reusable markdown instinct file under `~/.claude/instincts/`.
- **Progressive disclosure** — optional: inject only the first line of each memory
  with a char-count hint, cutting injected context by ~3-5x.
- **`/reflect` synthesis** — CLI command that analyzes recent memories for
  recurring patterns and generates CLAUDE.md rules. Uses Ollama.
- **Autonomous consolidation** — CLI command to find duplicates, compress old
  memories, and prune stale ones. Uses Ollama.

### Companion Tools
- **mnemex/claudemem** — semantic code search with AST-aware chunking (installed globally)
- **claudekit** — git checkpoints and hook performance profiling (installed globally)
- **caliber** — config drift detection (`npx @rely-ai/caliber bootstrap`)
- **claude-code-organizer** — MCP security scanner + token budget (`npx @mcpware/claude-code-organizer`)
- **episodic-memory** — transcript search (Claude Code plugin, see manual steps below)

## Requirements

- **Python 3.9+**. Stdlib only for the default Qdrant + Memory KG setup.
- **Claude Code** with hooks support.
- **A Qdrant MCP server** and/or a **Memory KG MCP server** over HTTP.
- *(Optional)* **Ollama** for HyDE, /reflect, and consolidation features.

## Install

### 1. Set up the conda environment

```bash
conda create -n claude-hooks python=3.11 -y
conda activate claude-hooks
pip install -r requirements-dev.txt          # pytest (for testing)
```

### 2. Run the installer

```bash
cd /shared/dev/claude-hooks
python install.py
```

If the conda env doesn't exist yet, the installer will **offer to create
it and install all requirements** automatically.

### Flags

```bash
python install.py --dry-run         # show changes, don't write
python install.py --non-interactive # CI-friendly, fail on prompts
python install.py --uninstall       # remove all claude-hooks entries
python install.py --probe           # force tool-probe detection
```

## Configuration

After install, `config/claude-hooks.json` lives in the repo (gitignored).
Full schema: [`config/claude-hooks.example.json`](config/claude-hooks.example.json).

### v0.2 features (all opt-in via config)

| Feature | Config key | Default |
|---------|-----------|---------|
| HyDE query expansion | `hooks.user_prompt_submit.hyde_enabled` | `false` |
| Attention decay | `hooks.user_prompt_submit.decay_enabled` | `false` |
| Progressive disclosure | `hooks.user_prompt_submit.progressive` | `false` |
| Memory dedup | `providers.qdrant.dedup_threshold` | `0.0` (set to `0.85`) |
| Observation classification | `hooks.stop.classify_observations` | `true` |
| Compact recall | `hooks.session_start.compact_recall` | `true` |
| Instinct extraction | `hooks.stop.extract_instincts` | `false` |
| /reflect synthesis | `reflect.enabled` | `true` |
| Consolidation | `consolidate.enabled` | `false` |

### Important notes

- **Qdrant `collection`**: Newer `mcp-server-qdrant` versions configure the
  collection server-side. The provider handles both modes transparently.

- **HyDE model**: Default is `gemma4:e2b` with `qwen3:4b` fallback. Requires
  Ollama running. Falls back to raw prompt if Ollama is down (e.g., during training).

- **Conda env**: `bin/claude-hook` prefers `/root/anaconda3/envs/claude-hooks/bin/python`.
  Edit line 19 if your anaconda is elsewhere.

## CLI Commands

```bash
# Reflect: analyze recent memories for patterns → CLAUDE.md rules
python -m claude_hooks.reflect
python -m claude_hooks.reflect --dry-run

# Consolidate: deduplicate and compress old memories
python -m claude_hooks.consolidate
python -m claude_hooks.consolidate --dry-run
```

## Companion Tools

These are installed separately and complement claude-hooks:

### mnemex/claudemem (semantic code search)

```bash
npm install -g mnemex
mnemex setup                    # interactive: pick Ollama + snowflake-arctic-embed2
```

**Known bug** ([MadAppGang/mnemex#4](https://github.com/MadAppGang/mnemex/issues/4)):
add `"openrouterApiKey": "dummy"` to `~/.claudemem/config.json` — the
tool checks for this key before reading the embedding provider config.

```bash
mnemex index .                  # index a project
mnemex search "how does X work" # semantic search
```

### claudekit (git checkpoints + hook profiling)

```bash
npm install -g claudekit
```

Use `/checkpoint:create` and `/checkpoint:restore` in Claude Code sessions.
Profile hook performance with `claudekit-hooks profile`.

### caliber (config drift detection)

```bash
npm install -g @rely-ai/caliber
caliber hooks --install         # pre-commit hook for auto-sync
caliber score                   # check config quality (aim for 85+)
caliber learn install           # enable session learning
```

### claude-code-organizer (security scanner + token budget)

```bash
npx @mcpware/claude-code-organizer   # launches dashboard at http://localhost:3847
```

### episodic-memory (transcript search)

```bash
# Build from source (requires Node 22+)
git clone https://github.com/obra/episodic-memory /shared/dev/episodic-memory
cd /shared/dev/episodic-memory && npm install && npm link

episodic-memory sync            # index past conversations
episodic-memory search "query"  # search across all sessions
```

## Per-project opt-out

```bash
touch /srv/sensitive-project/.claude-hooks-disable
```

## Uninstall

```bash
python install.py --uninstall   # removes hooks from ~/.claude/settings.json
```

This only removes the 4 hook entries tagged `_managedBy: "claude-hooks"`.
Your other hooks and settings are left intact. The config file and repo
can be deleted manually if you want a full cleanup.

## Tests

```bash
conda activate claude-hooks
python -m pytest tests/ -v      # 42 tests
```

## License

[MIT](LICENSE)

## Inspiration

- [openwolf](https://github.com/cytostack/openwolf) — project-anatomy tracking
- [claude-mem](https://github.com/thedotmack/claude-mem) — progressive disclosure
- [vestige](https://github.com/samvallad33/vestige) — HyDE query expansion
- [claude-cognitive](https://github.com/GMaN1911/claude-cognitive) — attention decay
- [everything-claude-code](https://github.com/affaan-m/everything-claude-code) — instincts
- [claude-diary](https://github.com/rlancemartin/claude-diary) — /reflect synthesis
- [mnemex](https://github.com/MadAppGang/mnemex) — semantic code search
- [caliber](https://github.com/caliber-ai-org/ai-setup) — config drift detection
- [episodic-memory](https://github.com/obra/episodic-memory) — transcript search
