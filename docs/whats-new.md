# What's new in v1.5

> Released 2026-05-14. Previous release notes: [v1.4](whats-new-v1.4.md),
> [v1.1](whats-new-v1.1.md). Full changelog: [`CHANGELOG.md`](../CHANGELOG.md).

v1.4 closed the embedding-side gap with llamafile; **v1.5 closes the
chat-completion side**. HyDE, `/reflect`, `/consolidate`,
`/get-advice`, `/consultants`, and the `caliber-grounding-proxy` can
now talk to a local llamafile chat model the same way they talk to
Ollama — with one extra prefix on the model identifier.

## At a glance

- New model-identifier prefix `llamafile://<label>` routes a flow to
  a daemon-supervised llamafile chat model. Bare names and `:cloud`
  suffix continue to route to Ollama — no migration burden on
  existing configs.
- New host-state registry at `~/.claude/llamafile-models.json`
  (schema v1). One label → one GGUF, one port, one ctx, one mode.
- New `claude-hooks-models` CLI: `list / add / remove / rename /
  copy / show / path / probe / gc`. All subcommands work daemon-up
  or daemon-down.
- The existing `claude-hooks-daemon` supervises chat models the
  same way it supervises the v1.4 embedding llamafile: spawn-on-
  demand, LRU eviction at concurrency cap, per-label idle reap
  (default 600 s), per-label sticky CPU fallback on GPU failure.
- `caliber-grounding-proxy` can point at any OpenAI-compatible
  upstream (llamafile, vLLM, LM-Studio) via the new env var
  `CALIBER_GROUNDING_UPSTREAM_BACKEND=openai_compat`. Skips the
  Ollama translation; keeps the 15-attempt retry + 5-attempt
  empty-content budget.
- `install.py` adds an optional llamafile chat-model sub-dialog
  inside the existing chat-backend setup.
- Test count: **2558 passing** (+225 from v1.4's 2333).

## Architecture overview

```
HyDE / reflect / consolidate ─┐
/get-advice ─────────────────┤  model_ref starts with llamafile://?
/consultants ────────────────┘
                              ├── yes → LlamafileChatClient (OpenAI /v1)
                              │           │
                              │           ├ daemon_client.chat_model_ensure(label)
                              │           │   └ ChatModelManager: dict[label, ProcessHandle]
                              │           │     LRU evict at cap, per-label idle reap
                              │           └ POST /v1/chat/completions on resolved port
                              │
                              └── no  → existing OllamaChatClient (unchanged)

caliber-grounding-proxy:
   inbound OpenAI ──┬── translate to Ollama → /api/chat   (backend=ollama, default)
                    └── pass through verbatim → /v1/chat/completions  (backend=openai_compat)
```

## Walkthrough

### 1. Register a GGUF

```bash
claude-hooks-models add gemma-local \
    /data/models/gemma-4-e4b-q5.gguf \
    --ctx 16384 --mode auto
# added 'gemma-local' -> /data/models/gemma-4-e4b-q5.gguf
#   (port=38093, ctx=16384, mode=auto)
```

### 2. Wire HyDE / reflect / consolidate

Either re-run `install.py` (it offers the wiring shortcut), or edit
`config/claude-hooks.json`:

```json
{
  "hooks": {
    "user_prompt_submit": {
      "hyde_model_ref": "llamafile://gemma-local"
    }
  },
  "reflect":     { "model_ref": "llamafile://gemma-local" },
  "consolidate": { "model_ref": "llamafile://gemma-local" }
}
```

The legacy `hyde_model` / `ollama_model` keys are still honored as
fallbacks when `*_model_ref` is unset, so existing configs work
unchanged.

### 3. Verify

```bash
claude-hooks-models probe gemma-local
# OK: 'gemma-local' listening on port 38093 (mode=auto, spawned=True)
```

### 4. /get-advice + /consultants

Set `model: "llamafile://gemma-local"` in
`~/.claude/advisor-state.json` (for `/get-advice`) or in any role
of your `consultants.toml`. Mixed-backend configs work — same role
can switch between bare Ollama refs and `llamafile://` refs without
touching anything else.

### 5. caliber-grounding-proxy

Drop a systemd override and restart:

```bash
sudo systemctl edit caliber-grounding-proxy
# [Service]
# Environment="CALIBER_GROUNDING_UPSTREAM=http://127.0.0.1:38094"
# Environment="CALIBER_GROUNDING_UPSTREAM_BACKEND=openai_compat"

sudo systemctl restart caliber-grounding-proxy
curl -s http://127.0.0.1:8765/health | jq .upstream_backend
# "openai_compat"
```

## Where the system listens (updated)

| Port range | Service |
|---|---|
| 38092 | v1.4 embedding llamafile (single instance) |
| 38093–38099 | v1.5 chat llamafile (one port per registered label) |
| 47018 | claude-hooks-daemon RPC |
| 38081 | proxy dashboard |
| 11433 | api.anthropic.com proxy |
| ... | (full table in [README.md](../README.md)) |

## Migrating

Nothing to do. Existing v1.4 installs upgrade in place:

- Ollama-backed HyDE / reflect / consolidate keep working — the
  legacy config keys still take effect when `*_model_ref` is unset.
- `/get-advice` and `/consultants` configs (bare Ollama refs and
  `:cloud` suffix) keep routing to Ollama.
- The v1.4 embedding llamafile keeps running on port 38092.

Opt in by either:

1. Re-running `python install.py` and answering "yes" to the new
   "Register a llamafile chat model now?" prompt, OR
2. Running `claude-hooks-models add` and editing `*_model_ref`
   keys by hand.

## Known limitations

- **Cross-backend fan-out** — `extra_models` lists that mix Ollama
  and llamafile refs use the role's primary backend for all
  entries; same-backend fan-out works. Deferred to v1.5.1.
- **No multi-GPU placement** — llamafile picks one device per
  process. A v1.6 could add `--device <id>` per registry entry.
- **No remote llamafile** — registry is per-host. `llamafile://<host>/<label>`
  may come in a later release; v1.5 supervises locally only.
- **No automatic GGUF download** — registry takes a path, doesn't
  fetch.

See [`docs/llamafile-chat-models.md`](llamafile-chat-models.md) for
the full reference + ops runbook.
