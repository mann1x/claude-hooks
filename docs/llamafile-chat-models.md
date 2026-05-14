# llamafile chat models (v1.5+)

> **Scope** — chat-completion llamafiles for HyDE, `/reflect`,
> `/consolidate`, `/get-advice`, `/consultants`, and the
> `caliber-grounding-proxy`. The v1.4 embedding integration is a
> separate runbook at [`llamafile-integration.md`](llamafile-integration.md).

## TL;DR

```bash
# 1. Register a GGUF
claude-hooks-models add gemma-local /data/models/gemma-4-e4b-q5.gguf --ctx 16384

# 2. Point HyDE / reflect / consolidate at it
#    (edit config/claude-hooks.json or re-run install.py)
#
#    hooks.user_prompt_submit.hyde_model_ref: "llamafile://gemma-local"
#    reflect.model_ref:                       "llamafile://gemma-local"
#    consolidate.model_ref:                   "llamafile://gemma-local"

# 3. Probe to verify daemon spawns it
claude-hooks-models probe gemma-local
# → OK: 'gemma-local' listening on port 38093 (mode=auto, spawned=True)
```

The same model identifier works in `/get-advice` and
`/consultants` configs — bare names go to Ollama, `llamafile://<label>`
to the daemon-supervised llamafile.

## Architecture

```
HyDE / reflect / consolidate ─┐
/get-advice ─────────────────┤  model_ref starts with llamafile://?
/consultants ────────────────┘
                              ├─→ yes: LlamafileChatClient (OpenAI /v1)
                              │       │
                              │       ├ daemon_client.chat_model_ensure(label)
                              │       │   └─ ChatModelManager spawns or returns port
                              │       └ POST http://127.0.0.1:<port>/v1/chat/completions
                              │
                              └─→ no:  OllamaChatClient (native /api/chat or /api/generate)
                                      └ POST http://localhost:11434/...
```

- **One registry**, one daemon, N llamafile processes.
- LRU eviction at `max_concurrent_loaded`.
- Per-label idle reap (default 600 s, configurable per entry).
- Per-label sticky CPU fallback on GPU spawn failure.

## Registry file

`~/.claude/llamafile-models.json`, schema v1:

```json
{
  "schema_version": 1,
  "max_concurrent_loaded": 2,
  "default_port_range": [38093, 38099],
  "default_idle_timeout_seconds": 600,
  "default_mode": "auto",
  "models": {
    "gemma-local": {
      "gguf_path": "/data/models/gemma-4-e4b-q5.gguf",
      "ctx_size": 16384,
      "port": 38093,
      "mode": "auto",
      "idle_timeout_seconds": 600,
      "notes": ""
    }
  }
}
```

The file is **host-state**, not repo-tracked config. Daemon
re-reads it whenever the mtime changes, so
`claude-hooks-models add` followed by `... probe` works without
a daemon restart.

### Label rules

`^[a-z0-9][a-z0-9._-]{0,63}$` — lowercase ASCII letters, digits,
dot, underscore, dash. Max 64 chars. Must start with a letter or
digit. URL-safe + CLI-safe.

### Port allocation

If `--port` is omitted on `add`, the registry picks the lowest free
port in `default_port_range`. Collision raises `PortCollision`; pick
explicitly or remove the conflicting label first.

## Model identifier grammar

Three forms, parsed by `parse_model_ref()`:

| Form | Routes to | Example |
|---|---|---|
| `llamafile://<label>` | daemon-supervised llamafile | `llamafile://gemma-local` |
| `<name>:cloud` | Ollama (it handles `:cloud` internally) | `kimi-k2.6:cloud` |
| `<bare>` | Ollama (local or LAN) | `gemma4-e4b` |

Mixed configs work — a consultants role can have
`extra_models: ["llamafile://big-q5", "kimi-k2.6:cloud"]` and fan
out across both backends.

## CLI: `claude-hooks-models`

| Command | What it does |
|---|---|
| `list [--json]` | Tabular registry dump. |
| `add LABEL PATH [--ctx N --port N --mode auto\|cpu --idle-timeout SEC --notes "..."]` | Register a new entry. Validates label, GGUF magic, port collision. |
| `remove LABEL [--force]` | Remove from registry + best-effort daemon shutdown. `--force` skips the RPC. |
| `rename OLD NEW` | Atomic rename. |
| `copy SRC NEW [--port N --ctx N]` | Alias same GGUF with new label/port/ctx. |
| `show LABEL [--json]` | Detail + live status (PID, idle seconds) from daemon. |
| `path` | Print registry file path. |
| `probe LABEL` | Ask daemon to bring up the model; report port + spawned/evicted state. |
| `gc` | Reap daemon-running models no longer in the registry. |

All subcommands work even when the daemon is down. The four that
talk to the daemon (`show`, `probe`, `gc`, `remove`) degrade
gracefully — `show` prints registry-only data; `probe` and `gc`
return rc=2 with a clear "daemon not reachable" message.

## Wiring config

Three precedence rules. `*_model_ref` wins when set:

```jsonc
{
  "hooks": {
    "user_prompt_submit": {
      "hyde_model_ref": "llamafile://gemma-local",      // v1.5+
      "hyde_fallback_model_ref": "llamafile://gemma-local",
      "hyde_model": "gemma4:e2b",                       // legacy
      "hyde_url": "http://localhost:11434/api/generate" // legacy
    }
  },
  "reflect": {
    "model_ref": "llamafile://gemma-local",  // v1.5+
    "ollama_model": "gemma4:e2b",            // legacy
    "ollama_url": "http://localhost:11434/api/generate"
  },
  "consolidate": {
    "model_ref": "llamafile://gemma-local",
    "ollama_model": "gemma4:e2b",
    "ollama_url": "http://localhost:11434/api/generate"
  }
}
```

`install.py` writes these for you if you accept the wiring prompt
during the llamafile dialog. To use the same model for HyDE but
different ones for the skills, set `*_model_ref` per-section.

## /get-advice + /consultants

Per-role models in `~/.claude/advisor-state.json` /
`consultants.toml` accept any of the three identifier forms.
Cold-start follow-ups (parent session reaped, follow-up resumes)
re-resolve the backend from the role's recorded model string, so
a session that was started on Ollama and continued after
reconfiguring to llamafile picks up the new backend transparently.

### Known limitation — cross-backend fan-out

`extra_models` fan-out (xmedium+ effort tiers) reuses the role's
primary ChatClient. Same-backend fan-out works (`primary=Ollama,
extras=[Ollama, Ollama]` or `primary=llamafile, extras=[llamafile,
llamafile]`). Cross-backend fan-out (`primary=Ollama,
extras=[llamafile://...]`) sends the llamafile model string to
the Ollama backend, which 404's it. Deferred to v1.5.1+.

## caliber-grounding-proxy

Point caliber at a llamafile chat model with two env vars on the
systemd unit:

```bash
# /etc/systemd/system/caliber-grounding-proxy.service.d/llamafile.conf
[Service]
Environment="CALIBER_GROUNDING_UPSTREAM=http://127.0.0.1:38094"
Environment="CALIBER_GROUNDING_UPSTREAM_BACKEND=openai_compat"
```

`openai_compat` skips the OpenAI ↔ Ollama translation entirely —
llamafile speaks OpenAI inbound + outbound. The 15-attempt HTTP /
network retry budget + 5-attempt empty-content detection still
apply.

Confirm via `/health`:

```bash
curl -s http://127.0.0.1:8765/health | jq .
# {
#   "ok": true,
#   "service": "caliber-grounding-proxy",
#   "upstream": "http://127.0.0.1:38094",
#   "upstream_backend": "openai_compat",
#   ...
# }
```

## Ops

### Eviction

```bash
$ claude-hooks-models show gemma-local --json
{
  "label": "gemma-local",
  "port": 38093,
  "_live": {
    "label": "gemma-local",
    "alive": true,
    "pid": 12345,
    "idle_seconds": 124.5
  }
}

# Force-spawn (e.g. before a known burst):
$ claude-hooks-models probe gemma-local
OK: 'gemma-local' listening on port 38093 (mode=auto, spawned=True)
     evicted to make room: ['older-model']
```

### Idle reap

Daemon's reaper thread polls every 60 s. Per-label reap fires when
`time.now() - last_activity_at >= idle_timeout_seconds`. Streaming
calls update `last_activity_at` on every chunk, so a long in-flight
generation can't be reaped mid-call.

### CPU fallback

GPU spawn failure (model larger than free VRAM, driver crash,
OOM during prefill) flips that **label** to CPU mode and
re-spawns. Sticky for the daemon's lifetime; restart the daemon
to retry GPU.

## Distribution

Unlike v1.4 (which ships a 1.5 GB composite embedding llamafile as
a GitHub Release asset), **v1.5 does not ship chat-model assets**.
Chat models range from 1 GB to 32+ GB; users supply their own
GGUFs. `install.py` fetches the
`vendor/llamafile/v0.10.1/llamafile-0.10.1` slim binary if missing
(same path the v1.4 custom-GGUF flow already uses).

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `daemon not reachable` from CLI | `claude-hooks-daemon-ctl status` and restart if needed |
| `daemon refused to bring up X: invalid_gguf` | GGUF moved; re-run `add` with the new path |
| First call hangs ~20-60 s | Cold spawn; daemon is loading the GGUF |
| 503 from llamafile mid-call | Model evicted; client auto-retries once with re-ensure |
| GPU mode never sticks | Check `idle_timeout_seconds` — long-idle hosts re-spawn cold and may hit the GPU mode test again |
