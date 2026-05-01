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

mnemex is used on-demand via the code-analysis skills
(`/code-analysis--search`, `/code-analysis--deep-analysis`, etc.) if installed.

> **Important:** The `code-analysis@mag-claude-plugins` plugin injects
> claudemem context on every `PreToolUse` event (Grep, Glob, Bash, Read),
> which rapidly consumes context and causes premature compaction. We
> recommend extracting the plugin's skills as standalone and disabling
> the plugin's hooks. Run `python3 extract_plugin.py` from the claude-hooks
> repo to do this automatically. See the README for details.

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

## 6. axon -- MCP-native Code Knowledge Graph (RECOMMENDED)

**Importance: HIGH** -- Pure-Python code-intelligence engine that
indexes Python/TS/JS into KuzuDB and exposes a focused MCP tool surface
(`query`, `context`, `impact`, `dead_code`, `cypher`, `detect_changes`,
`list_repos`). Best fit for the typical claude-hooks user (Python-heavy
codebase, conda env already in place). Where claude-hooks's built-in
`code_graph` covers the zero-config baseline, axon is the upgrade path
for richer live queries from inside Claude Code.

**What it brings (beyond `code_graph`):**
- 7 MCP tools (`mcp__axon__query`, `mcp__axon__impact`, ...) callable
  from any context, not just Grep
- Live file watcher mode -- index updates as you edit
- Hybrid search (BM25 + 384-d vector embeddings + fuzzy)
- Leiden community detection with cohesion scoring
- **Dead-code detection** (`axon_dead_code`) -- a feature gap our
  built-in `code_graph` doesn't cover
- Cypher graph queries against the embedded KuzuDB
- Branch-diff structural comparison (`axon diff`)

**Install (user-driven):**
```bash
pip install axoniq         # into your claude-hooks conda env
axon analyze .             # one-time index build per repo (only repos
                           # you actually want indexed - see warning
                           # below about the legacy MCP form)
axon setup --claude        # prints the MCP server JSON
```

Two MCP wiring options. **Pick the shared-host form on any machine
with more than one Claude Code project**:

```jsonc
// ~/.claude.json mcpServers - RECOMMENDED on multi-project hosts.
// One singleton daemon serves every registered repo over HTTP.
{
  "axon": {
    "type": "http",
    "url": "http://127.0.0.1:8420/mcp"
  }
}
```

```jsonc
// ~/.claude.json mcpServers - LEGACY single-session stdio.
// DANGEROUS on multi-project hosts. See "Failure mode" below.
{
  "axon": {
    "type": "stdio",
    "command": "axon",
    "args": ["serve", "--watch"]
  }
}
```

The shared-host form needs a daemon listening on `127.0.0.1:8420`.
claude-hooks ships a systemd unit for that (`systemd/axon-host.service`)
which install.py will offer to install when
`companions.axon_host.enabled` is true in `config/claude-hooks.json`.
Defaults to `false` because not every host runs systemd.

**Failure mode the shared host avoids.** The legacy
`axon serve --watch` MCP form starts a fresh daemon **per Claude Code
session**, in whatever directory Claude Code was launched from. Each
daemon then auto-bootstraps an axon index of that cwd. We hit this on
2026-04-27 with a 64 GB resident-set blow-up on a session that opened
inside a directory of `.gguf` / `.safetensors` model blobs. Eleven
parallel daemons stacked up over a day, each binding to port 8421 and
trying to watch a different cwd. The shared-host form replaces all of
that with one daemon serving a curated registry under `~/.axon/repos/`.

**`.axonignore` convention.** Drop a `.axonignore` file (gitignore-
style) in any directory you want axon to leave alone. claude-hooks
treats this as a documentary "do not analyze" marker; it's also the
right place to keep `*` if the directory might ever land under a
runaway watcher (model dirs, dataset roots, `/tmp`, etc.).

**claude-hooks integration (automatic when detected):**
- SessionStart inject appends a one-line hint pointing at the
  `mcp__axon__*` tools when `.axon/` is present in the repo
- Stop hook spawns `axon analyze .` detached when the turn modified
  source files (belt-and-braces -- axon's `host --watch` already
  re-indexes live; this catches sessions where the host was started
  with `--no-watch`, including the systemd unit's default config)
- `python -m claude_hooks.code_graph companions` shows detection state
- Silent no-op when axon isn't installed; `code_graph` still runs

Toggles live under `hooks.companions` in `config/claude-hooks.json`
(default `enabled: true` so detection just works). The optional
shared-host systemd unit lives under `companions.axon_host.enabled`
(default `false`).

---

## 7. gitnexus -- Multi-language Code Knowledge Graph (ALTERNATIVE)

**Importance: MEDIUM** -- The 14-language alternative to axon. Indexes
TS/JS/Python/Java/Kotlin/C#/Go/Rust/PHP/Ruby/Swift/C/C++/Dart with
cross-file resolution (constructor inference, heritage tracking, type
annotations) into LadybugDB and exposes 16 MCP tools including
multi-repo `group_*` queries.

**Pick gitnexus over axon when:**
- You write languages outside Python/JS/TS (Java, Kotlin, Rust,
  Swift, etc.)
- You need cross-repo queries via `group_list`/`group_query`/etc.
- You want Mermaid diagrams via the `generate_map` MCP prompt
  (claude-hooks ships its own `code_graph mermaid` CLI as a
  zero-dep alternative)

**Pick axon over gitnexus when:**
- Python/JS/TS only
- You want the lightest install footprint (no Node.js + native
  bindings, just `pip install`)
- Dead-code detection is on your wishlist
- You already have a conda env you'd rather not pollute with `npm`

**Install (user-driven):**
```bash
npm i -g gitnexus           # or: npx gitnexus init
gitnexus analyze .          # in your repo
gitnexus setup --claude     # prints MCP server JSON to add to ~/.claude.json
```

**Old-glibc workaround**: gitnexus 1.6+ ships LadybugDB binaries built
against glibc 2.32 / GLIBCXX 3.4.32. Hosts with older runtimes (e.g.
Debian 11 / Proxmox VE 7) will see `Error: ... version 'GLIBC_2.32'
not found` on every gitnexus call. claude-hooks ships a Docker wrapper
at [`docker/gitnexus/`](docker/gitnexus/) that runs gitnexus inside a
Debian-trixie container and persists the registry to
`/shared/config/gitnexus/`. After install, `gitnexus` becomes a
transparent drop-in — `which gitnexus` still resolves and the
companion_integration detects it normally. See `docker/gitnexus/README.md`
for setup.

**gitnexus 1.6.3 C# notes**: tree-sitter scope extraction can fail
(`"Invalid argument"`) on individual large C# files (30-100 KB range
with complex generics / async patterns). **Non-fatal** — the file is
dropped from the call graph but the index still builds.

Two harder failures that previously abandoned the entire C# index for
affected files have been fixed upstream and merged to `main`:

- `TypeError: Cannot add property N, object is not extensible` in
  `populateCsharpNamespaceSiblings` —
  [#1082](https://github.com/abhigyanpatwari/GitNexus/pull/1082)
  +
  [#1085](https://github.com/abhigyanpatwari/GitNexus/pull/1085)
  (companion fixture).
- `Namespace has kind 'Namespace' but no parent. Only 'Module' scopes
  may be root-level` for files where `compilation_unit` and the
  top-level `namespace_declaration` share an identical tree-sitter
  range (no leading content, no trailing newline — common shape for
  WinForms `.Designer.cs`) —
  [#1087](https://github.com/abhigyanpatwari/GitNexus/pull/1087).

Both fixes are in `main` as of 2026-04-27 but not yet in any npm
release (latest stable is still 1.6.3). Install from upstream main
until the next release ships:

```bash
git clone https://github.com/abhigyanpatwari/GitNexus
cd GitNexus/gitnexus-shared && npm ci && npm run build
cd ../gitnexus && npm ci && npm pack
npm i -g ./gitnexus-1.6.3.tgz   # version label unchanged until upstream tags 1.6.4
```

Once upstream publishes 1.6.4 (or rc.9+) on npm, drop this recipe
and use `npm i -g gitnexus@latest`.

**claude-hooks integration (automatic when detected):**
- SessionStart inject appends a hint pointing at the `mcp__gitnexus__*`
  tools when `.gitnexus/` is present
- Stop hook spawns `gitnexus analyze` detached on file edits
- Both axon and gitnexus can coexist in the same repo; both detection
  paths fire and both reindex hooks trigger when their respective
  marker dir is present

---

## 8. cclsp -- Multi-Language LSP via MCP (RECOMMENDED)

**Importance: HIGH** -- Pairs with the built-in `PostToolUse` ruff hook
to give Claude Code real IDE-grade context: hover, go-to-definition,
find-references, and type diagnostics across Python / Go / Rust / C/C++ /
C#. Where ruff is the always-on synchronous layer (Python only), cclsp
is the multi-language on-demand layer the model can call between edits.

**Why cclsp over one-MCP-per-language:**
- One MCP entry in `~/.claude.json` fronts every configured LSP child.
- Lazy spawn — language servers only start when a file in their extension
  list is touched. A Python-only session never pays the rust-analyzer
  cold-start.
- One config file (`cclsp.json`) to keep in sync across machines.

**Quick install (Linux):**
```bash
npm i -g pyright cclsp
go install golang.org/x/tools/gopls@latest
rustup component add rust-analyzer
sudo apt install -y clangd
# OmniSharp via upstream zip — see docs/lsp-mcp.md for details
```

**Config:** see [`docs/lsp-mcp.md`](docs/lsp-mcp.md) for the full
Linux + Windows recipe, the `cclsp.json` template, the C# / OmniSharp
binary install (csharp-ls upstream is broken), and the
`mcpServers.lsp` block to drop into `~/.claude.json`.

**Relationship to the LSP engine:** the in-tree
[`claude_hooks.lsp_engine`](docs/lsp-engine.md) is a session-scoped
daemon that loads language servers once per project, follows edits
in real time, and answers diagnostics in single-digit ms. It pairs
with cclsp rather than replacing it: cclsp covers on-demand MCP
calls (hover, definition, references) for lightweight use; the
engine covers persistent multi-session edit tracking with
per-file affinity locks, adaptive code-graph preload, git
branch-switch refresh, and opt-in `cargo check` / `tsc` /
`mypy` compile-aware diagnostics. POSIX-ready (Phases 0-3 shipped);
Windows parity is Phase 4. Use the `/setup-compile-aware` skill for
a guided proposal of the per-language compile commands.

---

## Summary

| Tool | Importance | Slash commands | Terminal commands |
|------|-----------|---------------|-------------------|
| **mnemex** | HIGH | `/code-analysis--claudemem-search` | `mnemex search/index/map/symbol/callers` |
| **episodic-memory** | HIGH | `/episodic <query>` | `episodic-memory search/sync/show/stats` |
| **axon** | HIGH | (via `mcp__axon__*` tools) | `axon analyze/serve/dead-code/cypher` |
| **caliber** | MEDIUM | `/setup-caliber`, `/find-skills` | `caliber score/hooks/learn/refresh/skills` |
| **claudekit** | MEDIUM | `/checkpoint:create/restore/list` | `claudekit-hooks profile` |
| **gitnexus** | MEDIUM | (via `mcp__gitnexus__*` tools) | `gitnexus init/analyze/mcp` |
| **claude-code-organizer** | LOW | `/cco` | `npx @mcpware/claude-code-organizer` |
| **code-analysis** | HIGH (extract!) | `/code-analysis--*` | `python3 extract_plugin.py` |
| **cclsp** | HIGH | (via `mcp__lsp__*` tools) | `cclsp` (multi-LSP MCP wrapper) |

All tools are optional. claude-hooks works fully without any of them.

---

## Claude Code Plugins

These are installed via the Claude Code marketplace system, not npm.

### code-analysis (MadAppGang)

**Importance: HIGH (skills) / HARMFUL (hooks)** -- The plugin provides
excellent investigation skills (deep-analysis, detective agents, claudemem
search). However, its hooks are problematic:

**Problems with the plugin's hooks:**

1. **Context bloat** -- `PreToolUse` hooks on `Grep|Bash|Glob|Read|Task`
   inject claudemem `additionalContext` on every single tool call. In a
   session with 50+ tool calls, this adds hundreds of KB of accumulated
   context, causing premature compaction even on the 1M context window.

2. **Windows cmd.exe flash** -- `PostToolUse` hooks on `Write|Edit` spawn
   `claudemem index` via the `.cmd` wrapper, which opens a visible console
   window on every file edit. The `windowsHide: true` flag has no effect on
   `.cmd`/`.bat` files. ([MadAppGang/claude-code#14](https://github.com/MadAppGang/claude-code/issues/14))

3. **No per-hook control** -- Claude Code's plugin system is all-or-nothing:
   `enabledPlugins` only supports `true`/`false`, with no way to selectively
   disable specific hooks while keeping skills.

**Recommended setup:** Extract skills as standalone, disable the plugin:

```bash
# From the claude-hooks repo:
python3 extract_plugin.py
```

This copies all 13 skills, agents, and commands to `~/.claude/skills/`,
`~/.claude/agents/`, and `~/.claude/commands/` as standalone files, then
sets `"code-analysis@mag-claude-plugins": false` in settings.json. Skills
are available as `/code-analysis--deep-analysis`, etc. Re-run after plugin
updates to pick up new skills.

**Requires:** mnemex (for semantic search backend)

**Skills provided (after extraction):**

| Skill | Description |
|-------|-------------|
| `/code-analysis--deep-analysis` | Primary: how does X work, trace flow, find implementations |
| `/code-analysis--ultrathink-detective` | Comprehensive multi-perspective audit (uses Opus) |
| `/code-analysis--developer-detective` | Implementation tracing, data flow, symbol usage |
| `/code-analysis--architect-detective` | Architecture, design patterns, system structure |
| `/code-analysis--debugger-detective` | Root cause analysis, bug tracing, error investigation |
| `/code-analysis--tester-detective` | Test coverage, missing tests, edge cases |
| `/code-analysis--investigate` | Auto-routes to the right detective based on query |
| `/code-analysis--claudemem-search` | Semantic code search via claudemem |

### ccusage — USD cost cross-reference

**Importance: LOW / useful** — Third-party CLI that walks the same
transcripts `scripts/weekly_token_usage.py` does and reports USD cost
per day, per model, per session. Useful for sanity-checking our numbers
and for seeing `$` figures (Anthropic subscription tokens are opaque
but the equivalent API price is knowable). Doesn't honour our
Fri-10:00-CEST weekly-reset window — ccusage groups by calendar day
only — so the two tools are complementary.

```bash
npx -y ccusage@latest daily -z Europe/Berlin --since 20260410 --breakdown
npx -y ccusage@latest weekly
npx -y ccusage@latest blocks --active
```

Repo: https://github.com/ryoppippi/ccusage. claude-hooks'
`weekly_token_usage.py` dedups transcript replays using the same
composite key ccusage uses (`message.id + model + requestId`) so the
two agree within the timezone-window delta.

---

### frontend-design (official)

**Importance: MEDIUM** -- Generates distinctive, production-grade frontend
interfaces. Available from the official Claude plugins marketplace. No
problematic hooks.

**Setup (inside Claude Code):**

The official marketplace is registered by default. Just enable:
```json
{
  "enabledPlugins": {
    "frontend-design@claude-plugins-official": true
  }
}
```

| Skill | Description |
|-------|-------------|
| `/frontend-design:frontend-design` | Create polished web components, pages, and apps |
