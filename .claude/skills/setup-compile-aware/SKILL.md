---
name: setup-compile-aware
description: Configures the claude-hooks LSP engine for the current project. Phase 0 audits cclsp.json against the project's actual language files and proposes additive LSP entries for languages that aren't covered yet. Phase 1 proposes [compile_aware.commands] for .claude-hooks/lsp-engine.toml by detecting build tools (Cargo.toml, tsconfig.json, pyproject.toml, go.mod, Makefile, etc). Use when the user wants Claude to set up or extend LSP coverage for the project, or asks how to wire pyright / gopls / rust-analyzer / clangd / cargo check / tsc --noEmit / mypy into the LSP engine.
---

# Set up the LSP engine for this project

This skill helps the user configure the two files the LSP engine
reads from a project:

- **`cclsp.json`** — registry of LSP servers + which file extensions
  spawn each one. Owned by `cclsp` format conventions; the claude-
  hooks LSP engine reads it for the `did_open` / `did_change` /
  `diagnostics` round-trip.
- **`.claude-hooks/lsp-engine.toml`** `[compile_aware]` — opt-in
  background build-tool diagnostics (`cargo check`, `tsc --noEmit`,
  `mypy`). Off by default.

The skill is **two-phase**:

| Phase | File | What it does |
|---|---|---|
| **Phase 0** | `cclsp.json` | Walks the project's actual language files, compares against existing `cclsp.json`, proposes **additive-only** LSP entries for languages not yet covered. Never proposes removing entries. |
| **Phase 1** | `.claude-hooks/lsp-engine.toml` | Walks build-tool markers, proposes `[compile_aware.commands]` per detected language. Disabled by default; user flips after smoke-testing. |

Both phases require **explicit user confirmation** before any file
write. The user controls what runs — your job is to detect, propose,
explain trade-offs, and wait.

**Background**: see `docs/lsp-engine.md` for the engine architecture
and `docs/PLAN-lsp-engine.md` for the design rationale.

## Constraints (apply to both phases)

- **Never write a file without explicit user confirmation.** The
  user must say "yes" / "go" / "do it" / similar before any edit.
- **Additive only on `cclsp.json`.** Never propose removing or
  modifying an existing entry. The file may contain custom flags
  (`--strict`, `--remote=auto`), manual entries for niche LSes, or
  deliberate user choices you can't infer. Adding new entries to
  cover languages the user didn't have before is safe; touching
  existing ones is not.
- **No auto-detection magic on compile commands.** Surface what you
  found and let the user pick. If you guess flags they don't want
  (`--strict`, `--release`), they'll silently spend minutes on the
  wrong workload every save.
- **Add `# why:` comments** above each `[compile_aware.commands]`
  entry. TOML supports comments — use them. The PostToolUse advisor
  will nag if you forget. (JSON has no comments, so `cclsp.json`
  doesn't get them — the skill instead explains its choices in
  chat before writing.)
- **Warn on missing binaries.** Before proposing a `cclsp.json`
  addition or a compile command, check that the binary is on
  `$PATH` (`command -v <bin>` via Bash). If missing, surface the
  install command from `claude_hooks/lang_servers.py` rather than
  silently registering a server that will never spawn.

---

## Phase 0 — cclsp.json audit

### Step 0.1: Read the existing `cclsp.json`

```bash
cat cclsp.json 2>/dev/null
```

- **Missing** → you'll create the file from scratch (still
  additive: there's nothing to preserve).
- **Present** → parse the JSON; you'll preserve every existing
  entry verbatim and append new ones.

Capture the existing extension coverage:

```python
covered_extensions = set()
for server in existing["servers"]:
    covered_extensions.update(server["extensions"])
```

### Step 0.2: Walk the project for language files

Use `Glob` to count files per language at the top level (skip
`node_modules/`, `.venv/`, `venv/`, `dist/`, `build/`, `target/`,
`__pycache__/`, `.git/`, vendored / generated dirs).

Build a map of `{extension: file_count}` for the project. Common
extensions to look for:

| Extension(s) | Language | Typical LSP |
|---|---|---|
| `.py`, `.pyi` | Python | `pyright-langserver --stdio` |
| `.ts`, `.tsx`, `.js`, `.jsx`, `.mts`, `.cts` | TypeScript/JavaScript | `typescript-language-server --stdio` |
| `.rs` | Rust | `rust-analyzer` |
| `.go` | Go | `gopls` |
| `.c`, `.cc`, `.cpp`, `.cxx`, `.h`, `.hh`, `.hpp` | C/C++ | `clangd` |
| `.cs` | C# | `omnisharp -lsp` |
| `.sh`, `.bash` | Shell | `bash-language-server start` |
| `.lua` | Lua | `lua-language-server` |
| `.zig` | Zig | `zls` |
| `.java` | Java | `jdtls` (manual install) |
| `.rb` | Ruby | `solargraph stdio` (manual install) |
| `.php` | PHP | `intelephense --stdio` (manual install) |

Use **file count, not just presence** — a single stray `.py` file in
a Rust project doesn't warrant wiring up pyright. A reasonable
threshold is 3+ files OR a build marker that implies the language
(`Cargo.toml` → Rust regardless of file count). State your
threshold to the user up front.

### Step 0.3: Compute the gap

Three useful sets:

1. **Missing** — languages with significant file counts in the
   project, but their extensions are NOT in `covered_extensions`.
   These are the additive candidates.
2. **Covered** — languages already wired up. Print these as a
   sanity check, but don't propose changes to them (additive-only
   rule).
3. **Stranger entries** — extensions in `cclsp.json` that have
   ZERO files in the project. Surface them as **informational
   only**: "you have `clangd` registered for `.c/.cc/.cpp/.h` but I
   found no matching files — you may have set this up for future
   work or it may be vestigial. I won't touch it." Never propose
   removal, but the user should know.

### Step 0.4: Resolve LSP commands

For each language in the **Missing** set, pick the canonical LS
command from `claude_hooks/lang_servers.py:SPECS` (Tier 1) or the
table above (Tier 2). Format:

```python
{
  "extensions": ["py", "pyi"],
  "command": ["pyright-langserver", "--stdio"]
}
```

Extensions are **lowercase, no leading dot** (engine convention,
see `claude_hooks/lsp_engine/config.py:158`).

### Step 0.5: Probe the binary

For each proposed entry, run `command -v <bin>` via Bash to check
the LSP binary is on `$PATH`. Three cases:

- **Found** → mark `[ok]`, proceed.
- **Missing, Tier 1** → mark `[!! install needed]` and surface the
  install hint from `claude_hooks/lang_servers.py:SPECS` (e.g.
  `npm install -g pyright`, `go install
  golang.org/x/tools/gopls@latest`). Ask whether to skip this
  entry or proceed (the engine will try to spawn and fail
  gracefully if missing — registering a missing binary isn't
  destructive, just slightly noisy).
- **Missing, Tier 2 / manual** → same as above but the install
  hint is "see `docs/lsp-engine.md`."

### Step 0.6: Propose and confirm

Show the user the proposed `cclsp.json` after merge (existing
entries + new additions, no removals), highlighting the new
entries:

```
Phase 0: cclsp.json — additive audit
Existing entries (preserved verbatim):
  ✓ py, pyi   → pyright-langserver --stdio
  ✓ go        → gopls

Project files found:
  47 .py files  ✓ covered
   3 .ts files  ✗ NOT covered — proposing typescript-language-server
   0 .rs files  ✓ irrelevant
   8 .c/.h files ✗ NOT covered — proposing clangd

Strangers (registered but no matching files in project):
  rs → rust-analyzer  (left untouched — may be intentional)

Proposed additions:
  + ts, tsx, js, jsx, mts, cts → typescript-language-server --stdio
     [!! install needed: npm install -g typescript-language-server typescript]
  + c, cc, cpp, cxx, h, hh, hpp → clangd
     [ok] /usr/bin/clangd present

Write the merged cclsp.json (4 entries total, 2 new)? [Y/n/edit]
```

Three options:
- **Y / yes / go** → write the merged JSON. Preserve indentation
  and order of existing entries; append new ones.
- **n / no / skip** → don't write Phase 0. Print the proposed JSON
  for manual editing. Proceed to Phase 1.
- **edit / drop X** → user names entries to drop from the
  proposal. Re-show, re-confirm.

### Step 0.7: Write the merged file

Only after explicit yes:

1. Backup the existing file (timestamped: `cclsp.json.bak-YYYYMMDD-HHMMSS`).
2. Merge: existing entries first (preserved verbatim), then new
   additions in the order proposed.
3. Write with `indent=2` to match the installer's starter format.
4. Verify by reading back and showing a diff.
5. Tell the user:
   - The LSP engine reads `cclsp.json` on session start; restart
     the Claude Code session (or re-run the `connect_or_spawn`
     daemon) to pick up the changes.
   - To roll back: delete the new entries or restore from the
     backup.

### Step 0.8: Skip-with-grace path

If Phase 0 produces zero additions (every language with files in
the project is already covered), just say so and move on:

```
Phase 0: cclsp.json — already covers all project languages
(.py → pyright, .go → gopls). No changes proposed.
```

Don't write a no-op.

---

## Phase 1 — compile_aware commands

### Step 1.1: Detect build-tool markers

Walk the project root for these files:

| Marker file | Implies | Default suggestion |
|---|---|---|
| `Cargo.toml` | Rust | `["cargo", "check", "--message-format=json"]` |
| `tsconfig.json` | TypeScript | `["tsc", "--noEmit"]` |
| `package.json` with `typescript` in deps | TypeScript (no tsconfig) | `["npx", "tsc", "--noEmit"]` |
| `pyproject.toml` with `mypy` in deps | Python (mypy) | `["mypy", "--strict", "<package>"]` (replace `<package>` with the actual src dir) |
| `pyproject.toml` with `pyright` in deps | Python (pyright CLI) | usually skip — the LSP already runs pyright per-file |
| `setup.cfg` or `mypy.ini` | Python (mypy) | `["mypy", "."]` |
| `go.mod` | Go | `["go", "vet", "./..."]` |
| `CMakeLists.txt` | C/C++ | suggest a manual choice — too varied |
| `Makefile` with a `check` / `lint` / `test` target | varies | `["make", "check"]` (after confirming the target exists) |
| `pubspec.yaml` | Dart/Flutter | `["dart", "analyze"]` |
| `mix.exs` | Elixir | `["mix", "compile", "--warnings-as-errors"]` |

For ambiguous cases (multiple Python type-checkers, monorepo with
several languages, custom build wrappers), **ask the user** before
proposing.

### Step 1.2: Read the existing lsp-engine.toml if any

```bash
cat .claude-hooks/lsp-engine.toml 2>/dev/null
```

- Empty / missing → you'll create the file from scratch.
- Has `[compile_aware]` already → preserve any non-`commands`
  knobs (debounce, etc); only modify the `commands` table and the
  `enabled` flag with the user's blessing.
- Has unrelated sections (`[preload]`, `[session_locks]`, etc) →
  preserve them verbatim.

### Step 1.3: Propose the block

Show the user the proposed `[compile_aware.commands]` block and
the `enabled` flag. Format:

```toml
[compile_aware]
# Off by default — flip after you're happy with the commands below.
enabled = false

[compile_aware.commands]
# why: cargo check is the canonical Rust build-time check. JSON output
# parses into structured diagnostics with E-codes (E0308 etc).
rs = ["cargo", "check", "--message-format=json"]

# why: project has tsconfig.json — tsc --noEmit is the standard
# whole-project type-check. Text output, ~1-3s on this codebase.
ts = ["tsc", "--noEmit"]
```

For each entry, the comment should explain **why this command and
not another** — observed evidence from the project ("project has
tsconfig.json", "Cargo.toml workspace with 4 crates", "mypy in
pyproject.toml dev-dependencies"). Future sessions read these
comments to understand the choice.

### Step 1.4: Surface trade-offs

Before asking for confirmation, name 2-3 things the user should
know:

- **Run cost.** Estimate how long each command takes — if `cargo
  check` on this repo takes 30 seconds, that's a debounced 30 s
  every time edits go quiet. Suggest extending `debounce_seconds`
  if the run is slow.
- **CI parity.** If the user's CI runs different flags
  (`--locked`, `--strict`, custom config), point that out — the
  engine's compile-aware should match what CI checks, otherwise
  diagnostics drift between local and CI.
- **`enabled = false` for now.** Recommend leaving the flag off
  until the user has tested one command manually. Easier to debug
  a single command than a daemon of them.

### Step 1.5: Ask for confirmation

Use AskUserQuestion (or just plain "want me to write this?") to
collect the explicit yes. Options:

- **Write it as-is** → write the TOML, confirm where, advise on
  flipping `enabled = true` once they've smoke-tested.
- **Adjust commands** → which ones to drop / change.
- **Don't write** → leave them with the proposed block printed
  for manual editing.

### Step 1.6: Write the file

Only after explicit yes:

1. Create `.claude-hooks/` directory if needed.
2. Read any existing `lsp-engine.toml` and preserve unrelated
   sections.
3. Write the file with the proposed block + comments.
4. **Verify** by reading it back and showing the diff (`diff
   /tmp/old.toml .claude-hooks/lsp-engine.toml`).
5. Tell the user the next steps:
   - To smoke-test: `python -m claude_hooks.lsp_engine status
     --project .` (daemon should be running before they test).
   - To enable: edit `enabled = false` → `enabled = true`.
   - To rollback: delete the file or comment out the block.

### Step 1.7: Don't enable automatically

Even if the user says "set it up", default to `enabled = false`
unless they specifically say "and turn it on". Lots of users want
to inspect the proposed commands before paying the per-edit cost.

---

## Examples

### Example: Phase 0 init on a fresh project

User: `/setup-compile-aware` on a TypeScript/Python repo with no
`cclsp.json` yet.

You:
1. No `cclsp.json` found — Phase 0 is init, not audit.
2. Walk the project:
   - 87 `.ts` / `.tsx` files
   - 42 `.py` files
   - 3 stray `.js` files (probably config — flag)
3. Propose:
   ```json
   {
     "servers": [
       {
         "extensions": ["py", "pyi"],
         "command": ["pyright-langserver", "--stdio"]
       },
       {
         "extensions": ["ts", "tsx", "js", "jsx", "mts", "cts"],
         "command": ["typescript-language-server", "--stdio"]
       }
     ]
   }
   ```
4. Probe binaries:
   - `pyright-langserver` → `[ok]`
   - `typescript-language-server` → `[!! install needed: npm install -g typescript-language-server typescript]`
5. Note: "Registering typescript-language-server now is safe even
   without the binary — the engine returns `[]` diagnostics for
   `.ts` files until you `npm install -g`. Want me to register it
   anyway or wait?"
6. Confirm + write.
7. Move to Phase 1.

### Example: Phase 0 additive audit on a project that grew

User: `/setup-compile-aware` on a project that started as Rust-only
and added a `web/` directory with React + TypeScript.

Existing `cclsp.json`:
```json
{"servers": [{"extensions": ["rs"], "command": ["rust-analyzer"]}]}
```

You:
1. Walk the project: 124 `.rs` files (covered), 56 `.ts/.tsx`
   files (NOT covered).
2. Phase 0 audit:
   ```
   Existing: rs → rust-analyzer
   Missing:  ts, tsx, js, jsx → typescript-language-server
   ```
3. Propose merge:
   ```json
   {
     "servers": [
       {"extensions": ["rs"], "command": ["rust-analyzer"]},
       {"extensions": ["ts", "tsx", "js", "jsx", "mts", "cts"],
        "command": ["typescript-language-server", "--stdio"]}
     ]
   }
   ```
4. Probe `typescript-language-server` → `[ok]`.
5. Mention backup will be at `cclsp.json.bak-YYYYMMDD-HHMMSS`.
6. Confirm + write.
7. Phase 1: detect `Cargo.toml` + `tsconfig.json` → propose
   `["cargo", "check"]` + `["tsc", "--noEmit"]`.

### Example: Phase 0 finds strangers, leaves them alone

User: `/setup-compile-aware`.

Existing `cclsp.json` has `clangd` registered for C/C++, but the
project has zero `.c` / `.cpp` / `.h` files (only `.py`).

You:
```
Phase 0: cclsp.json — additive audit

Existing entries (preserved verbatim):
  ✓ py, pyi → pyright-langserver --stdio
  · c, cc, cpp, cxx, h, hh, hpp → clangd
    [info] No matching files in this project. Left untouched
    (additive-only). If this was intentional, ignore; if vestigial,
    remove the entry manually.

Project files found:
  142 .py files  ✓ covered

No additions proposed — every project language is already covered.
```

Move to Phase 1.

### Example: Multi-language monorepo with build tools

User: `/setup-compile-aware`.

You:
1. Phase 0: walk languages, propose additions for any not yet in
   `cclsp.json`. Confirm + write.
2. Phase 1: detect `Cargo.toml`, `tsconfig.json`,
   `pyproject.toml` (with `mypy` listed under dev-deps), and
   `go.mod`.
3. Ask: "I see Rust, TypeScript, Python (with mypy), and Go in
   this project. All four have a sensible default compile command.
   Want all four wired up, or should I drop any of them?"
4. After answer, propose the commands with `# why:` comments
   citing what you found.
5. Mention that 4 simultaneous compile workers will burn CPU — the
   debounce coalesces but you'll have one running thread per
   language at peak.
6. Confirm, write.

### Example: Project with no obvious build tools

User: `/setup-compile-aware`.

You:
1. Phase 0 completes (e.g., adds `pyright` for a Python project).
2. Phase 1: walk the project, find no build markers
   (`pyproject.toml` has no `mypy` or `pyright` in deps).
3. Tell the user: "I didn't find a `mypy` config, a Cargo.toml, a
   tsconfig.json, or anything similar. Compile-aware mode needs a
   per-language compile command — there's nothing to auto-suggest
   for this project. Do you have a custom build script or wrapper
   I should wire up? (e.g., `make check`, `./scripts/typecheck.sh`,
   or some Bazel/Buck command)"
4. If they describe one, propose accordingly.
5. If they don't, suggest leaving compile-aware off — the LSP
   layer alone (Phase 0) is enough for many projects.
