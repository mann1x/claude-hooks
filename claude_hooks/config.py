"""
Config file loading + defaults.

The config lives at ``<repo>/config/claude-hooks.json`` (gitignored). The
schema is documented in ``config/claude-hooks.example.json``.
"""

from __future__ import annotations

import inspect
import json
import os
import sys
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Optional

DEFAULT_CONFIG: dict[str, Any] = {
    "version": 2,
    "providers": {
        "qdrant": {
            "enabled": True,
            "mcp_url": "",
            "headers": {},
            "collection": "memory",
            "recall_k": 5,
            "store_mode": "auto",        # auto | off
            "timeout": 5.0,
        },
        "memory_kg": {
            "enabled": True,
            "mcp_url": "",
            "headers": {},
            "recall_k": 5,
            "store_mode": "auto",        # auto | off
            "timeout": 5.0,
        },
        # --- pgvector — Postgres-backed memory + KG ---
        # Defaults target the qwen3 schema (memories_qwen3 +
        # kg_observations_qwen3 + shared kg_entities/kg_relations) that
        # ``install.py`` creates via ``_init_pgvector_schema``. The
        # qwen3-embedding:0.6b natively supports 32k tokens, but we run
        # it at 16k to match caliber_proxy/recall.py's hardcoded 16384 —
        # mismatched num_ctx values force ollama to rebuild the KV cache
        # (or full reload) every time the daemon and proxy hit the same
        # model with different sizes. 16k is plenty for a single chunk
        # at max_chars=30000 (~7500 tokens). Users who want the simpler
        # legacy single-table setup can override ``table`` / ``embedder``
        # in ``config/claude-hooks.json``.
        "pgvector": {
            "enabled": False,
            "dsn": "",                   # postgres://user:pass@host:5432/db
            "table": "memories_qwen3",
            "additional_tables": ["kg_observations_qwen3"],
            "embedder": "ollama",        # see claude_hooks/embedders.py
            "embedder_options": {
                "url": "http://localhost:11434/api/embeddings",
                "model": "qwen3-embedding:0.6b",
                # Embedding latency is superlinear in payload size and
                # max_chars is 30000: measured CPU-only on
                # qwen3-embedding-0.6b, 5 KB ~9 s / 16 KB ~48 s /
                # 30 KB ~135 s. A 30 s ceiling hard-fails large
                # memories. Raised 30 -> 180 on 2026-07-25.
                "timeout": 180.0,
                "num_ctx": 16384,
                "max_chars": 30000,
            },
            "recall_k": 5,
            "store_mode": "auto",
            # psycopg connect_timeout. 10 s is tight when the box is
            # loaded enough to make the embedder slow in the first place.
            "timeout": 30.0,
        },
        "sqlite_vec": {
            "enabled": False,
            "db_path": "~/.claude/claude-hooks-memory.db",
            "table": "memory",
            "embedder": "ollama",
            "embedder_options": {
                "url": "http://localhost:11434/api/embeddings",
                "model": "nomic-embed-text",
            },
            "recall_k": 5,
            "store_mode": "auto",
            "timeout": 30.0,
        },
    },
    "hooks": {
        "user_prompt_submit": {
            "enabled": True,
            "min_prompt_chars": 30,
            "include_providers": ["qdrant", "memory_kg"],
            "max_total_chars": 4000,
            # --- v0.2 features ---
            "hyde_enabled": False,
            "hyde_grounded": True,
            "hyde_ground_k": 3,
            "hyde_ground_max_chars": 1500,
            "hyde_model": "gemma4:e2b",
            "hyde_fallback_model": "gemma4:e4b",
            "hyde_url": "http://localhost:11434/api/generate",
            "hyde_timeout": 30.0,
            "hyde_max_tokens": 150,
            "hyde_keep_alive": "15m",
            # Context-window for the HyDE Ollama call. Ollama keeps the
            # FIRST loader's num_ctx sticky for the duration the model
            # stays resident, so omitting this leaves the model at the
            # Modelfile default (4k for gemma4:e2b) and triggers a
            # reload+rebuild whenever a different num_ctx hits the same
            # model. 16k matches reflect.num_ctx and consolidate.num_ctx
            # so all three gemma4:e2b callers share one resident
            # instance — set them in lockstep when overriding.
            "hyde_num_ctx": 16384,
            "progressive": False,
            "decay_enabled": False,
            "decay_file": "~/.claude/claude-hooks-decay.json",
            "decay_recency_halflife_days": 14,
            "decay_frequency_cap": 5,
            # Port 2 from thedotmack/claude-mem: filter candidates by
            # metadata before vector rerank. Default off (back-compat).
            "metadata_filter": {
                "enabled": False,
                # Over-fetch ratio so the filter has a bigger pool to
                # survive from. `recall_k * over_fetch_factor` candidates
                # per provider; survivors capped back to recall_k after
                # filtering. 4 is a sane default — raise when filters
                # are strict (cwd + type + age).
                "over_fetch_factor": 4,
                "require_cwd_match": False,
                "require_observation_type": None,
                "max_age_days": None,
                "require_tags": [],
            },
        },
        "session_start": {
            "enabled": True,
            "show_status_line": True,
            "compact_recall": True,
            "compact_recall_query": "session context, key decisions, and important patterns",
        },
        "stop": {
            "enabled": True,
            "store_threshold": "noteworthy",  # noteworthy | always | off
            "classify_observations": True,
            "extract_instincts": False,
            "instincts_dir": "~/.claude/instincts",
            # "markdown" (default, back-compat) or "xml" — the XML format
            # is the structured <observation> layout ported from
            # thedotmack/claude-mem. Every field (type/title/subtitle/
            # files_modified/...) is addressable, which helps downstream
            # recall filtering. Not default yet because existing Qdrant
            # corpora were written in markdown and search would mix them.
            "summary_format": "markdown",
            # Tier 1.3: when true, spawn a detached subprocess for the
            # dedup-and-store fan-out instead of running it inline. Stop
            # returns its systemMessage immediately and the user sees
            # ~200-500 ms less latency per noteworthy turn. Trade-off:
            # store failures are logged but not surfaced in the message
            # (the parent has already returned by then), so providers
            # cannot block the hook even on hard error. Once the daemon
            # (Tier 3.8) ships this flag will be replaced by the daemon
            # path.
            #
            # ON by default since 2026-07-25. Inline is not merely
            # slower, it is incorrect on a local embedder: store()
            # issues TWO embeds (dedup recall + content) and CPU-only
            # embedding is superlinear in payload size (~9 s per 5 KB),
            # so a 5 KB memory needs ~19 s and the Stop hook's own cap
            # SIGTERMs it first -- the memory is silently lost and the
            # session reports the embedder as down. Detaching takes the
            # embed off the hook's critical path entirely. Falls back to
            # inline if the spawn fails (see hooks/stop.py).
            "detach_store": True,
            # Layer 2 of the store gate (see claude_hooks/embedder_gate.py).
            # store_lock serialises stores within ONE host; this waits for
            # a *shared* embedder to go idle before a background store
            # adds load, so several machines pointed at one llamafile
            # don't pile on. Keyed on the embedder rather than the memory
            # backend so it works for pgvector / sqlite_vec / qdrant /
            # memory_kg alike.
            #
            # OFF by default: it only helps when multiple hosts share an
            # embedder, and it costs a probe per store. Advisory
            # backpressure, never mutual exclusion — a busy server
            # sometimes answers fast between micro-batches, and two hosts
            # can both read "idle" at once.
            "embedder_gate": {
                "enabled": False,
                # Give up waiting and store anyway after this long: a
                # delayed store is fine, a dropped one is not.
                "max_wait_s": 60.0,
                # /slots is answered from the inference loop, so a slow
                # reply IS the busy signal. Keep this short.
                "probe_timeout_s": 1.0,
                # Each probe enqueues a task on the server; poll coarsely.
                "poll_interval_s": 2.0,
            },
        },
        "stop_guard": {
            # Disabled by default: the default patterns are opinionated
            # ("nothing is pre-existing", "sessions are unlimited"). Enable
            # only if you want that behaviour. Patterns are user-overridable.
            "enabled": False,
            # Empty list = use claude_hooks.stop_guard.DEFAULT_PATTERNS.
            # Provide [{"pattern": "regex", "correction": "msg"}] to override.
            "patterns": [],
            # Meta-context escape: skip the check when the match is only
            # inside quoted spans OR the message contains a meta-marker
            # phrase ("trigger phrase", "stop_guard", ...). Prevents false
            # positives when the assistant is documenting / testing / quoting
            # the guard's rules. Turn off to get the raw regex behaviour.
            "skip_meta_context": True,
            # Empty list = use claude_hooks.stop_guard.DEFAULT_META_MARKERS.
            # Provide ["marker phrase", ...] to override.
            "meta_markers": [],
            # User-wrap-up escape (most important): if the LAST USER
            # message contains a wrap-up marker ("compact the context",
            # "wrap up", "save state", "/wrapup", ...), the guard is
            # bypassed entirely. Ensures the assistant can comply with
            # explicit user instructions to end/summarise the session
            # without being blocked.
            "skip_on_user_wrap_up": True,
            # Empty list = use claude_hooks.stop_guard.DEFAULT_USER_WRAP_UP_MARKERS.
            "user_wrap_up_markers": [],
            # Stall-after-commitment check: catches the failure mode
            # where the model writes a paragraph ending with an
            # action-commitment phrase ("Diving in now", "Writing the
            # script now") and then ends the turn WITHOUT calling any
            # tool. Stacks three independent conditions
            # (stop_reason=end_turn + zero tool_use + commitment phrase
            # in last paragraph) so false-positive risk is low. Default
            # on when stop_guard itself is enabled — set to false to
            # suppress just this sub-check while keeping the prose
            # pattern guard active.
            "stall_check_enabled": True,
            # Trailing-question escape: when the assistant's turn ends
            # with a genuine question ("Should I proceed with X, or Y?"),
            # suppress BOTH the stall-after-commitment check AND the
            # permission-seeking prose patterns. A genuine question must
            # not be force-overridden into "just continue" — waiting for
            # the user is the safer choice and honours "confirm before
            # destruction". Ownership-dodging / session-quitting nudges
            # are unaffected (they still fire even when phrased as a
            # question). Set false to restore the always-push behaviour.
            "suppress_on_trailing_question": True,
        },
        "session_end": {
            "enabled": True,
        },
        "pre_compact": {
            # Auto-synthesise a /wrapup-shaped summary just before
            # Claude Code auto-compacts the conversation. The summary
            # is written to disk (preferring .wolf/, then docs/wrapup/,
            # falling back to ~/.claude/wrapup-pre-compact/) and emitted
            # as additionalContext so it lands inside the compaction
            # window. Mechanically-extractable sections (commits, files
            # modified, plans referenced, ssh hosts, monitorings) are
            # filled in deterministically; sections needing model
            # judgment (open items, next items, narrative) are explicitly
            # marked so the next session knows to invoke /wrapup to fill
            # in the gaps.
            #
            # Activates only when BOTH:
            #   1. enabled is true (this flag, default true)
            #   2. ~/.claude/skills/wrapup/SKILL.md exists
            #      (override path via wrapup_skill_path)
            "enabled": True,
            "save_to_file": True,
            "wrapup_skill_path": None,  # null = default ~/.claude/skills/wrapup/SKILL.md
            # Cap (in MB) of the trailing transcript read during synthesis.
            # Long-lived sessions grow enormous .jsonl transcripts (the
            # backup_models case hit 581 MB); reading the whole file under
            # load blew past the 20 s PreCompact timeout so the hook was
            # killed before the wrap-up was written — and the post-compact
            # recovery had nothing to surface. The transcript is
            # append-only, so a bounded tail still covers the recent
            # working window. Set 0 to read the whole transcript.
            "max_transcript_mb": 24,
        },
        "wrapup_recovery": {
            # On every UserPromptSubmit, scan the wrap-up output dirs
            # (.wolf/, docs/wrapup/, ~/.claude/wrapup-pre-compact/) for
            # any pre-compact summary modified within max_age_seconds.
            # If one is found, prepend a pointer block to additionalContext
            # so the post-compaction assistant can find and read the
            # saved state summary even when the inline summary gets
            # trimmed across the compaction boundary.
            "enabled": True,
            "max_age_seconds": 86400,
        },
        "daemon": {
            # Long-lived hook executor (Tier 3.8). When the daemon is
            # running, bin/claude-hook sends events to it over an
            # HMAC-authenticated TCP localhost socket and skips the
            # ~150-300 ms Python interpreter spawn that the inline
            # path pays.
            #
            # ``enabled`` controls whether install.py should *prompt*
            # to install the systemd / launchd / Windows scheduled-task
            # autostart at install time. The runtime behaviour is
            # daemon-or-fallback regardless: if the daemon is up, the
            # client uses it; if not, the client falls back silently
            # to in-process dispatch.
            "enabled": True,
            # Override the bind host / port if you run multiple
            # daemons on one box (rare). Default 127.0.0.1:47018.
            "host": "127.0.0.1",
            "port": 47018,
            # Replay protection window (seconds). Requests older than
            # this are rejected even with a valid HMAC, bounding the
            # forgery surface on a leaked secret.
            "replay_window_seconds": 60,
        },
        "code_graph": {
            # Lightweight code-structure graph (modules, classes,
            # functions, imports, calls) written to
            # ``<project>/graphify-out/{graph.json, GRAPH_REPORT.md}``
            # so it co-exists with graphify (https://github.com/safishamsi/graphify).
            #
            # SessionStart injects a truncated GRAPH_REPORT.md as
            # additionalContext (one-shot, never PreToolUse) and may
            # spawn a detached rebuild if the graph is stale.
            "enabled": True,
            # Skip the build entirely when the project has fewer than
            # this many source files — too small for the orientation
            # value to outweigh the build cost.
            "min_source_files": 5,
            # Cap stale-scan + builder walks so monorepos can't freeze
            # SessionStart.
            "max_files_to_scan": 2000,
            # Cooldown: don't rebuild within N minutes regardless of
            # source churn (mirrors claudemem_reindex).
            "staleness_minutes": 10,
            # Lock guard: don't spawn another build if one started this
            # recently.
            "lock_min_age_seconds": 60,
            # Cap the injected report at this many chars. ~4k = ~1k tokens.
            "max_inject_chars": 4000,
            # Trigger a detached rebuild on SessionStart if stale.
            "rebuild_on_session_start": True,
            # Trigger a detached rebuild on Stop when the turn ran
            # Edit/Write/MultiEdit. Lock + cooldown prevent thrash when
            # many edits land in rapid succession.
            "rebuild_on_stop": True,
        },
        "lsp_engine": {
            # Opt-in integration with the bundled ``claude_hooks.lsp_engine``
            # daemon (v1.9+). When enabled, SessionStart calls
            # ``connect_or_spawn`` to bring a per-project LSP daemon up;
            # PostToolUse fires ``did_change`` + ``diagnostics`` against
            # edited files and merges the results into the same
            # ``additionalContext`` ruff writes. SessionEnd detaches so
            # the daemon's refcount drops cleanly.
            #
            # Off by default — install.py's LSP-engine dialog flips this
            # to ``true`` on confirmation. Requires at least one language
            # server (pyright / gopls / rust-analyzer / clangd / etc) on
            # ``$PATH`` and a ``cclsp.json`` mapping extensions to LSP
            # commands. install.py can drop a starter cclsp.json based
            # on detected language servers.
            "enabled": False,
            # Whether SessionStart should pre-spawn the daemon. When
            # ``false``, the daemon spawns lazily on the first
            # PostToolUse query (slower but correct).
            "spawn_on_session_start": True,
            # Whether SessionEnd should detach to release the daemon's
            # session refcount. Affinity locks for files this session
            # owned release immediately on detach.
            "detach_on_session_end": True,
            # Override the cclsp.json lookup chain. ``null`` =
            # ``$CCLSP_CONFIG_PATH`` → ``~/.config/cclsp/cclsp.json`` →
            # ``<project_root>/cclsp.json`` (the engine's default).
            "cclsp_config_path": None,
            # Override the daemon state directory.
            # ``null`` = ``~/.claude/lsp-engine``.
            "state_base": None,
            # Cap on ``connect_or_spawn`` — how long to wait for the
            # daemon socket to appear after fork-and-exec.
            "spawn_timeout_s": 5.0,
            # Per-file affinity-lock timeout (``lock_timeout_ms`` passed
            # to the engine). If another session holds the lock for
            # this file, our diagnostics query blocks this long before
            # serving the owner's view as a stale fallback.
            "diagnostics_timeout_ms": 500,
            # Server-side timeout for the diagnostics RPC itself
            # (``diag_timeout_s``). Bounds how long pyright / gopls /
            # etc has to respond before we surface what's cached.
            "diagnostics_wait_s": 2.0,
            # Skip diagnostics for these file extensions even if
            # cclsp.json claims them. Each entry is the extension
            # with no leading dot ("toml", "md", ...).
            "extensions_blacklist": [],
            # Truncate diagnostics arrays at this length per file —
            # noisy auto-generated code can otherwise flood the
            # additionalContext block.
            "max_diagnostics_per_file": 50,
            # Where the engine writes daemon-side logs (``log_path``
            # passed to ``connect_or_spawn``). Distinct from
            # ``~/.claude/claude-hooks.log`` so engine churn doesn't
            # swamp the hook log.
            "log_path": "~/.claude/claude-hooks-lsp-engine.log",
        },
        "companions": {
            # Coordinator for heavier-weight code-graph engines that
            # claude-hooks integrates when present:
            #   - axon     (https://github.com/harshkedia177/axon)
            #     RECOMMENDED for Python/JS/TS repos. Pure-Python install
            #     (`pip install axoniq`), KuzuDB-backed, dead-code
            #     detection, watcher mode.
            #   - gitnexus (https://github.com/abhigyanpatwari/GitNexus)
            #     14 languages + multi-repo group_* queries. Pick when
            #     you need languages outside Python/JS/TS.
            # When either is detected (binary + .axon/ or .gitnexus/
            # in the repo), the SessionStart hook appends a hint about
            # its mcp__*__* tools, and the Stop hook spawns its
            # reindexer when the turn modified files. Silent no-op
            # when neither is installed.
            "enabled": True,
            "reindex_on_stop": True,
            "lock_min_age_seconds": 60,
            # Optional shared-host axon daemon. When enabled, install.py
            # writes /etc/systemd/system/axon-host.service running
            # `axon host --port 8420 --bind 127.0.0.1 --no-open --no-watch`,
            # and the user can drop the legacy `axon serve --watch`
            # per-session stdio MCP from ~/.claude.json in favour of:
            #     {"axon": {"type":"http","url":"http://127.0.0.1:8420/mcp"}}
            # Recommended on multi-project hosts because the legacy
            # form auto-indexes whatever cwd Claude Code launched in -
            # it ate 64 GB of RAM on a model directory on 2026-04-27.
            # Linux-only (systemd). Off by default; opt-in.
            "axon_host": {
                "enabled": False,
            },
        },
        "claudemem_reindex": {
            # Auto-reindex the claudemem semantic index when the project
            # has been modified. Runs detached (no per-turn latency) and
            # silently no-ops if the claudemem binary is missing or the
            # project has no .claudemem directory. Complements claudemem's
            # own post-commit git hook (`claudemem hooks install`) by
            # covering mid-session edits and out-of-Claude-Code changes.
            "enabled": True,
            # Stop-event: reindex if any Edit/Write/MultiEdit ran this turn.
            "check_on_stop": True,
            # SessionStart: if the index is older than staleness_minutes AND
            # any source file is newer than the index, reindex. The
            # staleness window is a cooldown — reindex at most every N min.
            "check_on_session_start": True,
            "staleness_minutes": 10,
            # Max files walked per stale-check before bailing. Large
            # monorepos should bump this; pathological sizes should set it
            # low to keep SessionStart responsive.
            "max_files_to_scan": 2000,
            # Extra directory names to skip during the stale-scan walk.
            # Appended to the built-in default ignore set (.git,
            # .claudemem, .caliber, .wolf, node_modules, __pycache__,
            # .venv, .mypy_cache, .pytest_cache, .ruff_cache, dist,
            # build, target, out, etc.)
            "ignored_dirs": [],
            # Minimum age of the reindex lock before a new reindex may
            # run. Prevents spawn pileup when many Edit tools fire rapidly.
            "lock_min_age_seconds": 60,
        },
        "pre_tool_use": {
            # Memory-warn stage (advisory additionalContext from provider recall).
            "enabled": False,
            "warn_on_tools": ["Bash", "Edit", "Write"],
            "warn_on_patterns": ["rm ", "DROP TABLE", "git reset --hard"],
            # Port 5 from thedotmack/claude-mem. When true, Read / Edit /
            # MultiEdit on a file with prior memories always emit
            # additionalContext (bypassing warn_on_patterns). Advisory
            # only — never blocks the tool call.
            "file_read_gate": False,
            "file_read_gate_tools": ["Read", "Edit", "MultiEdit"],
            # Safety-scan stage (content-based pattern match, independent of
            # the memory-warn stage above). Emits permissionDecision:"ask"
            # for dangerous Bash commands even when chained/piped. See
            # claude_hooks/safety_patterns.py for the default list.
            "safety_scan_enabled": False,
            "safety_use_defaults": True,
            "safety_extra_patterns": [],  # [{"pattern", "name", "reason"}]
            "safety_log_enabled": True,
            "safety_log_dir": "~/.claude/permission-scanner",
            "safety_log_retention_days": 90,
            # rtk rewriter: transparently rewrite verbose find/grep/git log
            # commands to rtk equivalents for token savings. Requires the
            # external rtk binary (https://github.com/rtk-ai/rtk, NOT the
            # Rust Type Kit crate of the same name). If rtk is missing or
            # too old, the hook silently passes the command through.
            "rtk_rewrite_enabled": False,
            "rtk_min_version": "0.23.0",
            "rtk_timeout": 3.0,
            "rtk_log_rewrites": False,
            # When rtk_rewrite produces a rewrite, the hook emits
            # permissionDecision (allow/ask) which bypasses the user's
            # settings.json allow-list. To preserve the safety net,
            # safety_scan patterns are checked on rewritten commands
            # REGARDLESS of safety_scan_enabled. Set to false to opt out
            # and accept that rtk rewrites auto-approve unconditionally.
            "rtk_scan_rewrites": True,
            # code_graph symbol lookup on Grep. When enabled, a Grep
            # whose pattern looks like an identifier triggers a single
            # dict lookup against the pre-built graph and injects a
            # one-line "X is at file:line, N callers" hint. Cheap when
            # the pattern is regex-shaped or stopword-y (early reject);
            # silent when the symbol isn't in the graph or has > N hits.
            # Off by default — opt in after building graphify-out/.
            "code_graph_lookup_enabled": False,
            # Wall-clock budget for the lookup stage. Discard the result
            # silently rather than ever blocking the tool call.
            "code_graph_lookup_budget_ms": 50,
            # Max hits to render. Above this threshold we return nothing
            # — the grep is the right tool for that case.
            "code_graph_lookup_max_hits": 5,
        },
        # /consultants engine — multi-agent council. The /consultants
        # config-of-truth (per-role models, effort, topology) lives in
        # ``~/.claude/consultants-config.toml`` (see
        # ``consultants/config.py``). This block only carries the
        # flags that the daemon and CLI need to know about: which
        # service mode is active and where to find the engine.
        "consultants": {
            # Engine endpoint when running in always-on mode (the
            # default). The CLI hits this directly.
            "engine_url": "http://127.0.0.1:38095",
            "smart_start": {
                # When enabled, the daemon spawns the engine as a
                # child process on first request and reaps it after
                # idle_timeout_seconds. The CLI routes through the
                # daemon's forwarder instead of the engine directly.
                "enabled": False,
                # Reap the engine after this many seconds of idle.
                "idle_timeout_seconds": 1800,
                # Max time to wait for the engine subprocess to come
                # up after spawn (waiting for /v1/health to return 200).
                "spawn_timeout_seconds": 15,
                # Reaper poll interval — the daemon's idle thread
                # checks last_activity_at on this cadence.
                "reaper_interval_seconds": 60,
                # Daemon's forwarder URL when smart-start is on.
                # Distinct port so always-on and smart-start can
                # coexist on the same host without colliding.
                "forwarder_url": "http://127.0.0.1:38096",
            },
        },
    },
    "reflect": {
        "enabled": True,
        "max_memories_to_analyze": 50,
        "min_pattern_count": 3,
        "output_path": "~/.claude/CLAUDE.md",
        "ollama_model": "gemma4:e2b",
        "ollama_url": "http://localhost:11434/api/generate",
        # Match user_prompt_submit.hyde_num_ctx and consolidate.num_ctx —
        # all three load the same gemma4:e2b model, and a value mismatch
        # forces Ollama to rebuild the KV cache on every flip.
        "num_ctx": 16384,
    },
    "consolidate": {
        "enabled": False,
        "trigger": "manual",              # manual | session_start
        "min_sessions_between_runs": 10,
        "state_file": "~/.claude/claude-hooks-consolidate.json",
        "max_memories_to_scan": 200,
        "merge_similarity_threshold": 0.80,
        "prune_stale_days": 90,
        "ollama_model": "gemma4:e2b",
        "ollama_url": "http://localhost:11434/api/generate",
        # Keep in lockstep with hyde_num_ctx and reflect.num_ctx (same
        # model on the same Ollama instance — mismatched values thrash
        # the resident model).
        "num_ctx": 16384,
    },
    "episodic": {
        "mode": "off",                  # off | server | client
        "server_url": "",               # client: URL of the episodic-server
        "server_host": "0.0.0.0",       # server: bind address
        "server_port": 11435,           # server: port to listen on
        "binary": "episodic-memory",    # server: path to episodic-memory binary
        "timeout": 10.0,               # client: push timeout in seconds
    },
    "proxy": {
        # Optional local HTTP proxy sitting in front of api.anthropic.com.
        # Fixes the blind spots hooks can't reach: real weekly-limit %,
        # Warmup detection, rate-limit-header capture, synthetic-RL detection.
        # Default OFF — the proxy must be a deliberate opt-in. To activate:
        #   1. set enabled=true here
        #   2. run ``python -m claude_hooks.proxy`` (or the bin/claude-hooks-proxy
        #      shim) as a long-running process
        #   3. set ANTHROPIC_BASE_URL=http://127.0.0.1:<listen_port> in
        #      ~/.claude/settings.json under "env"
        # See docs/PLAN-proxy-hook.md for design + phased roadmap.
        "enabled": False,
        "listen_host": "127.0.0.1",
        "listen_port": 38080,
        "upstream": "https://api.anthropic.com",
        "timeout": 120.0,
        "log_requests": True,
        "log_dir": "~/.claude/claude-hooks-proxy",
        "log_retention_days": 14,
        # P1 (not yet): capture rate-limit headers into a rolling file that
        # scripts/weekly_token_usage.py can read to fill --current-usage-pct.
        "record_rate_limit_headers": True,
        # P3 (not yet): short-circuit Warmup requests. Leave false in P0.
        "block_warmup": False,
    },
    "update_check": {
        # Periodic GitHub release poll. Runs on the long-lived
        # ``claude-hooks-daemon`` thread so the Stop hook never blocks
        # on network I/O. Disable at runtime by flipping ``enabled`` to
        # false — both the daemon poll and the Stop-hook notice stop
        # immediately, no restart needed.
        "enabled": False,                     # opted in by install.py
        "interval_seconds": 86400,            # 24h between attempts
        "retry_pause_seconds": 300,           # 5min between retries
        "max_retries": 5,                     # then defer to next 24h window
        "github_repo": "mann1x/claude-hooks", # owner/name on github.com
        "timeout_seconds": 5,                 # network timeout per request
        "max_notifications": 10,              # Stop-hook notice budget
    },
    "system": {
        # "## Now" markdown block prepended to UserPromptSubmit and
        # SessionStart additionalContext. Costs ~30 tokens per turn
        # and gives the model a fresh, local-TZ timestamp to anchor
        # ETAs and scheduled-trigger reasoning on — without it the
        # model often defaults to UTC (because most internal code
        # uses datetime.now(timezone.utc)) or to stale timestamps
        # from earlier tool output.
        "now_block": {
            "enabled": True,
            # IANA zone name override. null = use the host's
            # /etc/localtime. Set this only if the daemon runs on a
            # host whose system TZ differs from where the user
            # actually is.
            "timezone": None,
        },
    },
    "logging": {
        "path": "~/.claude/claude-hooks.log",
        "level": "info",
        "max_bytes": 2097152,   # 2 MB per file
        "backup_count": 3,      # keep 3 rotated files (.log.1, .log.2, .log.3)
    },
    "disable_marker_filename": ".claude-hooks-disable",
}


def repo_root() -> Path:
    """Path to the claude-hooks repo root (one level above this file's package)."""
    return Path(__file__).resolve().parent.parent


def default_config_path() -> Path:
    return repo_root() / "config" / "claude-hooks.json"


def load_config(path: Optional[Path] = None) -> dict:
    """
    Load the config file from disk and merge it on top of DEFAULT_CONFIG.
    Missing file → returns the defaults unchanged.
    """
    cfg_path = path or default_config_path()
    merged: dict[str, Any] = deepcopy(DEFAULT_CONFIG)
    if not cfg_path.exists():
        return merged
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            user_cfg = json.load(f) or {}
    except (json.JSONDecodeError, OSError):
        return merged
    _deep_merge(merged, user_cfg)
    return merged


CONFIG_WRITE_AUDIT_LOG = Path(
    os.path.expanduser("~/.claude/claude-hooks-config-writes.log"),
)


def _audit_log_config_write(cfg_path: Path, size: int) -> None:
    """Append a single audit line to ``CONFIG_WRITE_AUDIT_LOG``.

    Records every successful :func:`save_config` so we can identify the
    last sanctioned write when the live config goes missing
    (the pandorum 2026-05-22 incident: ``claude-hooks.json`` vanished
    between the v1.10.0 deploy and the next session; no orphan
    ``.tmp`` was found, so the partial-write theory was ruled out, but
    we had no positive evidence of who *did* write it last).

    Format: one tab-delimited line per write::

        2026-05-22T16:12:03+0200\\tpid=...\\tsize=8096\\tpath=...\\tcaller=install.py:7600:_main\\targv0=python install.py

    **Never logs config content** — DSNs in this file carry the
    pgvector password (``postgresql://claude:Xct29ER...``); the audit
    log lives under ``~/.claude/`` which is more loosely protected
    than ``config/``. Metadata only.

    Soft-fail: any error in this helper (missing dir, permission
    denied, full disk, ...) is swallowed. The real save has already
    completed by the time this runs — a broken audit log must never
    surface as a broken ``save_config``.
    """
    try:
        # Caller frame: stack[0] = this fn, stack[1] = save_config,
        # stack[2] = the actual caller we want to record.
        stack = inspect.stack()
        if len(stack) > 2:
            f = stack[2]
            caller = f"{Path(f.filename).name}:{f.lineno}:{f.function}"
        else:
            caller = "<unknown>"

        CONFIG_WRITE_AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)

        # Local time matches the user's mental model when correlating
        # against shell history / file timestamps on the same box.
        ts = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())
        pid = os.getpid()
        # First 3 argv tokens; usually "python install.py [flag]". More
        # than enough to identify the entry point without bloating the
        # line for long bench invocations.
        argv0 = " ".join(sys.argv[:3]) if sys.argv else ""
        line = (
            f"{ts}\tpid={pid}\tsize={size}"
            f"\tpath={cfg_path}\tcaller={caller}\targv0={argv0}\n"
        )

        # Append-only — never truncate, no rotation. install.py calls
        # save_config ~5-10× per deploy; growth is sub-kB/run.
        with open(CONFIG_WRITE_AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        # Swallow EVERYTHING. The actual save already succeeded; we
        # are not allowed to break it here.
        pass


def save_config(cfg: dict, path: Optional[Path] = None) -> Path:
    """Atomically write the config to disk and return the path.

    Records a one-line audit entry to ``~/.claude/claude-hooks-
    config-writes.log`` after the atomic ``os.replace`` succeeds. See
    :func:`_audit_log_config_write` for the rationale (pandorum
    2026-05-22 disappearance) and format. The audit line never
    contains config content — pgvector DSN passwords live in the
    config and would leak otherwise; metadata only.
    """
    cfg_path = path or default_config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg_path.with_suffix(cfg_path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, cfg_path)

    # v1.10.2: write-audit log. Soft-fail — never propagates errors.
    try:
        size = cfg_path.stat().st_size
    except OSError:
        size = -1
    _audit_log_config_write(cfg_path, size)

    return cfg_path


def _deep_merge(dst: dict, src: dict) -> None:
    """Recursively merge ``src`` into ``dst`` in place."""
    for key, val in src.items():
        if key in dst and isinstance(dst[key], dict) and isinstance(val, dict):
            _deep_merge(dst[key], val)
        else:
            dst[key] = val


def expand_user_path(p: str) -> Path:
    """Expand ``~`` and environment variables in a path string."""
    return Path(os.path.expanduser(os.path.expandvars(p)))


def project_disabled(cwd: str, marker_filename: str) -> bool:
    """
    Check whether ``cwd`` (or any ancestor up to root) contains the disable
    marker file. Walks up so that ``project/sub/dir`` inherits ``project``'s
    opt-out.
    """
    p = Path(cwd or ".").resolve()
    while True:
        if (p / marker_filename).exists():
            return True
        if p.parent == p:
            return False
        p = p.parent
