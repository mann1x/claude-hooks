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

## Commands Reference

### Slash commands (inside Claude Code)

These are available as skills after running the installer. Type the
command in the Claude Code prompt.

| Command | Requires | Description |
|---------|----------|-------------|
| `/reflect` | Ollama | Analyze recent memories for recurring patterns, generate CLAUDE.md rules |
| `/consolidate` | Ollama | Find duplicate memories, compress old entries, prune stale ones |
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
session-quitting phrases ("good stopping point", "continue in the next
session"), and permission-seeking mid-task ("should I continue?").
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

When `rtk_rewrite_enabled` and `safety_scan_enabled` are both on, rtk
runs first and the safety scanner runs on the **rewritten** command —
so dangerous content hidden behind chained commands
(`rtk ls && rm -rf`) is still caught before the rewrite is approved.

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
pip install -r requirements-dev.txt   # just pytest
python -m pytest tests/ -v            # 58 tests (42 unit + 16 integration)
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
