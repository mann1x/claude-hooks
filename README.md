# claude-hooks

Cross-platform Claude Code hooks that auto-recall from **Qdrant** + **Memory KG**
on every prompt and write findings back at the end of the turn.

Install once at the **user level** and every Claude Code session gets
deterministic memory recall + storage — no per-project init, no model
forgetting.

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
- **Stdlib only** for the core. No `pip install` needed.
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

## Requirements

- **Python 3.9+**. Stdlib only for the default Qdrant + Memory KG setup.
- **Claude Code** with hooks support.
- **A Qdrant MCP server** and/or a **Memory KG MCP server** over HTTP.
- *(Optional)* **Ollama** for HyDE, /reflect, and consolidation features.

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

### HyDE model

Default: `gemma4:e2b` with `qwen3:4b` fallback. Any small Ollama model
works -- it just needs to produce a short hypothetical answer for search
expansion. If Ollama is down, HyDE degrades gracefully to the raw prompt.

## CLI Commands

```bash
# Reflect: analyze recent memories for patterns -> CLAUDE.md rules
python -m claude_hooks.reflect
python -m claude_hooks.reflect --dry-run

# Consolidate: deduplicate and compress old memories
python -m claude_hooks.consolidate
python -m claude_hooks.consolidate --dry-run
```

Both commands are also available as `/reflect` and `/consolidate` slash
commands inside Claude Code if you install the skills (see `CLAUDE.md`).

## Per-project opt-out

```bash
touch your-project/.claude-hooks-disable
```

Any directory with this marker file (or any ancestor) will skip all hooks.

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
are the entire contract.

## Tests

```bash
pip install -r requirements-dev.txt   # just pytest
python -m pytest tests/ -v            # 42 tests
```

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
