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
```

**Known bug** ([MadAppGang/mnemex#4](https://github.com/MadAppGang/mnemex/issues/4)):
add `"openrouterApiKey": "dummy"` to `~/.claudemem/config.json` -- the
tool checks for this key before reading the embedding provider config.

**Commands -- terminal:**
```bash
mnemex setup                    # configure embedding provider
mnemex index .                  # index current project
mnemex index . --force          # re-index from scratch
mnemex search "how does X work" # semantic search
mnemex status                   # show index stats (files, chunks)
mnemex map                      # show AST structure map
mnemex symbol <name>            # find symbol definition
mnemex callers <name>           # who calls this symbol
mnemex callees <name>           # what does this symbol call
mnemex context <name>           # full context around a symbol
```

**Commands -- inside Claude Code:**

mnemex is used automatically by the `code-analysis` plugin skills
(`/code-analysis:search`, `/code-analysis:deep-analysis`, etc.) if installed.
No manual slash command needed.

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
- Remote sync: client hosts push transcripts to a central server

**Install:**
```bash
# Build from source (requires Node 22+)
git clone https://github.com/obra/episodic-memory
cd episodic-memory && npm install && npm link
```

**Commands -- terminal:**
```bash
episodic-memory sync                    # index new conversations
episodic-memory search "bcache fix"     # semantic search
episodic-memory search "nginx proxy"    # search across all sessions
episodic-memory show path/to/conv.jsonl # display a conversation
episodic-memory show --format html conv.jsonl > out.html  # export
episodic-memory stats                   # index statistics
episodic-memory index --cleanup         # rebuild index
```

**Commands -- inside Claude Code:**
```
/episodic bcache fix          # search via the episodic skill (uses HTTP API)
/episodic nginx proxy config  # works from any host if episodic-server is running
```

**Remote setup (via claude-hooks installer):**
```bash
# On the server (has episodic-memory installed):
python3 install.py --episodic-server

# On client machines (transcripts pushed on session end):
python3 install.py --episodic-client http://SERVER:11435
```

**HTTP API (episodic-server):**
```bash
curl "http://SERVER:11435/search?q=query&limit=10"  # search
curl http://SERVER:11435/health                      # health check
curl http://SERVER:11435/stats                       # index stats
curl -X POST http://SERVER:11435/sync                # trigger re-index
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
```

**Commands -- terminal:**
```bash
caliber score                   # check config quality (aim for 85+)
caliber score --json            # machine-readable output
caliber hooks --install         # install pre-commit hook for auto-sync
caliber refresh                 # manually sync agent configs
caliber learn install           # enable session learning hooks
caliber learn finalize --auto   # finalize session learnings
caliber skills --query "react"  # search community skill registry
caliber init --agent claude     # generate AGENTS.md
```

**Commands -- inside Claude Code:**
```
/setup-caliber                  # interactive setup (checks what's missing)
/find-skills                    # search community skill registry
```

---

## 4. claudekit -- Git Checkpoints & Hook Profiling

**Importance: MEDIUM** -- Adds git checkpoint/restore commands to Claude Code
sessions (useful for risky refactors) and can profile your hook performance
to find slow hooks.

**What it brings:**
- Git checkpoint/restore for safe rollback during AI sessions
- Hook performance profiling
- Lightweight, no config needed

**Install:**
```bash
npm install -g claudekit
```

**Commands -- terminal:**
```bash
claudekit --version             # verify installation
claudekit-hooks profile         # profile hook performance (latency per hook)
```

**Commands -- inside Claude Code:**
```
/checkpoint:create              # save a git checkpoint (stash-like snapshot)
/checkpoint:restore             # restore to last checkpoint
/checkpoint:list                # list available checkpoints
```

---

## 5. claude-code-organizer -- Security Scanner & Dashboard

**Importance: LOW** -- A web dashboard that scans your MCP server configs for
security issues and shows token budget usage. Nice to have for auditing
your setup, not essential for daily work.

**What it brings:**
- Web dashboard for managing Claude Code configuration
- MCP server security scanning
- Token budget visualization
- Memory and skills management UI

**Install and run:**
```bash
npx @mcpware/claude-code-organizer   # launches dashboard at http://localhost:3847
```

**Commands -- inside Claude Code:**
```
/cco                            # open the dashboard (if alias configured)
```

**Dashboard endpoints:**
```
http://localhost:3847            # main dashboard
http://localhost:3847/security   # MCP security scan
http://localhost:3847/tokens     # token budget view
```

---

## Summary

| Tool | Importance | Slash commands | Terminal commands |
|------|-----------|---------------|-------------------|
| **mnemex** | HIGH | (via code-analysis plugin) | `mnemex search/index/map/symbol/callers` |
| **episodic-memory** | HIGH | `/episodic <query>` | `episodic-memory search/sync/show/stats` |
| **caliber** | MEDIUM | `/setup-caliber`, `/find-skills` | `caliber score/hooks/learn/refresh/skills` |
| **claudekit** | MEDIUM | `/checkpoint:create/restore/list` | `claudekit-hooks profile` |
| **claude-code-organizer** | LOW | `/cco` | `npx @mcpware/claude-code-organizer` |

All tools are optional. claude-hooks works fully without any of them.
