---
name: setup-compile-aware
description: Configures the claude-hooks LSP engine for the current project. Phase 0 audits cclsp.json against the project's actual language files and proposes additive LSP entries. Phase 1 proposes [compile_aware.commands] for .claude-hooks/lsp-engine.toml by detecting build tools (Cargo.toml, tsconfig.json, pyproject.toml, go.mod, Makefile). Use when the user wants Claude to set up or extend LSP coverage or wire pyright / gopls / rust-analyzer / clangd / cargo check / tsc --noEmit / mypy into the LSP engine.
---

# Set up the LSP engine for this project

This skill walks two files in sequence and writes only what the
user explicitly confirms:

- **Phase 0** — audit `cclsp.json` (LSP server registry) and
  propose **additive-only** entries for project languages not yet
  covered.
- **Phase 1** — propose `[compile_aware.commands]` in
  `.claude-hooks/lsp-engine.toml` based on detected build tools.

See `docs/lsp-engine.md` for the engine architecture.

## Constraints (both phases)

- **Never write without explicit confirmation** ("yes" / "go" /
  "do it").
- **Additive only on `cclsp.json`.** Never propose removing or
  modifying an existing entry — the file may contain custom flags
  (`--strict`, `--remote=auto`) or manual entries you can't infer
  the intent of.
- **No auto-detection magic on compile commands.** Surface what
  you found, let the user pick. Wrong flags (`--strict`,
  `--release`) silently waste minutes on every save.
- **Add `# why:` comments** above each `[compile_aware.commands]`
  entry. (JSON has no comments, so explain `cclsp.json` choices
  in chat before writing.)
- **Probe binaries** with `command -v <bin>` before proposing.
  Surface the install hint from `claude_hooks/lang_servers.py` if
  missing.

---

## Phase 0 — cclsp.json audit

### Step 0.1: Read existing `cclsp.json`

```bash
cat cclsp.json 2>/dev/null
```

Missing → init flow (nothing to preserve). Present → parse, then
capture `covered_extensions = ⋃ server.extensions`.

### Step 0.2: Walk the project for language files

Use `Glob` to count files per language at the top level. **Skip**
`node_modules/`, `.venv/`, `venv/`, `dist/`, `build/`, `target/`,
`__pycache__/`, `.git/`, and other vendored/generated dirs.

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

**Threshold: ≥ 3 files** OR a build marker that implies the
language (`Cargo.toml` → Rust regardless of file count). A single
stray `.py` file in a Rust project doesn't warrant wiring up
pyright. State the threshold to the user up front.

### Step 0.3: Compute the gap

Three sets:

1. **Missing** — significant file count, NOT in
   `covered_extensions`. These are the additive candidates.
2. **Covered** — already wired up. Preserve verbatim.
3. **Strangers** — extensions registered with ZERO files in the
   project. Surface informationally (e.g. "`clangd` registered
   but no `.c/.cpp/.h` files — vestigial or future-work; I won't
   touch it"). **Never propose removal.**

### Step 0.4: Resolve LSP commands

For each **Missing** language pick the canonical command from
`claude_hooks/lang_servers.py:SPECS` (Tier 1) or the table above
(Tier 2). Extensions are **lowercase, no leading dot** (engine
convention, `claude_hooks/lsp_engine/config.py:158`).

### Step 0.5: Probe each proposed binary

Run `command -v <bin>` via Bash:

- **Found** → mark `[ok]`, proceed.
- **Missing, Tier 1** → mark `[!! install needed]` + surface the
  install hint (e.g. `npm install -g pyright`,
  `go install golang.org/x/tools/gopls@latest`). Ask whether to
  skip or proceed — registering an absent binary isn't
  destructive (engine returns `[]` until the binary lands), just
  slightly noisy.
- **Missing, Tier 2 / manual** → same, install hint is "see
  `docs/lsp-engine.md`".

### Step 0.6: Propose and confirm

Show the merged-result preview (existing + new), then ask
`[Y/n/edit]`:

- **Y / yes / go** → write the merged JSON. Existing entries
  first (verbatim), new additions appended.
- **n / no / skip** → don't write. Print the proposed JSON for
  manual editing. Proceed to Phase 1.
- **edit / drop X** → user names entries to drop from the
  proposal. Re-show, re-confirm.

### Step 0.7: Write the merged file

After explicit yes:

1. Backup: `cclsp.json.bak-YYYYMMDD-HHMMSS`.
2. Merge: existing entries verbatim, new additions in propose-
   order.
3. Write with `indent=2` (matches the installer starter format).
4. Verify with a read-back diff.
5. Tell the user: restart the Claude Code session (or re-run
   `connect_or_spawn`) to pick up the changes. Rollback =
   restore from backup or delete new entries.

### Step 0.8: Skip-with-grace path

Zero additions (every project language already covered)? Say so
and move on:

```
Phase 0: cclsp.json — already covers all project languages
(.py → pyright, .go → gopls). No changes proposed.
```

Don't write a no-op.

---

## Phase 1 — compile_aware commands

### Step 1.1: Detect build-tool markers

Walk the project root:

| Marker file | Implies | Default suggestion |
|---|---|---|
| `Cargo.toml` | Rust | `["cargo", "check", "--message-format=json"]` |
| `tsconfig.json` | TypeScript | `["tsc", "--noEmit"]` |
| `package.json` with `typescript` in deps | TypeScript (no tsconfig) | `["npx", "tsc", "--noEmit"]` |
| `pyproject.toml` with `mypy` in deps | Python (mypy) | `["mypy", "--strict", "<package>"]` (replace `<package>` with src dir) |
| `pyproject.toml` with `pyright` in deps | Python (pyright CLI) | usually skip — LSP already runs pyright per-file |
| `setup.cfg` or `mypy.ini` | Python (mypy) | `["mypy", "."]` |
| `go.mod` | Go | `["go", "vet", "./..."]` |
| `CMakeLists.txt` | C/C++ | manual choice — too varied |
| `Makefile` with `check` / `lint` / `test` target | varies | `["make", "check"]` (confirm target exists first) |
| `pubspec.yaml` | Dart/Flutter | `["dart", "analyze"]` |
| `mix.exs` | Elixir | `["mix", "compile", "--warnings-as-errors"]` |

For ambiguity (multiple Python type-checkers, monorepo with
several languages, custom build wrappers), **ask the user**
before proposing.

### Step 1.2: Read existing `lsp-engine.toml`

```bash
cat .claude-hooks/lsp-engine.toml 2>/dev/null
```

Empty/missing → create from scratch. Has `[compile_aware]` →
preserve non-`commands` knobs (debounce etc); only modify the
`commands` table and `enabled` flag with the user's blessing.
Has unrelated sections (`[preload]`, `[session_locks]`) →
preserve verbatim.

### Step 1.3: Propose the block

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

Each `# why:` comment cites **observed evidence from the project**
("Cargo.toml workspace with 4 crates", "mypy in pyproject.toml
dev-dependencies"). Future sessions read these comments to
understand the choice.

### Step 1.4: Surface trade-offs

Before confirming, name 2-3 things the user should know:

- **Run cost.** Estimate command duration — if `cargo check`
  takes 30 s, that's a debounced 30 s every time edits go quiet.
  Suggest extending `debounce_seconds` if slow.
- **CI parity.** Different flags in CI (`--locked`, `--strict`)
  cause diagnostic drift between local and CI. Match where
  reasonable.
- **`enabled = false` for now.** Recommend leaving the flag off
  until the user has smoke-tested one command. Easier to debug
  a single command than a daemon of them.

### Step 1.5: Ask for confirmation

AskUserQuestion (or plain "want me to write this?"):

- **Write as-is** → write the TOML, advise on flipping
  `enabled = true` once smoke-tested.
- **Adjust** → which commands to drop/change.
- **Don't write** → leave the proposed block printed for manual
  editing.

### Step 1.6: Write the file

After explicit yes:

1. Create `.claude-hooks/` if needed.
2. Read existing `lsp-engine.toml`, preserve unrelated sections.
3. Write with proposed block + `# why:` comments.
4. Verify by diff (`diff /tmp/old.toml .claude-hooks/lsp-engine.toml`).
5. Tell the user:
   - Smoke-test: `claude-hooks-lsp status --project .` (the shim
     resolves the right Python + PYTHONPATH; the bare
     `python -m claude_hooks.lsp_engine` invocation does NOT work
     because the package isn't pip-installed by default — see
     `bin/claude-hooks-lsp` and `bin/_resolve_python.sh`).
   - Enable: edit `enabled = false` → `true`.
   - Rollback: delete the file or comment the block.

### Step 1.7: Don't enable automatically

Default to `enabled = false` unless the user specifically says
"and turn it on". Most users want to inspect proposed commands
before paying the per-edit cost.

---

## Examples

### Example: Phase 0 init or additive audit

User: `/setup-compile-aware` on a project that has either no
`cclsp.json` (init) or has one that doesn't cover every language
present (audit).

You:
1. Read `cclsp.json` (or note its absence).
2. Walk the project. Example finds:
   - 87 `.ts` / `.tsx` files (NOT covered)
   - 42 `.py` files (covered, if file existed) or to-be-added (init)
   - 3 stray `.js` config files (flag, below threshold)
   - `clangd` registered for C/C++ but zero `.c/.cpp/.h` files →
     surface as stranger, leave untouched
3. Probe binaries:
   - `pyright-langserver` → `[ok]`
   - `typescript-language-server` → `[!! install needed: npm install -g typescript-language-server typescript]`
4. Note that registering an absent binary is safe — engine
   returns `[]` for `.ts` until you `npm install -g`. Ask
   whether to register anyway or wait.
5. Confirm + write merged JSON. Move to Phase 1.

### Example: Multi-language monorepo

User: `/setup-compile-aware`.

You:
1. Phase 0: walk languages, propose additions for uncovered
   ones. Confirm + write.
2. Phase 1: detect `Cargo.toml`, `tsconfig.json`,
   `pyproject.toml` (with mypy), and `go.mod`.
3. Ask: "I see Rust, TypeScript, Python (mypy), and Go. All
   four have sensible defaults. All four, or drop any?"
4. Propose with `# why:` comments citing what you found.
5. Mention 4 simultaneous compile workers burn CPU — debounce
   coalesces but you'll have one running thread per language at
   peak.
6. Confirm + write.

### Example: No build tools detected

User: `/setup-compile-aware`.

You:
1. Phase 0 completes (e.g., adds `pyright` for a Python
   project).
2. Phase 1: walk, find no build markers (`pyproject.toml` has
   no mypy/pyright in deps).
3. Tell the user: "I didn't find a mypy config, Cargo.toml,
   tsconfig.json, or similar. Compile-aware needs a per-
   language compile command — nothing to auto-suggest. Got a
   custom build script? (e.g., `make check`,
   `./scripts/typecheck.sh`, Bazel/Buck command)"
4. If they describe one, propose accordingly.
5. If not, suggest leaving compile-aware off — the LSP layer
   alone (Phase 0) is enough for many projects.
