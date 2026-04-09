# claude-hooks

Cross-platform Claude Code hooks that auto-recall from **Qdrant** + **Memory KG**
on every prompt and write findings back at the end of the turn.

> See [`CLAUDE.md`](CLAUDE.md) for the full design and rationale.

## Status

**Design / proposal.** The implementation is not landed yet — the repo
currently contains only the design doc and skeleton. After the design is
approved, the installer + providers + hooks land under
`install.py`, `claude_hooks/`, and `bin/`.

## Quick goal

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
[Stop hook] ──► summarize the turn ──► store back to providers
```

## Requirements

- Python 3.9+ (stdlib only)
- Claude Code with hooks support
- A Qdrant MCP server and/or a Memory KG MCP server reachable over HTTP
  (the installer auto-detects them from `~/.claude.json`)

## Install (planned)

```bash
# Linux / macOS
python3 install.py

# Windows (PowerShell)
python install.py
```

The installer:
1. Reads `~/.claude.json`
2. Detects qdrant + memory_kg MCP servers (asks if ambiguous)
3. Verifies them with a real MCP call
4. Writes `config/claude-hooks.json`
5. Merges hook entries into `~/.claude/settings.json`
