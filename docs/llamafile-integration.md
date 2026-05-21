# llamafile embedding integration (v1.4)

> **Status:** shipped in v1.4.0 (2026-05-14). Opt-in: existing
> installs keep their Ollama-only embedder until `install.py` is
> re-run.
>
> **Scope** — this doc covers the **embedding-side** integration only.
> The chat-completion side (HyDE / reflect / consolidate / get-advice
> / consultants / caliber-grounding-proxy) is a separate subsystem
> shipped in v1.5; see
> [`docs/llamafile-chat-models.md`](llamafile-chat-models.md) for
> that runbook.

claude-hooks v1.4 adds **mozilla-ai/llamafile@0.10.1** as a
fallback-capable embedding engine for the `pgvector` and
`sqlite_vec` providers. A healthy install can now survive an
Ollama outage; a fresh install can run without an Ollama
dependency at all. Parity vs the Ollama `qwen3-embedding:0.6b`
blob was validated at 1024-dim with mean cosine 0.99963 across
23 mixed prompts — drop-in replacement. See
[`docs/llamafile-embedding-parity.md`](llamafile-embedding-parity.md)
for the bench detail.

## Architecture at a glance

```
pgvector / sqlite_vec provider
   └─ embedder = CompositeEmbedder(
                   primary  = OllamaEmbedder,
                   fallback = LlamafileEmbedder)
                          │ embed("foo")
                          ▼
                 LlamafileEmbedder.embed("foo")
                   1. RPC → claude-hooks-daemon "_embedding_ensure"
                   2. daemon's EmbeddingManager spawns llamafile
                      (lazy, idle-reaped after 5 min) or returns
                      "already up on :38092"
                   3. embedder POSTs to http://127.0.0.1:38092/embedding
                   4. on HTTP error → re-ensure, retry once
                          │
                          ▼
                 claude-hooks-daemon (existing TCP RPC :47018)
                   ├─ existing hook-dispatch threads (unchanged)
                   └─ NEW: EmbeddingManager  (one per daemon)
                          ├─ subprocess.Popen of
                          │     qwen3-embedding-0.6b-16k.llamafile
                          │     --port 38092 (and -ngl 99 if GPU-auto)
                          ├─ last_activity_at touched on each ensure
                          ├─ reaper thread (60s tick, 300s idle)
                          │   → SIGTERM, 10s grace, SIGKILL
                          └─ next ensure_running re-spawns transparently
```

No new systemd unit, no new Windows scheduled task — the daemon
is already installed by `install.py` on both platforms and gains
one well-bounded responsibility.

## Components

### `LlamafileEmbedder` (`claude_hooks/embedders.py`)

POSTs `{"content": text}` to `/embedding` and unwraps the three
response shapes llama.cpp can emit (flat `{"embedding":[...]}`,
list-wrapped, double-nested). On first connection failure it
calls back to the daemon's `_embedding_ensure` RPC, then retries
once. Raises `EmbedderError` on any persistent failure so
`CompositeEmbedder` can drop down to its fallback.

### `CompositeEmbedder` (`claude_hooks/embedders.py`)

Primary → fallback on `EmbedderError`. Dim mismatch between
primary and fallback is a config error (logged + raised) so the
vector space stays stable across failover — store_mode would
otherwise mix incompatible vectors silently.

### `EmbeddingManager` (`claude_hooks/embedding_manager.py`)

Lifecycle of the bundled llamafile binary inside the daemon
process. Adapted from `consultants_forwarder.EngineManager`:
same `start_new_session=True`, same SIGTERM → 10 s → SIGKILL
ladder, same `threading.Lock`-guarded `last_activity_at`.
Differences:

- **Idle threshold 300 s** (5 min) — matches Ollama's
  `OLLAMA_KEEP_ALIVE=5m`.
- **Health probe** `GET /health` (llama.cpp's endpoint).
- **GPU-fallback**: in `mode=auto`, spawn with `-ngl 99`; on
  spawn / health failure reap, respawn with `--gpu disable`,
  mark `gpu_offload_failed=True` so the rest of the session
  stays on CPU.
- **APE shim** on POSIX: the upstream binary is Cosmopolitan-Libc
  (`MZqFpD=` magic); the kernel rejects direct exec without
  binfmt_misc registration, so `EmbeddingManager` prepends
  `/bin/sh` to invoke the embedded shell bootstrap. Windows
  direct-exec (PE) is unaffected.

### Daemon RPC ops (`claude_hooks/daemon.py`)

Three new HMAC-signed ops:

| Op | Returns | Used by |
|----|---------|---------|
| `_embedding_ensure` | `{port:38092, ready:true, mode:"gpu"\|"cpu"}` | `LlamafileEmbedder` cold-start |
| `_embedding_status` | `{pid, mode, idle_seconds, last_activity_at}` | dashboards / debug |
| `_embedding_shutdown` | `{stopped:true}` | `install.py --uninstall`, daemon graceful-stop |

Typed wrappers live in `claude_hooks/daemon_client.py`. They are
**best-effort**: a missing daemon returns `None`, an `ok:false`
response returns `{"available": false}`.

## Distribution

The composite `qwen3-embedding-0.6b-16k.llamafile` (~1.5 GB) is
**not** committed and **not** rebuilt at install time. The flow:

1. **Release-cut step**: `make -C vendor/llamafile/dist` builds
   the composite from the slim binary + Ollama-blob symlink.
   Two consecutive builds produce byte-identical output — the
   recipe is reproducible.
2. **GitHub Release**: the asset is attached to the `vX.Y.Z`
   release; the SHA256 is committed in-tree at
   `vendor/llamafile/dist/SHA256SUMS.composite`.
3. **`install.py`**: on a fresh install with default-model
   choice, downloads the asset (via `gh` if present, otherwise
   `urllib.request`), verifies the SHA, places it at
   `vendor/llamafile/dist/qwen3-embedding-0.6b-16k.llamafile`,
   chmods +x.
4. **Custom-GGUF path**: `install.py` fetches only the slim
   binary from upstream (`gh release download 0.10.1 --repo
   mozilla-ai/llamafile --pattern llamafile-0.10.1`) and builds
   a composite locally with the user's GGUF via the same
   Makefile.

This keeps clones small (~10 MB) — the asset is fetched only on
hosts that actually enable the engine.

### Reproducible-build recipe

`vendor/llamafile/dist/Makefile`:

| Variable | Default | Purpose |
|----------|---------|---------|
| `VERSION` | `0.10.1` | upstream llamafile release tag |
| `PORT` | `38092` | baked into `.args` (adjacent to caliber 38090, consultants 38095) |
| `CTX_SIZE` | `16384` | baked into `.args` |
| `POOLING` | `last` | baked into `.args` |

Common targets:

```bash
# Build composite from cached inputs:
make -C vendor/llamafile/dist

# Fetch slim binary + zipalign helper from upstream:
make -C vendor/llamafile/dist fetch-slim

# Symlink the local Ollama qwen3-embedding blob into models/:
make -C vendor/llamafile/dist fetch-gguf-ollama

# Verify build + smoke-test:
make -C vendor/llamafile/dist check
```

## Installer flow (`install.py`)

v1.4 reorders the embedding-related sections of `main()` so the
chat-side URL is settled before the embedder dialog uses it,
and so the validate-only providers (Qdrant, memory_kg) report
*after* the client-embed providers (pgvector, sqlite_vec) are
configured:

```
... existing detection / verification ...
  ├─ _setup_ollama_chat            NEW — HyDE + reflect + consolidate
  ├─ _setup_pgvector_mcp           refactored — delegates to _setup_embedding_engine
  ├─ _setup_sqlite_vec_mcp         NEW — sqlite_vec had zero installer code pre-v1.4
  ├─ _validate_qdrant_embedding    NEW — connectivity probe + FastEmbed note
  ├─ _validate_memory_kg_embedding NEW — connectivity probe + server-side note
  ├─ _setup_proxy_orchestrator     (unchanged)
  └─ ...
```

### `_setup_ollama_chat`

Closes a long-standing gap: until v1.4 the **HyDE / reflect /
consolidate** sections had **no interactive prompts** — every
setting was hard-coded in `config.py`. The new dialog asks:

1. Use Ollama as a chat backend?
2. Ollama URL (`/api/generate`, validated via `/api/tags`).
3. HyDE: enabled, model, fallback model, `num_ctx`.
4. Share the same model/ctx for reflect + consolidate? (Y default.)
5. If no: separate model + ctx for each.

Writes:

- `hooks.user_prompt_submit.hyde_*`
- `reflect.ollama_url`, `reflect.ollama_model`, `reflect.num_ctx`
- `consolidate.ollama_url`, `consolidate.ollama_model`, `consolidate.num_ctx`

The chat URL is mirrored across all three blocks so a model
swap is a one-line edit rather than three.

### `_setup_embedding_engine` (per provider)

Asked once each for pgvector and sqlite_vec. If both are enabled
the second invocation defaults to "same as previous? [Y/n]" so
the common case is one dialog total. Per call:

1. **Ollama for embeddings?** [Y/n] — model (default
   `qwen3-embedding:0.6b`), `num_ctx` (default 16384 or
   existing), validate-model-present + offer to pull.
2. **OpenAI-compatible primary instead?** [y/N] — mutually
   exclusive with Ollama-primary. URL + model + API key
   (`${VAR}` references accepted). This is the **embeddings**
   endpoint (e.g. `text-embedding-3-small`), not chat.
3. **llamafile fallback?** [Y/n] — default Y; on fallback the
   user is warned that model + ctx must match the primary so
   the vector space stays consistent.
4. If both Ollama=no AND OpenAI=no: **llamafile is primary**
   (mandatory block).
5. llamafile sub-dialog: default settings (qwen3-embedding-0.6b,
   16k ctx) or custom GGUF + ctx; GPU mode (`auto` | `cpu`).

The composite asset is fetched once across providers if the
default model is chosen anywhere; custom-GGUF triggers a local
`make` instead.

### `_validate_qdrant_embedding` / `_validate_memory_kg_embedding`

Both Qdrant and memory_kg embed **server-side** (Qdrant uses
FastEmbed inside the MCP image; memory_kg has a bundled
embedder). claude-hooks doesn't override their embedders from
the client. The validators only:

1. Probe connectivity via the existing `Provider.verify()`.
2. Print a one-line `Probing http://h:32775/mcp ... OK` /
   `FAILED` line.
3. On OK, surface a short note about where the embedding model
   lives ("FastEmbed inside the MCP container" / "bundled in
   the MCP server").

No prompts beyond connectivity; no mutation of `cfg`.

## Operations

### Start / stop

The daemon owns the llamafile lifecycle — there's no separate
service to manage. To force-start the engine without waiting
for a real embed:

```python
from claude_hooks.daemon_client import embedding_ensure
embedding_ensure()  # {'port': 38092, 'ready': True, 'mode': 'gpu'}
```

To force-reap (e.g. before a model swap):

```python
from claude_hooks.daemon_client import embedding_shutdown
embedding_shutdown()
```

### Idle reaping

The reaper thread ticks every 60 s. If `last_activity_at` is
older than `idle_timeout_seconds` (300 s default) it sends
SIGTERM, waits 10 s, then SIGKILL. The next `_embedding_ensure`
respawns transparently — clients see one cold-start latency
(~1.2 s on solidpc with GPU) and otherwise nothing.

Tune via `cfg["embedding"]["idle_timeout_seconds"]`.

### GPU vs CPU

- **`mode: "auto"`** (default) — spawn with `-ngl 99`; the APE
  fat binary auto-probes CUDA → ROCm → Vulkan and picks the
  best backend. On failure the manager re-spawns with
  `--gpu disable` and sticks to CPU for the rest of the
  session.
- **`mode: "cpu"`** — always spawn with `--gpu disable`. Useful
  on shared boxes where VRAM is reserved for other workloads.

Detection helper `claude_hooks.gpu_probe.probe()` is used at
install time to suggest a default and at runtime to short-circuit
the `-ngl 99` attempt when no GPU is visible.

### Failure modes

| Symptom | Cause | What you'll see |
|---------|-------|-----------------|
| Composite missing | Pre-release host, or `install.py` not re-run | `LlamafileEmbedder` raises `EmbedderError`; composite falls back to Ollama primary if configured |
| Port 38092 already bound | Another llamafile, or stale orphan | `EmbeddingManager.ensure_running` logs + raises; daemon restart will re-adopt the PID file at `~/.claude/embedding-server.pid` |
| GPU spawn timeout | VRAM exhaustion | manager reaps and re-spawns with `--gpu disable`; subsequent embeds use CPU |
| Daemon down | systemd stopped, or `daemon_client` can't reach `:47018` | `LlamafileEmbedder` falls back to a direct HTTP probe of `:38092` (engine still alive across daemon restarts via `start_new_session=True`) |

## Cross-platform notes

- The fat llamafile is APE — one binary for Linux + macOS +
  Windows. Verified on solidpc (Linux). Pandorum (Windows)
  verification is the v1.4 deployment step.
- `subprocess.Popen(start_new_session=True)` works identically
  on POSIX and Windows (precedent: `consultants_forwarder.py`).
- `nvidia-smi.exe` is on PATH after CUDA install on Windows —
  `gpu_probe` works unchanged.
- `gh release download` works on Windows; absent `gh`,
  `install.py` falls back to `urllib.request` with progress +
  SHA verification.

## LAN-shared topology (#237 / #242, v1.8.4+)

The default install puts a llamafile on every host. On a multi-host
home setup that's wasteful — a desktop with limited RAM pays a
~200–400 MB resident cost for a service the LAN's beefier server
could provide once. v1.8.4 adds a producer/consumer split:

```
                 ┌──────────────────────────────────────────────┐
                 │ producer host (Linux server, lots of RAM)    │
   ┌──────────┐  │   embedding.host = "0.0.0.0"                 │
   │ consumer │──┼──> http://<producer-LAN-IP>:38092/embedding  │
   │ host A   │  │       (daemon supervises + idle-reaps)       │
   └──────────┘  │                                              │
   ┌──────────┐  │       same vector space, byte-identical     │
   │ consumer │──┼──>    weights → cross-host queries work     │
   │ host B   │  │                                              │
   └──────────┘  └──────────────────────────────────────────────┘
```

### Producer host (the one that runs the llamafile)

`install.py` asks during the llamafile sub-dialog:

```
Expose this llamafile on the LAN so other hosts can use it as a
remote embedder?
WARNING: the /embedding endpoint has NO authentication.
Only opt in on a trusted LAN.
Bind LAN-wide (0.0.0.0)? [y/N]:
```

Picking `y` writes `embedding.host = "0.0.0.0"` (default is
`127.0.0.1`). On the next daemon restart the llamafile binds all
interfaces and is reachable at `http://<this-host-LAN-IP>:38092/embedding`.

### Consumer host (the one without a local llamafile)

`install.py` asks during the embedding-engine dialog, after `Use
Ollama for embeddings? [Y/n]: n`:

```
Use a remote llamafile endpoint as primary (another LAN host)? [y/N]: y
  Endpoint URL [http://192.168.178.2:38092/embedding]:
  Timeout seconds [30]:
  Probing http://192.168.178.2:38092/embedding ... OK, dim=1024
```

The resulting config has `embedder = "llamafile"` with
`embedder_options = {url, timeout, daemon_ensure: false}`. The
`daemon_ensure: false` is critical — without it the consumer host's
daemon would try to spawn its own llamafile on every embed call,
defeating the whole point. install.py also strips any stale local
`embedding` block on the consumer side.

### Trade-offs

- **No auth on the embedder endpoint.** The home LAN is treated as
  a trust boundary. If your LAN isn't trusted, don't enable this.
- **Producer downtime takes consumers offline too.** Acceptable in
  practice because the pgvector / sqlite_vec store usually lives on
  the same producer host — recall would be down either way.
- **Linux producers serve Windows consumers faster than the consumer
  could embed locally.** Empirical 2026-05-19: pandorum (Windows)
  cold = 354 ms locally vs 28 ms via solidpc LAN, warm = 46 ms vs
  39 ms. llama.cpp's Windows build is consistently ~2× slower than
  the Linux build at identical model weights — the LAN hop is faster
  than running the Windows build locally.

## Out of scope (v1.4)

- **Chat-model migration**: llamafile's `--tools all` mode could
  replace Ollama for HyDE / advisor / consultants. Embedding-only
  is the cautious entry — the v1.4 HyDE prompts ask for **Ollama**
  chat models only.
- **Qdrant / memory_kg client-side embedding**: both MCP servers
  embed server-side; moving them onto our llamafile would need
  the MCP images to support a "raw vector ingest" mode.

## See also

- [`docs/llamafile-embedding-parity.md`](llamafile-embedding-parity.md)
  — the parity bench that motivated the integration.
- [`docs/daemon.md`](daemon.md) — daemon RPC protocol and HMAC
  wire format.
- [`docs/hyde.md`](hyde.md) — the HyDE recall pipeline that the
  v1.4 chat-side prompts feed into.
- [`docs/RELEASING.md`](RELEASING.md) — release-cut procedure.
