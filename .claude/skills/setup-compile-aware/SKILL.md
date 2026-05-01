---
name: setup-compile-aware
description: Proposes a [compile_aware.commands] block for .claude-hooks/lsp-engine.toml by detecting build tools in the current project (Cargo.toml, tsconfig.json, pyproject.toml, go.mod, Makefile, etc). Use when the user wants Claude to populate compile-aware commands or asks how to enable cargo check / tsc --noEmit / mypy in the LSP engine.
---

# Setup compile-aware commands for the LSP engine

This skill helps the user populate `[compile_aware.commands]` in
`.claude-hooks/lsp-engine.toml`. The user controls what runs — your
job is to detect the project's build tools, propose a sensible
command per language, and wait for explicit confirmation before
writing anything to disk.

**Background**: see `docs/lsp-engine.md` for what compile-aware does
and `docs/PLAN-lsp-engine.md` for the design rationale. The short
version: when enabled, the daemon runs `cargo check` / `tsc
--noEmit` / `mypy` / etc on a debounced background schedule and
merges their diagnostics into the same response the LSPs feed.
Disabled by default; explicitly opt-in via `enabled = true` AND a
non-empty `[compile_aware.commands]` table.

## Constraints

- **Never write the TOML file without explicit user confirmation.**
  The user must say "yes" or "go" or similar before any edit.
- **No auto-detection magic.** Surface what you found and let the
  user pick. If you guess flags they don't want (`--strict`,
  `--release`), they'll silently spend minutes on the wrong
  workload every save.
- **Add `# why:` comments** above each command. TOML supports
  comments — use them. The PostToolUse advisor will nag if you
  forget.
- **Don't touch `cclsp.json`.** That's a separate file, owned by
  cclsp, with its own format. You're only writing the per-project
  `.claude-hooks/lsp-engine.toml`.

## Steps

### Step 1: Detect build-tool markers

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

### Step 2: Read the existing lsp-engine.toml if any

```bash
cat .claude-hooks/lsp-engine.toml 2>/dev/null
```

- Empty / missing → you'll create the file from scratch.
- Has `[compile_aware]` already → preserve any non-`commands`
  knobs (debounce, etc); only modify the `commands` table and the
  `enabled` flag with the user's blessing.
- Has unrelated sections (`[preload]`, `[session_locks]`, etc) →
  preserve them verbatim.

### Step 3: Propose the block

Show the user the proposed `[compile_aware.commands]` block and the
`enabled` flag. Format:

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

### Step 4: Surface trade-offs

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

### Step 5: Ask for confirmation

Use AskUserQuestion (or just plain "want me to write this?") to
collect the explicit yes. Options:

- **Write it as-is** → write the TOML, confirm where, advise on
  flipping `enabled = true` once they've smoke-tested.
- **Adjust commands** → which ones to drop / change.
- **Don't write** → leave them with the proposed block printed for
  manual editing.

### Step 6: Write the file

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

### Step 7: Don't enable automatically

Even if the user says "set it up", default to `enabled = false`
unless they specifically say "and turn it on". Lots of users want
to inspect the proposed commands before paying the per-edit cost.

## Examples

### Example: Rust-only project

User: `/setup-compile-aware`

You:
1. Detect `Cargo.toml` at the project root.
2. Notice the workspace has 4 crates (read `[workspace.members]`).
3. Estimate `cargo check --message-format=json` takes ~5-15 s on a
   warm cache (you can't measure here, so say so).
4. Propose:

```toml
[compile_aware]
enabled = false  # flip after smoke-testing the cargo command

[compile_aware.commands]
# why: Cargo workspace with 4 member crates (api, core, cli, tests).
# --message-format=json gives structured diagnostics with E-codes;
# parser auto-detects this and emits Diagnostic.source="cargo".
rs = ["cargo", "check", "--message-format=json", "--workspace"]
```

5. Note: `cargo check` on a 4-crate workspace easily hits 10+ s on
   cold cache; consider `[session_locks] debounce_seconds = 2.0`
   too if the user is on a slow disk. Ask before adding it.

6. Wait for confirmation, then write.

### Example: Multi-language monorepo

User: `/setup-compile-aware`

You:
1. Detect `Cargo.toml`, `tsconfig.json`, `pyproject.toml` (with
   `mypy` listed under dev-deps), and `go.mod`.
2. Ask: "I see Rust, TypeScript, Python (with mypy), and Go in
   this project. All four have a sensible default compile command.
   Want all four wired up, or should I drop any of them?"
3. After answer, propose the commands with `# why:` comments
   citing what you found.
4. Mention that 4 simultaneous compile workers will burn CPU — the
   debounce coalesces but you'll have one running thread per
   language at peak.
5. Confirm, write.

### Example: Project with no obvious build tools

User: `/setup-compile-aware`

You:
1. Walk the project, find no markers.
2. Tell the user: "I didn't find Cargo.toml, tsconfig.json,
   pyproject.toml, go.mod, or anything similar. Compile-aware mode
   needs a per-language compile command — there's nothing to
   auto-suggest. Do you have a custom build script or wrapper I
   should wire up? (e.g., `make check`, `./scripts/typecheck.sh`,
   or some Bazel/Buck command)"
3. If they describe one, propose accordingly.
4. If they don't, suggest leaving compile-aware off — the LSP
   layer alone is enough for many projects.
