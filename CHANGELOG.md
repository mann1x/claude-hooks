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

_(work in progress on the `dev` branch — see `git log dev..main` for
landed but not-yet-released commits.)_

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

[Unreleased]: https://github.com/mann1x/claude-hooks/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/mann1x/claude-hooks/releases/tag/v1.0.0
