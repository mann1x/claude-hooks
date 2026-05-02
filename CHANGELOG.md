# Changelog

All notable changes to **claude-hooks** are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html):

- **MAJOR** — incompatible config / hook contract changes
- **MINOR** — new providers, new hook handlers, new opt-in subsystems
- **PATCH** — bug fixes and internal refactors that do not change behavior

Each release ships as a Git tag (`vX.Y.Z`) on `main` and a GitHub
release with the auto-generated source archive
(`claude-hooks-X.Y.Z.zip` / `.tar.gz`). See
[`docs/RELEASING.md`](docs/RELEASING.md) for the cut procedure.

## [Unreleased]

_(work in progress on the `dev` branch — see `git log v1.0.1..origin/dev`
for landed but not-yet-released commits.)_

### Added

- **PreCompact hook → wrap-up synthesiser** — new
  `claude_hooks/hooks/pre_compact.py` handler fires before Claude
  Code auto-compacts the conversation. Reads the session transcript,
  produces a deterministic eight-section `/wrapup`-shaped summary
  (mechanically-extractable parts filled in; model-judgment parts
  marked as `needs model`), persists it to disk (preferring `.wolf/`
  → `docs/wrapup/` → `~/.claude/wrapup-pre-compact/`), and emits the
  markdown as `additionalContext` so it lands inside the compaction
  window. Self-gates on (1) `hooks.pre_compact.enabled` (default
  true) and (2) the `/wrapup` skill being installed at
  `~/.claude/skills/wrapup/SKILL.md`. 17 unit tests in
  `tests/test_pre_compact.py`.
- **`/wrapup` skill: last-line file pointer** — the skill now always
  saves a copy to disk and ends its output with the exact pointer
  `**State summary saved to:** <abs-path> — Read this file to
  recover full session state.` Auto-compaction sometimes drops the
  inline output before the next session can read it; the file on
  disk is the only fully reliable carrier across the boundary, and
  the last-line position maximises the odds the post-compaction
  assistant sees the path. Edit applied to the canonical
  `.claude/skills/wrapup/SKILL.md` in the repo (deployed via
  `install.py`).

### Changed

- **Dispatcher table** — `PreCompact` event now routes to the new
  `pre_compact` handler.
- **`install.py`** — new `PRE_COMPACT_TEMPLATE` wires the hook into
  `~/.claude/settings.json`; `install_hooks()` gains
  `include_pre_compact` (defaults true).

### Fixed

- **Ollama `num_ctx` for gemma4 callers** — HyDE (`hyde.py`),
  `/reflect` (`reflect.py`), and `/consolidate` (`consolidate.py`)
  all use `gemma4:e2b` but none set `num_ctx` in the request body.
  Ollama keeps the FIRST loader's `num_ctx` sticky for the
  duration the model stays resident, so on a cold load the model
  inherited the 4k Modelfile default — and a different caller
  passing a different value would force a full reload + KV-cache
  rebuild. All three callers now pass `num_ctx=16384` (matching
  the pgvector embedder's existing 16k pin), with config knobs
  `user_prompt_submit.hyde_num_ctx`, `reflect.num_ctx`, and
  `consolidate.num_ctx` for overrides. Set them in lockstep —
  mismatched values across the three thrash the resident model.

## [1.0.1] — 2026-05-01

> Note on the version bump: by the SemVer rules in
> `docs/RELEASING.md`, "new opt-in subsystem" is normally a **MINOR**
> bump. v1.0.1 was chosen here as a deliberate exercise of the
> release workflow on a small, low-risk delta — treat this as
> precedent for "first follow-up release after the 1.0 cut," not
> as a recategorization of the SemVer rules.

### Added

- **Self-update check** — opt-in periodic poll of GitHub
  `releases/latest`. The daemon thread runs the check at most once
  every 24 hours (configurable). The Stop hook surfaces a
  `[claude-hooks] update available: vX.Y.Z` notice in its
  `systemMessage` when a newer tag is published.
  - Runs on the long-lived `claude-hooks-daemon` thread so the
    Stop hook never blocks on network I/O.
  - Failed checks retry up to 5 times at 5-minute intervals, then
    defer to the next 24-hour window.
  - Notification budget: the notice surfaces at most 10 times per
    discovered release before going silent until the next check
    finds a newer tag.
  - Silent on failure: timeouts, DNS errors, and HTTP errors all
    resolve to "no update" without raising or logging at info level.
  - Disable at runtime by setting `update_check.enabled` to `false`
    in `config/claude-hooks.json` — both the daemon poll and the
    Stop-hook notice stop immediately, no restart needed.
  - State persists in `~/.claude/claude-hooks-update-state.json`.
  - 35 unit tests in `tests/test_update_check.py`.
- **`install.py` self-update prompt** — installer asks
  "Do you want to automatically check every 24 hours for a new
  release?" and persists the answer to `update_check.enabled`.
  Warns when the daemon is disabled (the feature requires it).

### Fixed

- `claude_hooks/__init__.py` `__version__` was stale at `0.4.0`;
  bumped to match the package release (1.0.1).

## [1.0.0] — 2026-05-01

First tagged release. Consolidates all work prior to the move to a
proper branch + release workflow. The codebase has been operating in
production on solidpc and pandorum for months; v1.0.0 is the formal
cut, not a feature break.

### Highlights

- **Memory recall + storage** — deterministic `UserPromptSubmit`
  recall and `Stop` storage across pluggable providers (Qdrant,
  Memory KG, pgvector, sqlite-vec).
- **HyDE-expanded recall** — local Ollama (`gemma4:e2b` primary,
  `gemma4:e4b` fallback) generates hypothetical-document queries with
  on-disk caching.
- **Tier 1.3 detached store** — fork-and-return so the `Stop` hook
  doesn't block on provider writes.
- **Tier 3.8 daemon stack** — single long-lived Python process owns
  providers + config; each hook answers in milliseconds.
- **Transparent api.anthropic.com proxy** — opt-in HTTP proxy with
  SSE tail, rate-limit state file, retry-on-5xx, and SQLite
  rollups (schema v5).
- **Read-only stats dashboard** (port 38081) — JSON API + embedded
  HTML view; per-effort × per-day stop-phrase canary panel
  (stellaraccident #42796).
- **Stop-phrase canary** — in-stream scanner with 8 behavior
  categories from `config/stop_phrases.yaml`; daily health line via
  `claude-hooks-health.timer`.
- **In-process AST code-graph** — Python stdlib `ast`-driven by
  default; optional tree-sitter, Louvain clustering, and an MCP
  server for cross-tool integration.
- **Session-scoped LSP engine** — per-project daemon, Windows IPC
  parity (UNIX socket + named pipes), session-affinity locks,
  adaptive preload from the code-graph hot set, and opt-in
  compile-aware diagnostics merging `cargo check` / `tsc --noEmit`
  / `mypy` / `go vet` on top of the LSP layer.
- **PostToolUse ruff hook** — IDE-style diagnostics surfaced as
  `additionalContext` after Edit/Write/MultiEdit on Python files.
- **Caliber grounding proxy** — native-tools agent loop,
  `survey_project`, recall integration; full multi-harness skill
  mirroring across `.claude/`, `.agents/`, `.cursor/`.
- **Companion integrations** — OpenWolf (`.wolf/cerebrum.md`,
  `buglog.json`), axon, gitnexus, claudemem-reindex.
- **Cross-platform installer** — Linux, macOS, Windows; idempotent;
  preserves `_managedBy`-tagged hook entries on re-run.
- **System-wide `pgvector-mcp`** — stdio MCP server exposing pgvector
  recall + KG ops to any MCP-aware client.
- **Operator tooling** — `proxy_health_oneliner.py`, weekly token
  usage report, statusline segment, bench harnesses for recall and
  the LSP engine.

### Subsystem milestones (internal versioning, pre-1.0)

| Internal tag | Capability                                                                  |
|--------------|------------------------------------------------------------------------------|
| v0.2         | Recall pipeline (HyDE, decay, dedup), instincts, reflect, consolidate       |
| v0.4         | Pgvector + sqlite-vec providers, Caliber proxy, daemon stack                |
| v0.5         | Transparent API proxy, SQLite rollups, dashboard, stop-phrase canary        |
| v0.6         | In-process AST code-graph, MCP server, optional clustering                  |
| v0.7         | LSP engine (Phases 0-4), Windows IPC parity, compile-aware diagnostics      |
| **v1.0.0**   | Formal release cut + CHANGELOG + dev-branch workflow                         |

### Test coverage

~1.5k tests in `tests/` (run
`/root/anaconda3/envs/claude-hooks/bin/python -m pytest tests/ -q`).
Run `pytest --collect-only -q | tail -1` for the current count.

### Known issues at release

- Caliber 1.45.2 has a hook-recursion bug on `init`; use 1.45.3+ or
  see `memory/reference_caliber_timeouts.md`.
- Claude Code at `/effort xhigh` exhibits elevated
  ownership-dodging (~29/1k vs medium's ~2/1k) per the proxy canary;
  upstream issue [anthropics/claude-code#55301](https://github.com/anthropics/claude-code/issues/55301).
  Recommend `/effort medium` until upstream resolves.

### Upgrade notes

This is the first tagged release; there is no upgrade path from a
prior tag. From any unreleased checkout, just `git pull` on `main`
once `v1.0.0` is published. The on-disk config schema
(`config/claude-hooks.json` version 2) is unchanged from late-v0.7.

[Unreleased]: https://github.com/mann1x/claude-hooks/compare/v1.0.1...HEAD
[1.0.1]: https://github.com/mann1x/claude-hooks/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/mann1x/claude-hooks/releases/tag/v1.0.0
