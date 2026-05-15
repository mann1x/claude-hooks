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

_(no entries yet — next batch of work since v1.5.3 lands here.)_

## [1.5.3] — 2026-05-15

PATCH — emergency hotfix for v1.5.2. The v1.5.2 prep accidentally
introduced a duplicate ``_wait_for_consultants_health`` function in
``install.py``. The original at line 4732 has the signature
``(port: int, *, timeout: float = 30.0) -> bool``; the new one I
added at line 1968 had ``(*, timeout: float = 15.0) -> None``.
Python's last-def-wins overrode the new one with the original, so
my caller at line 1965 (``_wait_for_consultants_health(timeout=15.0)``)
raised ``TypeError: missing 1 required positional argument: 'port'``
on any host with the consultants engine installed.

Test coverage missed it because the unit tests
``patch.object(install, "_wait_for_consultants_health")`` — patching
replaces whichever def Python resolved, so the wrong signature
slipped through.

### Fixed

- Removed the duplicate ``_wait_for_consultants_health`` definition.
  ``_restart_consultants_service`` now reuses the existing
  ``_wait_for_consultants_health(port, timeout)`` helper (which polls
  ``/v1/health``, matching what the consultants engine actually
  serves) instead of duplicating the loop with a different endpoint.
- Restart still completes successfully; the success / timeout
  messages are formatted by the caller now that the helper returns
  bool rather than printing itself.

### Tests

New regression guard in ``tests/test_install_service_restart.py``:
``test_consultants_restart_invokes_health_probe_cleanly`` calls the
real ``_restart_consultants_service`` end-to-end with the real
``_wait_for_consultants_health`` patched only at the return value.
Asserts the call uses the ``(port, *, timeout=...)`` signature so
any future signature drift raises in CI instead of in production.

Full suite: 2593 passing, 24 skipped (+1 over v1.5.2).

## [1.5.2] — 2026-05-15

PATCH — install.py end-of-install service restart. Closes a gap
that bit the 2026-05-15 v1.5.0 deploy: install.py declared
"daemon: responding ✓" while the running daemon process was still
executing pre-pull bytecode (newly-pulled code was on disk but
never imported until the daemon was killed manually). After this
fix, `git pull && python install.py` consistently picks up new
code without any manual restart step.

### Added

- `_restart_managed_services(dry_run, skip)` runs at end of
  `main()` and restarts both `claude-hooks-daemon` and the always-on
  `claude-hooks-consultants` service when they exist.
- `_restart_claude_hooks_daemon()` — cross-platform restart:
  - **Linux**: `systemctl restart claude-hooks-daemon.service` if
    `/etc/systemd/system/claude-hooks-daemon.service` exists.
  - **macOS**: `launchctl unload + load -w` on
    `com.claude-hooks.daemon.plist`.
  - **Windows**: `schtasks /End /TN claude-hooks-daemon` then
    `/Run /TN claude-hooks-daemon`. No UAC prompt since the task
    is owned by the current user.
  - After restart, polls the daemon's HMAC port (47018) for up to
    20 s. Prints `restarted + responding` on success; warns
    without failing the install on timeout.
- `_restart_consultants_service()` — same shape, targets the
  consultants engine. Linux uses `systemctl --user restart
  claude-hooks-consultants.service`; Windows uses
  `schtasks /End + /Run` on `claude-hooks-consultants`. Health
  check polls `http://127.0.0.1:38095/health` for up to 15 s.
  Smart-start mode (lazy-spawn) has nothing long-lived to recycle
  so it's silently skipped — the next cold spawn picks up new code.
- New `--skip-daemon-restart` flag for the rare case the user
  wants install.py to leave running processes alone. Prints a hint
  reminding them to run `claude-hooks-daemon-ctl restart` manually
  when they want the new code loaded.

### Fixed

- Both Linux (`_setup_systemd_daemon`, install.py:1573-1581) and
  Windows (`_install_daemon_windows_steps`, install.py:2044-2077)
  previously had a "if service already exists, just probe port +
  return" branch with no restart. That meant a re-run of
  `install.py` after `git pull` left the daemon running stale
  Python bytecode until something else killed the process.
  v1.5.2 layers the unconditional end-of-install restart on top,
  preserving the existing "don't recreate the systemd unit / task
  file if it's identical" idempotency.

### Tests

15 new tests in `tests/test_install_service_restart.py`:
`--dry-run` skips, `--skip-daemon-restart` skips + prints hint,
default calls both restarts; Linux systemctl path (skip when unit
missing, call with unit present, warn when systemctl fails, warn
when daemon doesn't come back); Windows schtasks path (skip when
task missing, /End-then-/Run sequencing, warn when /Run fails);
consultants service (skip when missing on Linux/Windows, restart
when present on both platforms); argparse flag wiring.

Full suite: 2592 passing, 24 skipped (+15 new, +0 regressions).

## [1.5.1] — 2026-05-15

PATCH — install.py hook-path drift safeguard + every-write backup
trail. Closes a silent destructive-rewrite bug that bit a real
deployment: running `install.py` from a second clone at a
different filesystem path used to rewrite all existing
`_managedBy: claude-hooks` hook entries in `~/.claude/settings.json`
to point at the new location with no warning, effectively
un-deploying the working install.

### Added

- **Path-drift detection** in `install_hooks`: compares the existing
  `_managedBy` hook commands' repo path against the current
  install.py invocation's repo path. On mismatch, raises
  `HookPathDrift` (exit 2) in `--non-interactive` mode; in
  interactive mode, prompts with a side-by-side path diff and only
  rewrites on explicit `y`. New `--rewire` flag overrides the
  refusal when an intentional clone migration is desired.
- **Semantic backup names**: `backup_path(p, reason="...")` now
  embeds a kebab-case reason tag in the timestamped backup filename
  (e.g. `settings.json.bak-20260515-074559-hook-rewrite`,
  `...-plugin-marketplace`, `...-env-vars`, `...-uninstall`). A
  directory of backups becomes readable at a glance.
- New `_backed_up_save_json(path, data, *, reason, dry_run=False)`
  helper that funnels every settings.json save through the
  backup-then-write path. Replaces three previously-unbacked
  `_save_json(settings_path, ...)` call sites (plugin marketplace
  registration, recommended-plugin enable, uninstall).
- 19 new tests in `tests/test_install_path_drift.py`:
  `backup_path` reason sanitization + suffix, `_backed_up_save_json`
  behaviour (writes / no-write / dry-run / backup-content),
  `_extract_existing_hook_repo_path` (empty / no-managed / POSIX /
  Windows / backslash command / most-common tie-break),
  `install_hooks` drift (non-interactive refuse, --rewire override,
  interactive Y/N, same-path idempotent, fresh-install no-prompt,
  semantic backup name verification).

### Fixed

- `_save_json(settings_path, ...)` calls at three sites that
  previously wrote without backing up (plugin marketplace
  registration, recommended-plugin enable, uninstall) now go
  through `_backed_up_save_json` so every mutation leaves a
  recovery trail.

### Background

The 2026-05-12 incident on pandorum: an install.py run from
`C:\Users\manni\dev\claude-hooks` (a second clone created
inadvertently) silently rewrote all 6 hook entries in settings.json
to point at the `\dev\` path. The scheduled tasks still ran the
daemon from `C:\Users\manni\claude-hooks`, leaving hooks and the
daemon out of sync for three days. Repaired manually 2026-05-15;
this patch makes the regression impossible in non-interactive mode
and loud in interactive mode.

Test count: 2577 passing, 24 skipped (+19 new, +0 regressions).

## [1.5.0] — 2026-05-14

MINOR — extends v1.4's llamafile integration from embedding-only to
the **chat-completion side**. HyDE, `/reflect`, `/consolidate`,
`/get-advice`, `/consultants`, and the `caliber-grounding-proxy`
can now route to a daemon-supervised local llamafile via a new
`llamafile://<label>` model identifier prefix. Bare-Ollama
identifiers and `:cloud` suffix continue to route to Ollama
unchanged — opt-in for existing installs.

### Added

- **Chat-model registry**: `~/.claude/llamafile-models.json`
  (schema v1), `claude_hooks/chat_model_registry.py` (load / save /
  list / add / remove / rename / copy with port-collision +
  GGUF-magic validation, schema migration).
- **Daemon-side `ChatModelManager`**: multi-instance variant of
  v1.4's `EmbeddingManager`. `dict[label, ProcessHandle]`, LRU
  eviction at `max_concurrent_loaded`, per-label idle reap
  (default 600 s; streaming chat calls update `last_activity_at`
  per chunk so long generations can't be reaped mid-call),
  per-label sticky CPU fallback on GPU spawn failure, registry
  mtime hot-reload, orphan GC.
- **Daemon RPC ops** (`_chat_model_ensure / _chat_model_status /
  _chat_model_shutdown / _chat_model_gc`) with typed wrappers in
  `daemon_client.py`. Best-effort semantics match v1.4 embedding ops.
- **Shared chat backend** (`claude_hooks/chat_backend.py`):
  `parse_model_ref`, `OllamaChatClient` (extracted from pre-v1.5
  `_call_ollama`), `LlamafileChatClient` (daemon-ensured, OpenAI
  `/v1/chat/completions`, port-cache TTL, retry-on-failure with
  re-ensure), `make_chat_client` factory, `call()` one-shot helper.
- **Agent-loop chat client factory** in `get_advice/chat_client.py`:
  `LlamafileAgentChatClient` (same `chat(payload) -> dict` interface
  as `ChatClient`, talks OpenAI `/v1/chat/completions` directly,
  maps OpenAI `usage` -> Ollama `last_usage` field names) +
  `make_agent_chat_client` factory. Both `/get-advice` CLI and
  `/consultants` runner construction sites use it.
- **caliber-grounding-proxy openai_compat mode**: new env var
  `CALIBER_GROUNDING_UPSTREAM_BACKEND=openai_compat` skips the
  OpenAI ↔ Ollama translation entirely (targets
  `<upstream>/v1/chat/completions`). Retry budget, empty-content
  detection, and FlapCounters still apply. Surfaced at `/health`
  for ops visibility.
- **`claude-hooks-models` CLI** (`bin/claude-hooks-models` +
  `.cmd`, `claude_hooks/models_cli.py`). Subcommands:
  `list / add / remove / rename / copy / show / path / probe /
  gc`. Daemon-talking subcommands degrade gracefully when the
  daemon is down.
- **`install.py` chat-backend dialog**: new
  `_setup_chat_backends` dispatcher wraps the existing
  `_setup_ollama_chat` and a new `_setup_llamafile_chat_models`
  sub-dialog (GGUF path + label + ctx + mode + port, optional
  wiring of `hyde_model_ref` / `reflect.model_ref` /
  `consolidate.model_ref`).
- **`*_model_ref` config keys**: `hooks.user_prompt_submit.{hyde_model_ref,
  hyde_fallback_model_ref}`, `reflect.model_ref`,
  `consolidate.model_ref`. Take precedence over the legacy
  `*_model` / `*_url` keys when set.

### Changed

- `hyde.py` / `reflect.py` / `consolidate.py`: bare-ref calls keep
  using the existing `_call_ollama` helper (so tests that
  monkeypatch it stay valid); `llamafile://<label>` refs dispatch
  through `chat_backend.call`.
- `Registry.__init__` resolves `DEFAULT_REGISTRY_PATH` at call time
  (was function-definition time) so test fixtures and installers
  can monkeypatch the constant.
- README `Where the system listens` table adds the 38093-38099
  chat-llamafile port range.
- CLAUDE.md status banner v1.4.0 -> v1.5.0; new Key directories
  bullet for the chat engine.

### Tests

2558 passing, 24 skipped (+225 from v1.4's 2333). Coverage:
registry CRUD + schema migration (61), `ChatModelManager`
lifecycle (36), daemon chat RPC (28), `chat_backend` (32), HyDE /
reflect / consolidate dispatch (10), agent-loop factory (12),
caliber openai_compat (7), models CLI (29), install dialog (10).

### Known limitations

- Cross-backend `extra_models` fan-out in `/consultants` reuses
  the role's primary client; same-backend fan-out works. Deferred
  to v1.5.1.
- llamafile picks one device per process; multi-GPU placement
  deferred.
- Registry is per-host; remote `llamafile://<host>/<label>` deferred.

### Docs

- New: [`docs/llamafile-chat-models.md`](docs/llamafile-chat-models.md),
  [`docs/whats-new.md`](docs/whats-new.md) (v1.5).
- Archived: `docs/whats-new.md` (v1.4) -> `docs/whats-new-v1.4.md`.
- Updated: [`docs/daemon.md`](docs/daemon.md) (new RPC ops table +
  chat-model lifecycle section),
  [`docs/caliber-proxy.md`](docs/caliber-proxy.md) (new
  "Pointing at llamafile" section),
  [`docs/llamafile-integration.md`](docs/llamafile-integration.md)
  (scope clarification), [`README.md`](README.md), [`CLAUDE.md`](CLAUDE.md).

## [1.4.0] — 2026-05-14

MINOR — adds **mozilla-ai/llamafile@0.10.1** as a fallback-capable
embedding engine for the local-embed providers (`pgvector` and
`sqlite_vec`), supervised by the existing `claude-hooks-daemon`.
A healthy install can now survive an Ollama outage; a fresh
install can run without an Ollama dependency at all.

Opt-in: existing installs keep their Ollama-only embedder until
`install.py` is re-run.

### Added

- **`LlamafileEmbedder` + `CompositeEmbedder`**
  (`claude_hooks/embedders.py`). The composite tries the primary
  (Ollama / OpenAI-compatible) on every embed and drops to the
  fallback on `EmbedderError`, with a dim-mismatch guard so the
  vector space stays stable across failover.
- **`EmbeddingManager`** (`claude_hooks/embedding_manager.py`) —
  daemon-side llamafile lifecycle. Spawn-on-demand, 5-minute idle
  reap (matches Ollama's `OLLAMA_KEEP_ALIVE=5m`), SIGTERM →
  10 s → SIGKILL ladder, PID-file at
  `~/.claude/embedding-server.pid` for re-adoption across daemon
  restarts. APE-binary `/bin/sh` shim on POSIX so the
  Cosmopolitan-Libc binary boots without binfmt_misc registration.
- **`gpu_probe`** (`claude_hooks/gpu_probe.py`) — `nvidia-smi` /
  `rocm-smi` / `vulkaninfo` chain with 2-second timeout. Used at
  install time to suggest defaults and at runtime to decide the
  `-ngl 99` vs `--gpu disable` spawn flag.
- **Daemon RPC ops** (`claude_hooks/daemon.py`,
  `claude_hooks/daemon_client.py`): `_embedding_ensure`,
  `_embedding_status`, `_embedding_shutdown` with typed
  best-effort wrappers.
- **HyDE / reflect / consolidate installer dialog**
  (`install._setup_ollama_chat`). Until v1.4 these sections had
  zero interactive prompts (hard-coded defaults in `config.py`).
  The dialog asks for the Ollama chat URL, HyDE model + fallback
  + `num_ctx`, and offers a shared-skills shortcut so reflect +
  consolidate inherit by default.
- **`install._setup_embedding_engine`** — parameterized embedder
  dialog now drives **both** pgvector and sqlite_vec; "use the
  previous provider's choice?" shortcut on the second invocation.
  OpenAI-compatible primary supported alongside Ollama-primary
  and llamafile-primary.
- **`install._setup_sqlite_vec_mcp`** — sqlite_vec previously had
  **zero** installer code; v1.4 pays back that latent gap.
- **`install._validate_qdrant_embedding` /
  `_validate_memory_kg_embedding`** — validate-only branches for
  the server-side-embedding MCPs. Probe connectivity, surface a
  one-line note about where the embedding model lives, never
  mutate `cfg`.
- **`vendor/llamafile/dist/Makefile`** — reproducible
  composite-build recipe. Two consecutive
  `make clean && make` invocations produce byte-identical output
  (verified SHA `414f6166...` for the canonical
  qwen3-embedding-0.6b-16k composite).
- **`vendor/llamafile/dist/SHA256SUMS.composite`** — committed
  in-tree; `install.py` verifies the GH-Release-downloaded asset
  against it (hard error with `redownload or rebuild` breadcrumb
  on mismatch).
- **`docs/llamafile-integration.md`** — architecture + installer
  flow + ops runbook.
- **~187 new tests** (2333 passed + 24 skipped at cut, up from v1.3.2's 2146):
  `test_embedders_llamafile.py`, `test_gpu_probe.py`,
  `test_embedding_manager.py` (incl. APE-wrap regressions),
  `test_daemon_embedding_rpc.py`,
  `test_install_embedding_engine.py`,
  `test_install_sqlite_vec_mcp.py`, `test_install_ollama_chat.py`,
  `test_install_validate_mcp_embedding.py`.

### Changed

- **`install._setup_pgvector_mcp`** now delegates its embedder
  dialog to `_setup_embedding_engine`; the DSN/schema/init path
  is unchanged. The Ollama-side model-pull stays in the
  Ollama-primary branch only.
- **`main()` ordering**: `_setup_ollama_chat` →
  `_setup_pgvector_mcp` → `_setup_sqlite_vec_mcp` →
  `_validate_qdrant_embedding` → `_validate_memory_kg_embedding`
  → `_setup_proxy_orchestrator`. The chat URL is settled before
  the embedder dialog uses it; the validate-only providers
  report after the client-embed providers are configured.
- **Canonical embedding port `38092`** — adjacent to caliber-proxy
  (38090) and consultants (38095).

### Fixed

- **Windows console-window detachment** in
  `EmbeddingManager._spawn_once` (`ff14f3a`). The spawned
  llamafile was inheriting a console on Windows because the code
  only passed POSIX `start_new_session=True`. v1.4 ships with
  `CREATE_NO_WINDOW | DETACHED_PROCESS` on Windows + stdin=DEVNULL,
  matching the pattern used by `claudemem_reindex._spawn_reindex`
  and `lsp_engine.client`. Verified on pandorum: the new spawn
  reports `Window Title: N/A` and no cmd window appears.

### Distribution

- **GitHub Release asset** for the composite (~1.5 GB) —
  `qwen3-embedding-0.6b-16k.llamafile` attached to the `v1.4.0`
  release. Clones stay small (~10 MB); `install.py` fetches the
  asset only when needed, verifies against the committed SHA, and
  falls back to `urllib.request` if `gh` is absent.

### Verified

- Reproducible composite builds on solidpc (Linux + RTX 3090).
- End-to-end through standalone daemon: cold-spawn 1.2 s, 1024-dim
  L2-normalized vector, idle reap clean.
- Full test suite green: 2333 passed + 24 skipped (final pre-cut run).

## [1.3.2] — 2026-05-13

PATCH — fixes a long-standing version-drift bug that caused the
update-check banner to misreport every release since v1.0.3.

### Fixed

- **`claude_hooks.__version__` no longer drifts from `pyproject.toml`.**
  The constant was hard-coded to `"1.0.3"` and never bumped during
  the v1.0.4 / v1.1.0 / v1.2.0 / v1.3.0 / v1.3.1 cuts (only
  `pyproject.toml`, `CHANGELOG.md`, and the `CLAUDE.md` banner were
  updated each time). The Stop-hook update-check banner reads
  `CURRENT_VERSION` from this constant, so every install reported
  itself as `current 1.0.3` — visible to users as e.g.
  `[claude-hooks] update available: v1.3.1 (current 1.0.3)` on a
  host that was actually running v1.3.1.
- **`claude_hooks/__init__.py`** now resolves `__version__` at import
  time via a three-step chain:
  1. `importlib.metadata.version("claude-hooks")` — canonical when
     pip-installed (editable or wheel).
  2. Walk up from `__file__` looking for `pyproject.toml`, parse
     `[project].version` with a tiny hand-rolled scanner (no
     `tomllib` import, keeps the 3.9 floor). This is the path the
     `bin/claude-hook` shim install model hits.
  3. Final string fallback (`"0.0.0+unknown"`) — only reached on a
     broken deploy; conservative so update-check reports "no update
     available" rather than hallucinating a build number.
- **`tests/test_version_no_drift.py`** pins the contract: a new
  test asserts `claude_hooks.__version__` equals the
  `pyproject.toml::[project].version` value. The cut procedure no
  longer relies on remembering to edit two files in lock-step.

## [1.3.1] — 2026-05-13

PATCH — single-bug fix for the `sqlite_vec` backend.

### Fixed

- **sqlite_vec is no longer silently skipped on every event.**
  `claude_hooks/dispatcher.py:build_providers` only checked for
  `mcp_url` (HTTP MCP backends) or `dsn` (pgvector) when extracting
  the per-provider URL it hands to `ServerCandidate.url`. The
  sqlite_vec provider — and its example config — write the path
  under `db_path`, so the dispatcher saw an empty URL and skipped
  the provider unconditionally with
  `provider sqlite_vec has no mcp_url/dsn configured — skipping`.
  Net effect on a sqlite_vec-only install: no DB was ever created,
  recall and storage were both no-ops for the lifetime of the
  install. The dispatcher now also accepts `db_path` and the log
  message reflects all three field names. Regression test in
  `tests/test_coverage_phase8.py::TestBuildProviders::
  test_sqlite_vec_db_path_accepted_as_url`.
  Reported and diagnosed end-to-end by
  [@JGFSnyman](https://github.com/JGFSnyman) in
  [#2](https://github.com/mann1x/claude-hooks/issues/2) — thanks!

## [1.3.0] — 2026-05-12

MINOR bump for a **user-facing slash-command vocabulary change**
— the per-verb skills shipped at v1.1 (`/get-advice--model`,
`/get-advice--effort`, `/get-advice--tools`,
`/consultants--config`, `/consultants--list`, `/consultants--show`,
`/consultants--followup`) are collapsed into two dispatcher
skills. Backing CLIs (`claude-advisor`, `claude-consultants`)
already subcommand-dispatch internally; the skill-file split was
pure duplication of that CLI shape and burned 9 entries in the
Claude Code slash-command menu (each with its own description).
The dispatcher pattern cuts that to 2 entries while keeping all
functionality.

### Changed (breaking — slash-command shape)

- **`/get-advice <query>`** is now a dispatcher with verbs:
  - `ask <query>` — run / continue an advisor conversation
    (default; **implicit** — bare `/get-advice <query>` works).
  - `model [NAME [CTX]]` — report or set the advisor's Ollama
    model and pinned context length. Replaces `/get-advice--model`.
  - `effort [tier]` — report or set the sessions-per-invocation
    budget (`low`/`medium`/`high`/`max`). Replaces
    `/get-advice--effort`.
  - `tools [csv|all|none]` — report or set the tool list exposed
    to the advisor. Replaces `/get-advice--tools`.
- **`/consultants <query>`** is now a dispatcher with verbs:
  - `ask <query>` — run a fresh council on a question (default;
    **implicit** — bare `/consultants <query>` works).
  - `followup [<sid>] <question>` — iterate on a prior session,
    failed-session-aware. Replaces `/consultants--followup`.
  - `list [--limit N]` — past sessions. Replaces
    `/consultants--list`.
  - `show <sid> [--raw]` — re-read a stored summary. Replaces
    `/consultants--show`.
  - `config [args...]` — interactive role/model/effort/service-
    mode walk-through, or passthrough sub-args. Replaces
    `/consultants--config`.
- The seven per-verb slash commands are **removed cold-turkey**;
  no aliases retained. Net upfront menu cost drops by ~7 skill
  descriptions per session; total skill body 42 KB → 28 KB.

### Added

- **Idempotent legacy-cleanup pass in `install.py`**
  (`_install_skills` → `LEGACY_SKILL_DIRS`). On upgrade, removes
  `~/.claude/skills/get-advice--{model,effort,tools}/` and
  `~/.claude/skills/consultants--{list,show,config,followup}/`
  so the old slash commands stop appearing in the menu. Runs
  unconditionally — no-op on fresh installs, removes on first
  v1.3 run, no-op on re-runs. Respects `--dry-run`.
- **6 new tests** at `tests/test_install_skills_legacy_cleanup.py`
  covering the cleanup contract (constant enumerates all v1.2
  variants, removes pre-seeded stale dirs, no-op fresh,
  idempotent re-run, dry-run prints but doesn't touch, SKILLS
  list registers only the two dispatchers).

### Documentation

- **`docs/get-advice.md`** — rewrites slash-command usage section
  to the verb form, adds a v1.3 migration note.
- **`docs/consultants.md`** — rewrites all `/consultants--*`
  references to `/consultants <verb>` form, adds a v1.3 migration
  note.
- **`docs/whats-new.md`** — preserved as historical v1.1 record;
  callout at top points readers at the v1.3 dispatcher shape for
  the up-to-date invocations.
- **`README.md`** — collapses the 9-row skills table section to
  2 rows showing the dispatchers with their verb lists.
- **`CLAUDE.md`** — status banner extended with the v1.3 paragraph.
- **`.wolf/anatomy.md`** — collapses the 9 skill entries to 2 with
  verb summaries.

## [1.2.0] — 2026-05-09

MINOR bump for the **caliber-grounding-proxy cloud-resilience
layer** — a new opt-in retry subsystem visible to any `caliber init`
run against a flapping cloud Ollama. Also ships the v1.2 of the
`/consultants` benchmark protocol (Q3 actionability sub-rubric, first
confirmed heterogeneous PROD-READY label) and the first caliber-eval
cohort published in-repo (six labels graded against the `claude-cli`
reference).

### Added

- **caliber-grounding-proxy cloud-resilience retry layer**
  (`claude_hooks/caliber_proxy/ollama.py`, shared
  `claude_hooks/_chat_retry.py`). Two parallel retry budgets
  protect every chat completion to upstream Ollama:
  - **15-attempt HTTP/network budget** with exponential backoff
    (base 1.5 s, cap 90 s, ≈ 15 min total). Catches `408 / 429 /
    500 / 502 / 503 / 504` plus a curated list of retryable 4xx
    body substrings (the same set the consultants engine
    already proved against `kimi-k2.6:cloud` flapping).
  - **5-attempt empty-content budget** for `200 OK` responses
    with empty `content`, no `tool_calls`, and
    `finish_reason ≠ length` — the "throat-clearing" pattern
    every cloud-tagged Ollama model exhibits on heavy initial
    prompts.

  Tunable via env vars: `CALIBER_PROXY_RETRY_MAX_ATTEMPTS`,
  `CALIBER_PROXY_RETRY_BASE_DELAY_S`, `CALIBER_PROXY_RETRY_MAX_DELAY_S`,
  `CALIBER_PROXY_EMPTY_RETRY_MAX`. Defaults match the consultants
  engine, so behavior is consistent across both cloud paths.
- **`FlapCounters` exposed at `/health.upstream_flaps`**. Five
  process-scoped counters surfaced in the `/health` JSON:
  `upstream_5xx_total`, `upstream_retryable_4xx_total`,
  `upstream_empty_total`, `upstream_retry_succeeded_total`,
  `upstream_retry_exhausted_total`. Lets `claude-hooks-rollup`
  (and any operator dashboard) detect cloud-weather degradation
  before it fails a bench.
- **Generic tool-call passthrough on the proxy round-trip.**
  Provider extras like Gemini's `thought_signature` are now
  preserved verbatim across both legs of the round-trip instead
  of being stripped — the earlier targeted strip broke
  `gemini-3-flash-preview:cloud` with
  `400 missing thought_signature in functionCall parts`. A small
  denylist (`function.index` for deepseek/qwen; empty at top
  level) handles the inverse case where an upstream field would
  confuse the OpenAI-compat client. Net effect: every cloud model
  that ships a custom tool-call extra works without per-model
  patches.
- **caliber-eval cohort published** at
  [`docs/caliber-eval-results/`](docs/caliber-eval-results/) —
  six labels graded against the `claude-cli` reference:
  `gemma-native-tools-v3`, `gemma4-31b-cloud`,
  `gemini-3-flash-preview-cloud`, `deepseek-v4-flash-cloud`,
  `glm-5-1-cloud`. Each label ships its `score.py` JSON + a
  narrative summary comparing to baseline. The workbench (full
  rsynced workspaces, run logs, fake-HOMEs) stays off-repo at
  `/srv/dev-disk-by-label-opt/dev/caliber-eval/` per
  `PROTOCOL.md`, which documents the reproduce + publish recipe.
- **`docs/caliber-eval.md`** — in-repo entry-point pointing at the
  off-repo workbench and the published-results dir.
- **`docs/PLAN-caliber-proxy-cloud-resilience.md`** —
  implementation plan that drove the resilience port (marked
  "shipped 2026-05-09").
- **27 new tests** at `tests/test_caliber_proxy_retry.py`
  covering the decision helpers (`is_retryable_status`,
  `is_retryable_empty_response`, `compute_backoff`) and an
  end-to-end mocked `httpx` harness exercising both budgets.
- **Updated `tests/test_caliber_proxy.py`** with passthrough
  coverage: `test_assistant_tool_calls_passthrough_unknown_fields`,
  `test_assistant_tool_calls_function_index_stripped`,
  `test_response_tool_call_extras_passthrough`,
  `test_response_tool_call_preserves_upstream_id`,
  `test_round_trip_preserves_provider_extras`.

### Fixed

- **consultants synthesizer no longer silently synthesizes over a
  failure tombstone.** The SYNTHESIZER prompts now refuse to render
  a coherent answer when an upstream role marked the section as
  failed, surfacing the failure in the final synthesis instead.
  Caught by the v1.2 protocol's Q3 actionability sub-rubric.

### Documentation

- **`/consultants` benchmark sweeps** — 2026-05-09 cloud screening
  (7 new models), N=3 aggregate runs of the 3 PROD-READY
  candidates, Q3 actionability re-grade against protocol v1.2,
  per-role recommendation refresh, and the first confirmed
  heterogeneous PROD-READY label
  ([`mix-gemini-PRC-gemma4-S-2026-05-09`](docs/benchmarks/mix-gemini-PRC-gemma4-S-2026-05-09/)).
- **`docs/benchmarks/index.md`** — refreshed TL;DR (5 PROD-READY
  labels at v1.1.0 engine HEAD), per-role token/wall winner
  matrix, cross-reference to the caliber cohort with the
  caliber-init verdict (`claude-cli` stays default; `glm-5.1:cloud`
  is the recommended non-claude-cli fallback).
- **`docs/caliber-eval-results/README.md`** — `tl;dr — verdict`
  section with the pick-when table and explicit disqualifications
  (`deepseek-v4-flash:cloud` and `gemini-3-flash-preview:cloud`
  both fail the references-point-to-real-files rubric — the same
  grounding-discipline weakness they show on consultants Q3).

## [1.1.0] — 2026-05-08

MINOR bump for several new opt-in subsystems landed since v1.0.3:
the `/get-advice` LLM-to-LLM advisor skill (multi-turn second
opinions via local Ollama), the shared `agent_loop.runner` that
backs both caliber and the advisor, the stop_guard stall check, a
full pgvector backup + canary stack, and the v1.1 of the
`/consultants` agentic engine — full per-role LLM message-history
persistence so a session closed and reopened from disk produces
identical follow-up answers to a warm one, plus multi-model
researcher (xmedium / xhigh) and multi-critic + meta-critic
(xmax) fan-out tiers for hard architectural questions where
diverse cloud-model perspectives matter. Ten phases on `dev`
(`9f71c9d`..`cdea074`) plus the planning commit (`5cb6738`).

### Added

- **/consultants v1.1 — multi-model x-tiers (xmedium / xhigh / xmax)**
  — three new effort tiers that fan out fan-outable roles across
  multiple Ollama models per plan-item lane, so the synthesizer
  (or meta-critic at xmax) sees diverse perspectives from
  different model trainings on the same evidence. Configured via
  per-role `extra_models = [...]` in the role's TOML block;
  silently ignored at every base tier (a benchmark labeled
  `high` is never accidentally 3× the cost — opting into x-tiers
  requires the explicit tier name). xmedium / xhigh only fan
  out the researcher; xmax additionally fans out the critic and
  adds a meta-critic node that synthesizes the C parallel-critic
  verdicts into one consolidated decision (anonymized as
  `Critic 1` / `Critic 2` / ... in the prompt to avoid biasing
  toward a model the meta-critic "knows" performs better; the
  recorder's per-row `model` column is the audit map). Cost-of-
  fan-out warning fires once at consultation start with the
  expected token-cost multiplier. Skill (`/consultants--config`)
  gains a "Manage extra models" sub-action under researcher /
  critic; CLI gains `set-role <role> --add-model X --remove-model
  Y --clear-extras`. Live-verified on solidpc — xmax with 2
  researcher models and 3 critic models produces 3 distinct
  critic verdicts (one per model) that the meta-critic
  consolidates. Ten phases on `dev` (`9f71c9d`..`cdea074`).

- **/consultants v1.1 — full message-history persistence** — every
  consultation now produces a SQLite `transcript.db` sidecar at
  `<cwd>/.claude-hooks/consultants/<sid>/transcript.db` alongside
  the existing `summary.md`, `transcript.md`, and `metadata.json`.
  The recorder writes one row per LLM call, tool execution, and
  node enter/exit boundary in WAL mode (concurrent fan-out lanes
  write through per-thread connections). When a session is
  reopened from disk after engine restart or eviction, the
  per-role LLM message threads are reconstructed via SQL —
  follow-ups against disk-reopened parents now extend those
  threads with the new question instead of rebuilding prompts
  from scratch. Live-verified: a follow-up against a closed
  parent issued **0 tool calls vs. the parent's 5** because the
  model could lean on prior tool results in context (the explicit
  v1.1-is-done criterion from the plan). SQLite was chosen over
  JSONL for opacity to text indexers (`claudemem reindex`,
  ripgrep, RAG ingestors) since `transcript.db` carries full LLM
  payloads. Schema documented at
  [`docs/consultants-transcript-db-schema.md`](docs/consultants-transcript-db-schema.md);
  inspect a session via `claude-consultants show --raw <sid>` with
  optional `--filter role=researcher --filter kind=tool_call
  --limit N`. Backward-compatible: v1.0 sessions without a `.db`
  reopen via the existing turn-content fallback. The legacy v1.0
  JSONL trace at `~/.claude/consultants-traces/<sid>.jsonl` is
  decommissioned; `CONSULTANTS_TRACE` and the `--trace` /
  `--no-trace` CLI flags are now no-ops with a one-shot
  deprecation warning (will be removed in v1.2). Plan and
  pre-implementation log: [`docs/PLAN-consultants-v1.1-message-history.md`](docs/PLAN-consultants-v1.1-message-history.md).

- **/get-advice — LLM-to-LLM advisor skill** — Claude Code can now consult
  a configured Ollama model (default `qwen3.5:cloud`) for a multi-turn
  second opinion via the `/get-advice <query>` skill. Three helper
  skills (`/get-advice--model`, `/get-advice--effort`,
  `/get-advice--tools`) configure model + ctx, effort tier (low=1
  session / medium=3 / high=5 / max=25), and the per-tool gate
  (CSV / `all` / `none`) without editing JSON. Settings persist to
  `~/.claude/get-advice-config.json`. New CLI `bin/claude-advisor`
  drives the conversation: `turn`, `reset`, `cleanup`, get/set
  subcommands. Per-turn JSON exposes `prompt_eval_count` /
  `eval_count` so Claude knows when to summarize and reset before the
  advisor's context fills (default threshold 85%). Reuses caliber-proxy
  grounding (project anchors + structure map) and the same six tools
  (`read_file`, `grep`, `glob`, `list_files`, `survey_project`,
  `recall_memory`) when enabled.

- **agent_loop.runner — shared tool-use loop** — extracted the agent
  loop from `claude_hooks.caliber_proxy.server.run_agent_loop` into a
  reusable `claude_hooks.agent_loop.runner.run_loop` function with a
  `LoopConfig` dataclass. Both the caliber grounding proxy and the new
  `/get-advice` advisor drive their conversations through this single
  loop, so every gemma4-era quirk (force-first-tool-call,
  force-answer-after, tool-call burst dedup + cap, preseed survey)
  benefits both consumers consistently. The runner is transport-
  agnostic: callers pass their own `chat_fn` and `tool_executor`.
  `caliber_proxy/server.py:run_agent_loop` is now a thin shim that
  reads env vars, builds the `LoopConfig`, prepends grounding, calls
  the runner, and applies the caliber-specific
  `sanitize_assistant_json` post-processor. Behavior unchanged — the
  full caliber-proxy test suite (91 tests across `TestAgentLoop` /
  `TestPreseedSurvey` / etc.) passes against the refactored path.
  v1.1 added optional `on_iter` / `on_tool` callbacks so consumers
  (notably the consultants `MessageRecorder`) can observe every
  chat round and tool execution without sub-classing the runner.

- **stop_guard: stall-after-commitment check** — catches a new failure
  mode observed on `claude-opus-4-7` (1M context): the model writes a
  paragraph ending with an action-commitment phrase ("Diving in now",
  "Writing the script now", "On it.") and then ends the turn WITHOUT
  calling any tool. The user has to nudge the session to unstall it.
  Three independent conditions stack so false-positive risk is low:
  (1) `stop_reason=end_turn`, (2) zero `tool_use` blocks in the
  message content, (3) one of the commitment phrases appears in the
  last ~250 chars of the message text. The Stop hook returns
  `decision=block` with a correction asking the model to either
  execute the action it described or ask a specific question. Honours
  the same user-wrap-up bypass as the prose-pattern guard so an
  "All done. On it." closing after the user said "wrap up" doesn't
  trigger. Default on when stop_guard itself is enabled; opt out via
  `hooks.stop_guard.stall_check_enabled = false`. New module entry
  points: `claude_hooks.stop_guard.check_stall_after_commitment`,
  `COMMITMENT_PATTERNS`, `STALL_CORRECTION`. 15 unit tests in
  `tests/test_stop_guard.py::StallAfterCommitmentTests` plus an
  end-to-end smoke through `_run_stop_guard`.
- **pgvector backup-validity canary** — new
  `claude-hooks-pgvector-backup-check.{service,timer}` runs every
  Monday at 02:43 local and walks each retention tier
  (daily/weekly/monthly), validating the most recent dump in two
  layers: (1) `pg_restore -l` for the TOC + metadata, (2)
  `pg_restore -f /dev/null` for a full byte-read of the archive
  (catches mid-file corruption that the TOC scan misses). Both
  layers run inside the `mcp-pgvector` container so the
  pg_restore version always matches whatever wrote the dump.
  Exits non-zero on any failure → wireable into `OnFailure=`.
  New script: `scripts/pgvector_backup_check.sh`.
- **pgvector daily backup timer** — new
  `claude-hooks-pgvector-backup.{service,timer}` runs
  `pg_dump -Fc` inside the `mcp-pgvector` container at 01:17 local
  every day and writes to `/shared/config/mcp-pgvector/backups/`
  with three retention tiers: 7 daily, 4 weekly (promoted on
  Sunday by hardlink), 3 monthly (promoted on day 1 by hardlink).
  `pg_dump` takes only `ACCESS SHARE` locks so reads + writes are
  not blocked during the backup. New scripts:
  `scripts/pgvector_backup.sh` (the worker) and
  `scripts/pgvector_restore.sh` (interactive restore helper with
  `latest_daily` / `latest_weekly` / `latest_monthly` shortcuts).
  Wired into `install.py` — installed when `providers.pgvector.enabled`
  is true. Tunables: `CONTAINER`, `PG_USER`, `PG_DB`, `BACKUP_DIR`,
  `KEEP_DAILY`, `KEEP_WEEKLY`, `KEEP_MONTHLY`, `WEEKLY_DOW`.

### Fixed

- **/consultants xmax — critic-fanout 6× cost overshoot** — the
  Phase 10 multi-critic dispatcher wired its conditional fan-out
  edge directly to `researcher`, which is itself Send-multiplexed
  by the Phase 9 researcher fan-out (N×M parallel invocations at
  x-tiers). LangGraph's `add_conditional_edges` from a
  Send-multiplexed source fires PER UPSTREAM SEND INVOCATION,
  not per-barrier-merge — so 6 researcher lanes spawned 6 ×
  C critic invocations instead of C. Caught on the first live
  xmax smoke (`csl-2026-05-07-1707-2a8f`): 18 critic LLM calls
  against an intended 3. Correctness wasn't affected — every
  critic still saw the same merged research and meta-critic
  consolidated correctly — but token cost was 6× the design.
  Fix inserts a single-invocation pass-through `research_barrier`
  node between researcher and the critic-fanout dispatcher.
  Unconditional edges from Send-multiplexed sources DO barrier-
  merge (this is how the legacy single-critic edge always worked),
  so routing through the barrier node forces the conditional
  fan-out to fire exactly once. Same question post-fix: 3 critic
  invocations, wall time 374s → 174s (54% faster). Test pins the
  count invariant: regardless of how many researcher lanes fan
  out, the critic fires exactly `1 + len(extra_models)` times.

- **axon-host crash-loop after host restart** — two compounding
  issues that put the unit into a 5s `Restart=on-failure` loop
  forever. (1) `/root/.axon` had been deleted between installs;
  the unit's `ReadWritePaths=/root/.axon` directive failed
  systemd's namespace bind-mount with `status=226/NAMESPACE`
  ("Failed to set up mount namespacing"). (2) The `claude-hooks`
  conda env had drifted — `uvicorn`, `httpx-sse`,
  `pydantic-settings`, and `sse-starlette` were silently dropped
  (likely from a partial reinstall during another env's build),
  and once the namespace bug was fixed axon crashed on import
  with `ModuleNotFoundError: No module named 'uvicorn'`. The
  loop just moved one step deeper. install.py now does two
  pre-flight checks before enabling the unit: `_ensure_axon_
  registry_dir` mkdir's `~/.axon/repos/` so the bind-mount has a
  target, and `_ensure_axon_deps` probes the env's import
  surface and pip-installs `requirements-axon.txt` (new file
  pinning the runtime deps) when anything is missing. Refuses to
  enable the unit when either pre-flight fails, so future drift
  becomes a clear `install.py` re-run rather than a silent
  service-loop.

- **PreCompact: stop emitting hookSpecificOutput** — Claude Code's
  PreCompact event schema does NOT accept `hookSpecificOutput`
  (only the universal `continue` / `stopReason` / `suppressOutput`
  envelope). Returning the wrap-up markdown as
  `hookSpecificOutput.additionalContext` failed CC's JSON validator
  with `(root): Invalid input` — the disk write succeeded but the
  hook was reported as failed every time the user resumed a session
  that had auto-compacted. The wrap-up file on disk is the sole
  delivery channel; `wrapup_recovery` already surfaces the pointer
  on the next post-compaction `UserPromptSubmit`, so dropping the
  inline context loses nothing. Handler now returns `None` on
  success. Existing `test_pre_compact.py` updated to pin the new
  contract.

### Added

- **/get-advice — LLM-to-LLM advisor skill** — Claude Code can now consult
  a configured Ollama model (default `qwen3.5:cloud`) for a multi-turn
  second opinion via the `/get-advice <query>` skill. Three helper
  skills (`/get-advice--model`, `/get-advice--effort`,
  `/get-advice--tools`) configure model + ctx, effort tier (low=1
  session / medium=3 / high=5 / max=25), and the per-tool gate
  (CSV / `all` / `none`) without editing JSON. Settings persist to
  `~/.claude/get-advice-config.json`. New CLI `bin/claude-advisor`
  drives the conversation: `turn`, `reset`, `cleanup`, get/set
  subcommands. Per-turn JSON exposes `prompt_eval_count` /
  `eval_count` so Claude knows when to summarize and reset before the
  advisor's context fills (default threshold 85%). Reuses caliber-proxy
  grounding (project anchors + structure map) and the same six tools
  (`read_file`, `grep`, `glob`, `list_files`, `survey_project`,
  `recall_memory`) when enabled.

- **agent_loop.runner — shared tool-use loop** — extracted the agent
  loop from `claude_hooks.caliber_proxy.server.run_agent_loop` into a
  reusable `claude_hooks.agent_loop.runner.run_loop` function with a
  `LoopConfig` dataclass. Both the caliber grounding proxy and the new
  `/get-advice` advisor drive their conversations through this single
  loop, so every gemma4-era quirk (force-first-tool-call,
  force-answer-after, tool-call burst dedup + cap, preseed survey)
  benefits both consumers consistently. The runner is transport-
  agnostic: callers pass their own `chat_fn` and `tool_executor`.
  `caliber_proxy/server.py:run_agent_loop` is now a thin shim that
  reads env vars, builds the `LoopConfig`, prepends grounding, calls
  the runner, and applies the caliber-specific
  `sanitize_assistant_json` post-processor. Behavior unchanged — the
  full caliber-proxy test suite (91 tests across `TestAgentLoop` /
  `TestPreseedSurvey` / etc.) passes against the refactored path.

- **stop_guard: stall-after-commitment check** — catches a new failure
  mode observed on `claude-opus-4-7` (1M context): the model writes a
  paragraph ending with an action-commitment phrase ("Diving in now",
  "Writing the script now", "On it.") and then ends the turn WITHOUT
  calling any tool. The user has to nudge the session to unstall it.
  Three independent conditions stack so false-positive risk is low:
  (1) `stop_reason=end_turn`, (2) zero `tool_use` blocks in the
  message content, (3) one of the commitment phrases appears in the
  last ~250 chars of the message text. The Stop hook returns
  `decision=block` with a correction asking the model to either
  execute the action it described or ask a specific question. Honours
  the same user-wrap-up bypass as the prose-pattern guard so an
  "All done. On it." closing after the user said "wrap up" doesn't
  trigger. Default on when stop_guard itself is enabled; opt out via
  `hooks.stop_guard.stall_check_enabled = false`. New module entry
  points: `claude_hooks.stop_guard.check_stall_after_commitment`,
  `COMMITMENT_PATTERNS`, `STALL_CORRECTION`. 15 unit tests in
  `tests/test_stop_guard.py::StallAfterCommitmentTests` plus an
  end-to-end smoke through `_run_stop_guard`.
- **pgvector backup-validity canary** — new
  `claude-hooks-pgvector-backup-check.{service,timer}` runs every
  Monday at 02:43 local and walks each retention tier
  (daily/weekly/monthly), validating the most recent dump in two
  layers: (1) `pg_restore -l` for the TOC + metadata, (2)
  `pg_restore -f /dev/null` for a full byte-read of the archive
  (catches mid-file corruption that the TOC scan misses). Both
  layers run inside the `mcp-pgvector` container so the
  pg_restore version always matches whatever wrote the dump.
  Exits non-zero on any failure → wireable into `OnFailure=`.
  New script: `scripts/pgvector_backup_check.sh`.
- **pgvector daily backup timer** — new
  `claude-hooks-pgvector-backup.{service,timer}` runs
  `pg_dump -Fc` inside the `mcp-pgvector` container at 01:17 local
  every day and writes to `/shared/config/mcp-pgvector/backups/`
  with three retention tiers: 7 daily, 4 weekly (promoted on
  Sunday by hardlink), 3 monthly (promoted on day 1 by hardlink).
  `pg_dump` takes only `ACCESS SHARE` locks so reads + writes are
  not blocked during the backup. New scripts:
  `scripts/pgvector_backup.sh` (the worker) and
  `scripts/pgvector_restore.sh` (interactive restore helper with
  `latest_daily` / `latest_weekly` / `latest_monthly` shortcuts).
  Wired into `install.py` — installed when `providers.pgvector.enabled`
  is true. Tunables: `CONTAINER`, `PG_USER`, `PG_DB`, `BACKUP_DIR`,
  `KEEP_DAILY`, `KEEP_WEEKLY`, `KEEP_MONTHLY`, `WEEKLY_DOW`.

### Late additions (post-2026-05-07 cut-prep work, landed 2026-05-08)

The 2026-05-07 batch above was complete but uncut — pyproject.toml
was bumped to 1.1.0 with a "prep for tag, not yet cut" commit
(`f984d73`). The day before the actual cut produced four more sets
of changes that landed under the same MINOR version because they're
all extensions of the v1.1 work above (cloud-flap recovery for the
new `/consultants` engine, install.py glue so the new skill CLIs
resolve on every platform, and a documentation pass for the v1.1
surface).

#### Added (2026-05-08)

- **/consultants — synthesizer fallback chain on persistent
  failure** — when the primary synthesizer model exhausts its
  ChatClient retry budget on a cloud flap (HTTP 500 / 502 / 503 /
  504 / 408 / 429), the engine now walks `synthesizer.extra_models`
  in order before declaring the consultation failed. Same
  `chat_client` (so the same proxy + connection pool); only the
  `model` field of the payload changes per attempt. First success
  wins. Each attempt records an `llm_call` event with the actual
  model used, so post-hoc audit via `/consultants--show <sid> --raw`
  reveals which model produced the final answer. Configure with
  `claude-consultants config set-role synthesizer --add-model
  <tag>`. Active at every effort tier (not gated by the x-prefix —
  cloud flaps don't care about effort).

- **/consultants — degraded-answer composer on synthesizer
  failure** — when every model in the fallback chain fails, the
  council now writes a `summary.md` whose `final_answer` field
  surfaces the researcher's full reports + the critic's verdict
  rather than `(consultation incomplete: synthesizer error: ...)`.
  Researcher reports often run 3-5k tokens of analysis at xhigh
  effort, and the critic verdict adds another 1k of structured
  decision text — that's the most expensive work in a consultation
  and now survives the synthesizer's failure to the user. The
  banner explains it's a degraded answer (not a synthesized one)
  and points the user at `claude-consultants follow-up <THIS_SID>
  --message "compose a final answer..."` to recover cheaply (the
  next synthesizer attempt inherits research + critic warm and
  costs one more call, not a full re-run).

- **/consultants--followup — failed-session-aware parent picker**
  — the skill now defaults to the most recent session of *any*
  status (was: most recent `completed` only). When the most recent
  is `failed`, AskUserQuestion offers two paths: (1) chain off the
  failed sid (cheapest — researcher + critic threads inherit from
  disk and only the synthesizer re-runs) or (2) chain off the
  failed sid's `parent_sid` (start over from a known-good thread).
  Pairs with the engine-side fallback chain + degraded answer above
  to make recovery from a cloud flap a one-step user action.

- **/consultants--followup — dedicated sub-skill** — the new fifth
  member of the `/consultants` skill family, exposes
  `claude-consultants follow-up` directly. Previously only
  reachable via the underlying CLI or by asking Claude to dispatch
  it manually; now `/consultants--followup [<sid>] <question>` is
  a first-class skill with its own SKILL.md + activation guard +
  failed-session handling.

- **Cloud-model evaluation suite — full grading pass** — every
  label in the 2026-05-07 sweep now carries Claude-graded per-query
  + per-role grades + a verdict (PROD-READY / EVALUATED-ONLY) per
  the [`docs/benchmarks/EVALUATION.md`](docs/benchmarks/EVALUATION.md)
  rubric. Three labels are PROD-READY at single-run with
  `P:A R:A C:A S:A`: `kimi-k2.6-cloud`, `gemma4-31b-cloud`,
  `glm-5-1-cloud`. Three are EVALUATED-ONLY usable in mixes for
  specific roles where the per-role grade is A:
  `minimax-m2-7-cloud` (strong critic), `qwen3-5-397b-cloud`
  (strong planner + critic), `qwen3-5-cloud` (cheap sibling).
  Headline matrix lives at the top of [`docs/benchmarks/index.md`](docs/benchmarks/index.md).
  EVALUATION.md §3.5 was updated to clarify the grader is Claude
  reading transcripts, not the human (the original "grader is the
  human" wording contradicted the LLM-to-LLM workflow).

- **User-facing v1.1 documentation pass** — three new top-level
  user runbooks landed: [`docs/get-advice.md`](docs/get-advice.md)
  (351 lines: when to use, prereqs, the four sub-skills, model
  picking, effort tiers, tools, common workflows, troubleshooting),
  [`docs/consultants.md`](docs/consultants.md) (639 lines: the
  four-role council, x-tier multi-model fan-out semantics, service
  modes, follow-ups + chaining + failed-session recovery, the
  three-layer cloud-flap recovery story, configuration via
  `/consultants--config`, picking models with explicit benchmark
  links, troubleshooting), and [`docs/whats-new.md`](docs/whats-new.md)
  (276 lines: human-readable v1.1 highlights with the full benchmark
  verdict matrix). README.md grew from 8 to 16 slash-command rows
  with a new "Since" column flagging v1.1 additions, and a CLI
  block per skill family. Install section grew from 6 to 8
  numbered steps to cover the new bin/* PATH wrappers and the
  opt-in /consultants conda env.

#### Changed (2026-05-08)

- **ChatClient retry budget bumped from 8 attempts / ~136 s to 15
  attempts / ~905 s (~15 min)** — `DEFAULT_MAX_RETRIES` 8 → 15 and
  `DEFAULT_RETRY_MAX_DELAY_S` 30 → 90 in
  `claude_hooks/get_advice/chat_client.py`. The motivating session
  (`csl-2026-05-07-2158-7c75`, xhigh effort) burned the whole
  pre-bump budget on a 2+ minute Ollama Cloud 500 window and lost
  the synthesizer outright; with the new budget a flap of that
  shape is absorbed by the retry loop and the consultation
  completes. A 5-15 minute consultation can now tolerate up to ~15
  minutes of cloud unavailability without failing — the trade-off
  being that an actual permanent outage takes longer to surface as
  a user-visible error. Affects both `/consultants` (synthesizer
  + every other role's ChatClient) and `/get-advice` (the advisor
  itself). Override via `ChatClient(..., max_retries=N,
  retry_max_delay_s=S)` per call site if a cheaper budget is
  desirable.

#### Fixed (2026-05-08)

- **install.py — bin/* shim PATH wrappers (cross-platform)** —
  skill CLIs (`claude-consultants`, `claude-advisor`, …) invoked by
  bare name from a `/consultants--config` or `/get-advice` skill
  failed with `command not found` because Claude Code's bash
  subprocess does not include the repo's `bin/` on PATH on any
  platform. Symlinks don't fix it either: the shims resolve `REPO`
  via `dirname "$0"`, which through a symlink points at the symlink
  dir (e.g. `/usr/local/bin/..`) and the helper sourcing breaks.
  Installer now drops thin exec-wrappers in a known PATH-friendly
  location for all 11 shims (`claude-hook`, `claude-consultants`,
  `claude-advisor`, `claude-hooks-daemon`, `claude-hooks-daemon-ctl`,
  `claude-hooks-proxy`, `claude-hooks-dashboard`,
  `claude-hooks-rollup`, `caliber-grounding-proxy`, `caliber-smart`,
  `claude-hook-pgvector-mcp`):
  - **POSIX (Linux + macOS)**: `~/.local/bin/<shim>` — POSIX sh
    wrapper that `exec`s the absolute repo path.
  - **Windows**: `%LOCALAPPDATA%\claude-hooks\bin\<shim>` (POSIX sh
    for the MSYS bash that Claude Code uses) plus a `.cmd` sibling
    for native cmd / PowerShell users.
  Wrappers carry an install-time tag in their first comment line,
  so `python install.py` is fully idempotent and won't clobber a
  hand-rolled wrapper. `python install.py --uninstall` removes only
  tagged wrappers. Same root cause as the 2026-05-02 ruff PATH fix
  — once the wrappers land, every bare-name invocation from a skill
  resolves on every platform.

- **install.py — Windows User PATH auto-prepend via `reg add`** —
  for `/consultants` and `/get-advice` skills to actually resolve
  on Windows the wrapper directory needs to be on User PATH that
  Claude Code's bash subprocess inherits. Installer now prepends
  `%LOCALAPPDATA%\claude-hooks\bin` to `HKCU\Environment\PATH`
  using `reg add` (NOT `setx` — `setx` silently truncates User
  PATH to 1024 chars, which is destructive on any developer
  machine), then broadcasts `WM_SETTINGCHANGE` so new processes
  pick it up without a logoff. Defensive 16 KB ceiling on the
  resulting PATH.

## [1.0.3] — 2026-05-03

Continuation of the v1.0.2 soak: the PreCompact wrap-up surfaced two
gaps in the field (lost connection state after auto-compaction; the
post-compaction model didn't pick up the saved state file), plus a
token-cost regression from the always-on `## Now` block + wrap-up
recovery pointer. PATCH bump per the project precedent for opt-in
additions and perf fixes.

### Changed

- **Now-block + wrap-up recovery: ~90% token reduction.** Both blocks
  were stacking on every `UserPromptSubmit` (now-block ~59 tok, recovery
  pointer ~103 tok), and `additionalContext` from prior turns stays in
  the conversation history forever — so the cost compounded across the
  session (≈14k tokens after 85 turns). Two cuts:
  1. Now-block dropped its inline anchor reminder (the rule lives in
     the user's feedback memory, no need to repeat it every turn) —
     59 → ~16 tok/turn.
  2. Recovery pointer is now one-shot per wrap-up file via a `.seen`
     sidecar marker written next to the file on first surfacing —
     103 tok × every turn for 24h → ~26 tok exactly once. Also shorter:
     just heading + path instead of the full rationale.
  Combined: ~163 tok/turn (compounding) → 16 tok/turn ongoing + 26
  once. Knobs unchanged.

### Added

- **Wrap-up recovery + endpoint extraction** — two-part fix for the
  failure mode the user hit on the backup_models pod after auto-
  compaction (lost the training pod ID/IP and didn't read the saved
  state summary file):
  1. `claude_hooks/wrapup_synth.collect_endpoints()` now sweeps both
     transcript text blocks AND bash commands for URLs, IPv4/IPv6
     addresses, and pod-style hostnames (RunPod / Modal / Vast.ai /
     Lambda Labs / Paperspace). The previous synth only looked at
     `ssh` bash commands, which missed RunPod proxy URLs and any IP
     mentioned only in prose. Section 7 of the synthesised wrap-up
     is now "Connection state (re-attach targets)" with separate
     subsections for pod hostnames, ssh targets, URLs, and IPs.
  2. New `claude_hooks/wrapup_recovery.py` scans the three known
     wrap-up output dirs (`<cwd>/.wolf/`, `<cwd>/docs/wrapup/`,
     `~/.claude/wrapup-pre-compact/`) on every `UserPromptSubmit`
     and prepends a short pointer block to `additionalContext` if a
     pre-compact summary was modified within the last 24h. Survives
     the compaction boundary even when the inline context gets
     trimmed. Knobs: `hooks.wrapup_recovery.enabled` (default true),
     `hooks.wrapup_recovery.max_age_seconds` (default 86400). 16
     unit tests in `tests/test_wrapup_recovery.py`.
- **`## Now` block injection** — every `UserPromptSubmit` and
  `SessionStart` now prepends a one-line markdown block with the
  current local-TZ timestamp, IANA zone, UTC offset, and weekday.
  Reason: the assistant has no internal clock, and most of our
  internal code uses `datetime.now(timezone.utc)` (correct for
  storage but UTC leaks into user-facing output); ETAs and
  scheduled-trigger times also drifted because the model anchored
  on stale timestamps from earlier tool output. The injected line
  becomes the authoritative "now" for the turn. ~30 tokens per
  surface. New module `claude_hooks/now_block.py`; config knobs
  `system.now_block.enabled` (default true) and
  `system.now_block.timezone` (default null = host
  `/etc/localtime`). 16 unit tests in `tests/test_now_block.py`
  plus integration coverage in `tests/test_handlers.py` and
  `tests/test_dispatcher.py`.

## [1.0.2] — 2026-05-02

Soak release for the PreCompact wrap-up synth + the operational
fixes that surfaced while exercising it on solidpc and pandorum.
Per the precedent set in v1.0.1, the bump stays PATCH for low-risk
opt-in additions plus stability fixes.

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

- **Daemon stdout race in concurrent dispatches** — the daemon's
  `_run_handler` redirected the process-global `sys.stdout` to a
  per-call StringIO buffer and ran `dispatch()` to capture output.
  Because the daemon is multi-threaded (`ThreadingTCPServer`), two
  concurrent hook calls clobbered each other's redirects — one
  thread's handler output landed in the other thread's buffer.
  Symptom on the user side: a `Stop` hook receiving a
  UserPromptSubmit recall payload (`hookEventName: "UserPromptSubmit"`),
  rejected by Claude Code with "Hook returned incorrect event name:
  expected 'Stop' but got 'UserPromptSubmit'". Refactored
  `dispatcher.py` to expose `dispatch_capture(event, payload) -> dict`
  that returns the handler output directly without touching
  `sys.stdout`; the daemon now calls that. The legacy
  `dispatch(event, payload)` (stdout-write) is retained for the
  inline `run.py` single-process path. Two new regression tests in
  `tests/test_dispatcher.py` (`TestDispatchCaptureThreadSafety`)
  pin the contract — one asserts `dispatch_capture` never touches
  `sys.stdout`, the other runs UserPromptSubmit + Stop concurrently
  20× and asserts neither thread receives the other's payload.
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

[Unreleased]: https://github.com/mann1x/claude-hooks/compare/v1.0.3...HEAD
[1.0.3]: https://github.com/mann1x/claude-hooks/compare/v1.0.2...v1.0.3
[1.0.2]: https://github.com/mann1x/claude-hooks/compare/v1.0.1...v1.0.2
[1.0.1]: https://github.com/mann1x/claude-hooks/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/mann1x/claude-hooks/releases/tag/v1.0.0
