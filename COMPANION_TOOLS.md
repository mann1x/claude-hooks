# Recommended Companion Tools

These tools are installed separately and complement claude-hooks. Each one
fills a different gap in the AI coding workflow. Ranked by how much value
they add on top of claude-hooks.

---

## 1. mnemex / claudemem -- Semantic Code Search

**Importance: HIGH** -- Without this, Claude Code relies on Grep/Glob for code
search, which misses semantic matches. mnemex indexes your codebase with
AST-aware chunking and embedding-based search, so queries like "how does
authentication work" return relevant code even if the word "authentication"
doesn't appear.

**What it brings:**
- Semantic search across your entire codebase
- AST-aware chunking (understands function boundaries, classes, modules)
- PageRank-based ranking of code symbols
- Structural navigation: callers, callees, symbol maps

**Install:**
```bash
npm install -g mnemex
mnemex setup                    # interactive: pick Ollama + snowflake-arctic-embed2
mnemex index .                  # index a project
mnemex search "how does X work" # semantic search
```

**Known bug** ([MadAppGang/mnemex#4](https://github.com/MadAppGang/mnemex/issues/4)):
add `"openrouterApiKey": "dummy"` to `~/.claudemem/config.json` -- the
tool checks for this key before reading the embedding provider config.

---

## 2. episodic-memory -- Transcript Search

**Importance: HIGH** -- Claude Code sessions are ephemeral. Once a
conversation ends, the context is gone. episodic-memory indexes all your
past Claude Code transcripts and makes them searchable. When you think
"I fixed this before, what did I do?", this is what answers that question.

**What it brings:**
- Full-text + embedding search across all past Claude Code conversations
- Date-aware results (shows when each conversation happened)
- Works across all projects, not just the current one

**Install:**
```bash
# Build from source (requires Node 22+)
git clone https://github.com/obra/episodic-memory
cd episodic-memory && npm install && npm link

episodic-memory sync            # index past conversations
episodic-memory search "query"  # search across all sessions
```

---

## 3. caliber -- Config Quality & Drift Detection

**Importance: MEDIUM** -- Keeps your CLAUDE.md, Cursor rules, and Copilot
instructions in sync. Scores your AI agent config quality and flags when
things drift. Most useful if you work across multiple AI coding tools or
want to maintain config hygiene.

**What it brings:**
- Config quality scoring (aim for 85+)
- Pre-commit hook to auto-sync agent configs on every commit
- Session learning: observes tool usage and extracts patterns
- AGENTS.md generation for cross-agent compatibility

**Install:**
```bash
npm install -g @rely-ai/caliber
caliber hooks --install         # pre-commit hook for auto-sync
caliber score                   # check config quality
caliber learn install           # enable session learning
```

---

## 4. claudekit -- Git Checkpoints & Hook Profiling

**Importance: MEDIUM** -- Adds git checkpoint/restore commands to Claude Code
sessions (useful for risky refactors) and can profile your hook performance
to find slow hooks.

**What it brings:**
- `/checkpoint:create` and `/checkpoint:restore` slash commands
- Hook performance profiling (`claudekit-hooks profile`)
- Lightweight, no config needed

**Install:**
```bash
npm install -g claudekit
```

---

## 5. claude-code-organizer -- Security Scanner & Dashboard

**Importance: LOW** -- A web dashboard that scans your MCP server configs for
security issues and shows token budget usage. Nice to have for auditing
your setup, not essential for daily work.

**What it brings:**
- Web dashboard at `http://localhost:3847`
- MCP server security scanning
- Token budget visualization
- Memory and skills management UI

**Install:**
```bash
npx @mcpware/claude-code-organizer   # launches dashboard
```

---

## Summary

| Tool | Importance | What it fills |
|------|-----------|--------------|
| **mnemex** | HIGH | Semantic code search (Claude can't do this natively) |
| **episodic-memory** | HIGH | Search past conversations (otherwise lost forever) |
| **caliber** | MEDIUM | Config quality + drift detection across AI tools |
| **claudekit** | MEDIUM | Git safety net + hook performance |
| **claude-code-organizer** | LOW | Security audit dashboard |

All tools are optional. claude-hooks works fully without any of them.
