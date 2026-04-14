# Plan: Integrate code-factory hooks into claude-hooks

**Source**: [rtfpessoa/code-factory](https://github.com/rtfpessoa/code-factory) — rtfpessoa's Claude Code & OpenCode marketplace.

## What we're adopting

Three of the four hooks in `code-factory/hooks/` are in scope.
`brag-reminder.sh` is personal workflow tied to code-factory's `/brag`
slash command and stays out.

### 1. Stop-phrase guard (HIGH priority)

**File in source**: [`hooks/stop-phrase-guard.sh`](https://github.com/rtfpessoa/code-factory/blob/main/hooks/stop-phrase-guard.sh)

Scans the last assistant message on `Stop` events for "ownership-dodging"
and "session-quitting" phrases (e.g. "pre-existing issue", "known
limitation", "should I continue", "good stopping point") and returns
`decision: block` with a correction that forces the assistant to continue.
Prevents infinite loops via the `stop_hook_active` flag.

The patterns are hardcoded and reflect rtfpessoa's golden rules
("NOTHING IS PRE-EXISTING", "Sessions are unlimited"). For claude-hooks
we want this to be **pattern-configurable** so users can add/remove
triggers without forking.

**Plan**: add `claude_hooks/hooks/stop_guard.py`
- Reads `hooks.stop_guard` from config
- Patterns loaded from `stop_guard.patterns` (list of `{pattern, correction}`)
- Ship sensible defaults derived from code-factory but clearly marked as
  opinionated (users can replace with an empty list)
- Disabled by default — opt-in via `hooks.stop_guard.enabled: true`
- Register in `dispatcher.py` to fire on `Stop`
- Respects `stop_hook_active` to avoid loops

### 2. Command safety scanner (HIGH priority)

**File in source**: [`hooks/command-safety-scanner.sh`](https://github.com/rtfpessoa/code-factory/blob/main/hooks/command-safety-scanner.sh)

PreToolUse hook that scans the **full Bash command string** (not just
the prefix) for dangerous patterns — `sudo`, `rm -rf` (even after pipes,
in `find -exec`, in subshells), `mkfs`, `dd`, `curl | sh`, `git push --force`,
etc. Returns `permissionDecision: "ask"` on match, forcing user
confirmation. Safe commands auto-approve.

Complements the prefix-based allow-list in `~/.claude/settings.json`. The
allow-list catches `rm -rf` at command start; this catches
`ls && rm -rf /tmp/foo` where `rm -rf` is hidden after a chain.

Also maintains an append-only JSONL log of scanner decisions under
`~/.claude/permission-scanner/YYYY-MM-DD.jsonl` with 90-day rotation.

**Plan**: replace the existing stub `hooks.pre_tool_use` config with a
real handler `claude_hooks/hooks/pre_tool_use.py`:
- Port pattern list from the bash script to a Python constant
- Port log rotation using stdlib (`pathlib`, `datetime`)
- Make patterns configurable: defaults + `hooks.pre_tool_use.extra_patterns`
- Disabled by default (stays opt-in as documented), but with a much
  richer default when enabled
- Respects `SCANNER_NO_LOG` env and `hooks.pre_tool_use.log_retention_days`
- Register in `dispatcher.py` to fire on `PreToolUse`

### 3. rtk command rewriter (MEDIUM priority, opt-in)

**File in source**: [`hooks/rtk-rewrite.sh`](https://github.com/rtfpessoa/code-factory/blob/main/hooks/rtk-rewrite.sh)

PreToolUse hook that shells out to [`rtk rewrite`](https://github.com/rtk-ai/rtk)
(a Rust CLI, [rtk-ai/rtk](https://github.com/rtk-ai/rtk), not to be
confused with the similarly-named "Rust Type Kit") to transparently
substitute verbose `find` / `grep` / `git log` / `du` style commands with
terser rtk equivalents. Claims 60-90% token reduction on matching
commands (their number; real savings depend heavily on command mix).

The hook returns `permissionDecision: "allow"` with an `updatedInput`
payload containing the rewritten command. Claude never sees the original
— the Bash tool just runs the rewritten version and the output comes
back compacted.

**Fallback design (already safe):**
- `jq` not installed → warn, exit 0, command unchanged
- `rtk` not installed → warn, exit 0, command unchanged
- `rtk` version < 0.23.0 → warn once, exit 0, command unchanged
  (`rtk rewrite` subcommand added in 0.23.0)
- `rtk rewrite` exits 1 (no rewrite known) → silent pass-through
- `rtk rewrite` output == input → silent pass-through

This makes the hook **safe to enable even on hosts that don't have rtk**
— it just no-ops. Rented/ephemeral pods without rtk are fine; the hook
runs on the host where Claude Code lives, not on the pod.

**What rtk does NOT fix:** the escaping/heredoc retry problem. rtk
rewrites target verbose output, not bash quoting. Tokens lost to
heredoc/subshell quoting retries are a separate issue.

**Version state (2026-04-14):** rtk-ai/rtk is at v0.36.0. Both solidPC
and pandorum have 0.36.0 installed (after replacing the unrelated
"Rust Type Kit" tool that also uses the `rtk` binary name — common
name collision).

**Plan**: port to Python as `claude_hooks/hooks/rtk_rewrite.py`:
- Check `rtk` on PATH; if missing, exit 0 silently (no warning spam —
  log once per session max)
- Version probe with 5s timeout; require >= 0.23.0
- Subprocess `rtk rewrite <cmd>`; if exit 1 or unchanged, pass through
- Emit `hookSpecificOutput.updatedInput.command` with rewritten value
- Config:
  - `hooks.rtk_rewrite.enabled: false` (default)
  - `hooks.rtk_rewrite.min_version: "0.23.0"`
  - `hooks.rtk_rewrite.timeout: 3.0`
  - `hooks.rtk_rewrite.log_rewrites: false` (debug aid, off by default)
- Tests: mock the rtk subprocess; verify pass-through, rewrite emission,
  timeout behaviour, missing-binary behaviour

**Cross-platform:** rtk ships MSI and portable zip for Windows. The
Python port uses `shutil.which` and subprocess, which work identically
on both. No bash dependency.

### 4. Reference in docs

**Plan**: add a credit line in:
- `README.md` — under a new "Credits & inspiration" section
- `docs/PLAN-code-factory-integration.md` — this file, archived for
  history
- Top-of-file comment in each ported hook pointing back to the
  original bash script

## Design decisions

1. **Port, don't shell out**. claude-hooks is Python-stdlib-only; we
   don't want to add a bash dependency. Ports also let us unify logging,
   config, and test coverage with the rest of the codebase.

2. **Opt-in, not on-by-default**. Both hooks change agent behavior in
   noticeable ways — stop_guard rejects assistant stops; pre_tool_use
   interrupts tool execution. Users should flip them on deliberately
   after reading docs.

3. **Configurable patterns, opinionated defaults**. rtfpessoa's
   hardcoded strings are a good starting set but reflect his CLAUDE.md
   rules. We surface them as defaults and let users override.

4. **No brag-reminder**. It's tied to the `/brag` slash-command from
   code-factory's productivity plugin, which we don't ship.

5. **No rtk-rewrite**. Requires installing the `rtk` Rust binary and
   its rewrite registry. Meaningful work only if users want to adopt
   rtk; we can document the hook pattern (external-tool-delegating
   PreToolUse) as an example without shipping it.

## Phases

### Phase 1 — Stop-phrase guard (small, self-contained)
- [ ] `claude_hooks/hooks/stop_guard.py`
- [ ] Default patterns in `claude_hooks/stop_guard_patterns.py`
- [ ] Config schema update in `claude_hooks/config.py`
- [ ] Wire into `dispatcher.py` Stop event
- [ ] Tests: matching/non-matching, loop protection, empty message
- [ ] Docs: README section, config example

### Phase 2 — Command safety scanner (larger, replaces stub)
- [ ] `claude_hooks/hooks/pre_tool_use.py`
- [ ] Default pattern list in `claude_hooks/safety_patterns.py`
- [ ] Log rotation helper in `claude_hooks/logging_util.py`
- [ ] Config schema update
- [ ] Wire into `dispatcher.py` PreToolUse event
- [ ] Tests: 10+ pattern variants, log rotation, SCANNER_NO_LOG
- [ ] Docs: explain relationship with settings.json allow-list

### Phase 3 — rtk rewriter (medium, optional runtime dep)
- [ ] `claude_hooks/hooks/rtk_rewrite.py`
- [ ] Cached version probe (avoid spawning rtk twice per call)
- [ ] Config schema update
- [ ] Wire into `dispatcher.py` PreToolUse event (ordered BEFORE
      safety scanner so rewritten commands are scanned too)
- [ ] Tests: subprocess mocking, timeout, version-guard, missing-binary
- [ ] Docs: what rtk is, how to install, the name-collision warning
      (rtk-ai/rtk vs Rust Type Kit — both claim the `rtk` binary)

### Phase 4 — Documentation
- [ ] README "Credits & inspiration" section linking to code-factory
- [ ] Per-hook doc page with config examples
- [ ] Migration note if user already wired the bash hook directly

## Out of scope

- `/brag`, `/daily`, `/do`, `/reflect` slash commands — those are
  skills, not hook infrastructure, and the project already has its own
  `reflect` / `consolidate` skills.
- The other code-factory infrastructure (`.claude-plugin/`, `.codex/`,
  `init.sh`, `sync-*.sh`) — those are about multi-agent marketplace
  packaging, orthogonal to claude-hooks' scope.

## Policy on external binary dependencies

rtk porting partially breaks the "stdlib-only Python with no external
tool dependencies" promise for the **core** framework. The chosen
mitigation is a split:

- **Core hooks** (recall, store, session lifecycle, stop_guard,
  pre_tool_use safety scan) — stdlib-only, no binary dependencies.
- **Optional hooks** (rtk_rewrite) — may depend on an external binary
  that the user has to install. Disabled by default. When the binary
  is missing the hook no-ops silently so partial fleets work.

This keeps the default install footprint tiny while allowing users who
want rtk-style savings to opt in. Future external-binary hooks should
follow the same pattern.
