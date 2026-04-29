# Caliber grounding proxy

A local OpenAI-compatible HTTP server that augments Ollama with project
grounding so caliber's `init` / `regenerate` output cites real
`path:line` references instead of hallucinated ones.

```
caliber ──POST /v1/chat/completions──► caliber-grounding-proxy
                                          │
                                          ├─ prepend grounding system + anchors
                                          ├─ inject tool specs (read_file, grep,
                                          │  glob, list_files)
                                          ▼
                                       Ollama
                                          │
                                          ▼
                          finish_reason == "tool_calls":
                              execute tools locally, loop
                          else:
                              mirror back to caliber
```

The proxy binds `127.0.0.1:38090` by default — local-only because
another host can't see your project files anyway. When it's running
caliber answers like a project-aware assistant; when it's down,
caliber falls back to its own LLM, untouched.

## When you want this

- You run caliber against Ollama (not Anthropic / OpenRouter directly).
- You want `caliber init` to cite real symbols in your repo instead of
  inventing them.
- You're using a tool-capable Ollama model (`gemma4-98e` and similar).

When the proxy is unreachable, you get plain-Ollama caliber output —
the only loss is the grounding, nothing breaks.

## Components

| Path | Purpose |
|---|---|
| `claude_hooks/caliber_proxy/` | Server source — `server.py`, `tools.py`, `prompt.py`, `ollama.py` |
| `bin/caliber-grounding-proxy` | POSIX shim that runs `python -m claude_hooks.caliber_proxy` |
| `bin/caliber-smart` | Drop-in `caliber` wrapper — uses the proxy when up, falls back to claude-cli when down |
| `systemd/caliber-grounding-proxy.service` | systemd unit, installed by `install.py` |

## Install

The installer prompts for it under `caliber_proxy.enabled`. Set in
`config/claude-hooks.json`:

```jsonc
"caliber_proxy": {
  "enabled": true
}
```

Then `python install.py` writes
`/etc/systemd/system/caliber-grounding-proxy.service` (Linux only) and
starts the unit. The service runs the proxy on `127.0.0.1:38090`
under `__REPO_PATH__` with the env knobs below.

Manual run (no systemd, any OS):

```bash
bin/caliber-grounding-proxy           # POSIX
bin/caliber-grounding-proxy.cmd       # Windows
# or directly:
python -m claude_hooks.caliber_proxy
```

## Verify

```bash
curl -sf http://127.0.0.1:38090/health         # → 200
curl -s http://127.0.0.1:38090/v1/models | jq  # → list of Ollama models
```

End-to-end:

```bash
OPENAI_API_KEY=ollama \
OPENAI_BASE_URL=http://127.0.0.1:38090/v1 \
caliber refresh --quiet
```

## Environment knobs

All optional; defaults shown.

| Var | Default | Purpose |
|---|---|---|
| `CALIBER_GROUNDING_HOST` | `127.0.0.1` | Bind interface — keep loopback unless you understand the trust model |
| `CALIBER_GROUNDING_PORT` | `38090` | Bind port |
| `CALIBER_GROUNDING_UPSTREAM` | `http://127.0.0.1:11434/v1` | Ollama OpenAI-compat endpoint. Override to a remote host (e.g. `http://192.168.178.2:11433/v1`) when Ollama lives elsewhere. |
| `CALIBER_GROUNDING_CWD` | `$(pwd)` | The project to ground against. **Must be set if you run the proxy from a different dir than the project** (e.g. as a systemd service). |
| `CALIBER_GROUNDING_MAX_ITER` | `35` | Cap on tool-call rounds before the proxy strips tools and forces an answer |
| `CALIBER_GROUNDING_FORCE_ANSWER_AFTER` | `5` | After this many tool rounds, drop `tools` / `tool_choice` so the model commits |
| `CALIBER_GROUNDING_MAX_TOOL_CALLS_PER_TURN` | `8` | Cap parallel tool calls in one assistant turn — guards against Gemma's 800-call looping mode |
| `CALIBER_GROUNDING_THINK` | `false` | Maps to Ollama's `think` field — accepts `false`, `true`, `low`, `medium`, `high`. Gemma4 left unconstrained burns context on overthinking; `medium` is a safe ceiling for grounding tasks |
| `CALIBER_GROUNDING_HTTP_TIMEOUT` | `1800` | Per-request timeout to Ollama (seconds) |
| `CALIBER_GROUNDING_LOG_LEVEL` | `INFO` | `DEBUG` to see prompt assembly + tool dispatch |

`bin/caliber-grounding-proxy` defaults `CALIBER_GROUNDING_UPSTREAM` to
`http://192.168.178.2:11433/v1` — that's the author's home-LAN
Ollama proxy. **Override it for your install** via the systemd
drop-in (see below) or shell environment.

### systemd drop-in for env overrides

Don't edit the shipped unit. Drop overrides at
`/etc/systemd/system/caliber-grounding-proxy.service.d/local.conf`:

```ini
[Service]
Environment=CALIBER_GROUNDING_UPSTREAM=http://127.0.0.1:11434/v1
Environment=CALIBER_GROUNDING_THINK=medium
```

Then `systemctl daemon-reload && systemctl restart
caliber-grounding-proxy`.

## `caliber-smart` — the dispatcher

`bin/caliber-smart` is a wrapper that picks the right backend at
invocation time:

```
caliber-smart <args>
   │
   ├── if Ollama health OK AND proxy /health OK
   │       export OPENAI_BASE_URL=http://127.0.0.1:38090/v1
   │       export OPENAI_API_KEY=ollama
   │       export CALIBER_MODEL=gemma4-98e:cd-q6k-256k    (overridable)
   │       exec caliber <args>
   │
   └── else
           export CALIBER_USE_CLAUDE_CLI=1
           exec caliber <args>      # fallback to claude-cli
```

Use it as a drop-in replacement for `caliber`:

```bash
caliber-smart refresh
caliber-smart init --dry-run --auto-approve --agent claude
caliber-smart learn finalize
```

Knobs (all optional, env-only — `caliber-smart` never modifies
`~/.caliber/config.json`):

| Var | Default | Purpose |
|---|---|---|
| `OLLAMA_HEALTH_URL` | `http://192.168.178.2:11433/api/tags` | Ollama liveness probe |
| `CALIBER_GROUNDING_URL` | `http://127.0.0.1:38090/v1` | Proxy endpoint |
| `CALIBER_MODEL` | `gemma4-98e:cd-q6k-256k` | Tool-capable Ollama model (see below) |
| `CALIBER_FAST_MODEL` | `gemma4-98e:cd-q6k-256k` | Same default — caliber's "fast" path uses this for the cheaper sub-tasks |
| `CALIBER_CLAUDE_CLI_TIMEOUT_MS` | `1800000` | Total budget (30 min) when falling back to claude-cli |
| `CALIBER_GENERATION_TIMEOUT_MS` | `1800000` | Per-generation budget |
| `CALIBER_STREAM_INACTIVITY_TIMEOUT_MS` | `600000` | Streaming-stall timeout (10 min) |
| `CALIBER_SMART_QUIET=1` | unset | Suppress the "using X" status line |

## Modelfile expectations

The proxy's tool-call loop assumes a tool-capable Ollama model. The
default is `gemma4-98e:cd-q6k-256k`, a custom Modelfile-built variant
of `gemma4:9b-instruct-256k` with:

- `q6k` quant (good quality at ~7 GB on disk)
- 256k context (caliber's grounding pulls a lot of project files)
- Tool-call template wired through

If you build your own Modelfile, the only hard requirement is that
the model's chat template supports `tools` / `tool_choice` and the
OpenAI tool-call format. Set `CALIBER_MODEL` to the resulting tag.

Caveats from the Gemma4 tuning notes:
- Unconstrained "thinking" mode burns the entire context on
  `"Wait, let me re-read..."` loops. Set `CALIBER_GROUNDING_THINK=false`
  or `medium` to cap it.
- Some templates emit 800+ tool-call attempts in a single assistant
  turn. The `MAX_TOOL_CALLS_PER_TURN=8` cap silently drops the rest;
  the model can request more next round.

See [`gemma4-tool-use-notes.md`](gemma4-tool-use-notes.md) for the
empirical bench results that drove these defaults.

## Health, logs, troubleshooting

```bash
# liveness
systemctl status caliber-grounding-proxy
curl http://127.0.0.1:38090/health

# logs (systemd)
journalctl -u caliber-grounding-proxy -f
journalctl -u caliber-grounding-proxy -n 200 --no-pager

# logs (manual)
CALIBER_GROUNDING_LOG_LEVEL=DEBUG bin/caliber-grounding-proxy
```

Common failure shapes:

| Symptom | Cause | Fix |
|---|---|---|
| `caliber-smart: grounding proxy down at http://127.0.0.1:38090 — using claude-cli` | Service not running | `systemctl start caliber-grounding-proxy` (or `bin/caliber-grounding-proxy &`) |
| Caliber output cites paths that don't exist | `CALIBER_GROUNDING_CWD` points at the wrong project | Override the env var in the systemd drop-in to match your project root |
| Tool calls loop forever, never produce a final answer | Model template buggy on `tool_choice` | Lower `CALIBER_GROUNDING_FORCE_ANSWER_AFTER` (e.g. to `3`) so tools get stripped sooner |
| `bind: address already in use` | Another process on `:38090` | Override `CALIBER_GROUNDING_PORT` |

## Disable

```jsonc
// config/claude-hooks.json
"caliber_proxy": {
  "enabled": false
}
```

Then `python install.py` removes the systemd unit cleanly. The shim
binaries in `bin/` stay around (they're harmless when not invoked).
