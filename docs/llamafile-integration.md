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

### Concurrency: `n_parallel` (slots)

The embedder is **one shared service with two callers of very
different urgency**:

| caller | on the critical path? | typical cost |
|---|---|---|
| interactive recall (`UserPromptSubmit`) | yes — bounded by a hook timeout | ~5 s at the 3.5 KB query clamp |
| detached turn-store (`Stop`) | no — nobody waits on it | ~5 s at the 2 KB store clamp |

With a single slot the second blocks the first, and the user pays
for a background write. `embedding.n_parallel` (default **3**) sets
`--parallel`, so a store no longer sits in front of a recall.

**`ctx_size` is per slot.** llama.cpp's `--ctx-size` is a *total* KV
budget divided across `--parallel`, so the manager spawns with
`ctx_size * n_parallel`. Passing it unscaled would shrink every
individual embed's window as you raise concurrency — trading a
visible queue for invisible truncation, which is the worse trade.
Budget RAM accordingly: 3 slots × 16 k ctx ≈ 3× the KV cache.

> **Do not set this to 1**, and do not "fix" it by passing
> `--parallel` to the running llamafile. The daemon rebuilds the argv
> on every respawn, so a hand-set flag survives only until the next
> idle reap — and its loss is silent, showing up as gradually slower
> turns rather than an error. It belongs in config. Changing it
> requires a **daemon restart**, not just an embedder reap: the
> daemon reads `cfg["embedding"]` at startup.

### Backend: CPU, Vulkan, CUDA, auto (ch-33)

claude-hooks picks the backend, never the engine. An opencoti
llamafile started without `--gpu` chooses a GPU itself (Metal → CUDA
→ AMD → Vulkan), so every spawn names one
(`claude_hooks/llamafile_backend.py`):

- **`mode: "cpu"`** — `--gpu disable`. A fresh install's default.
- **`mode: "vulkan"`** — `--gpu vulkan -dev <VulkanN> -ngl 99`. The
  device comes from `embedding.device` (part of the name
  `<llamafile> --gpu vulkan --list-devices` prints, e.g. `RENOIR`, or
  a `VulkanN` id; empty = the only device) and is resolved before each
  spawn, since the index moves with drivers. After the server is up,
  the manager embeds three probe texts and compares them with the CPU
  reference in `claude_hooks/embedder_probe_reference.json`; below
  cosine 0.99 it falls back to CPU for the daemon session and
  `_embedding_status` names why (`fallback_reason`). An unknown device,
  no health within `spawn_timeout_seconds`, or a refused driver fall
  back the same way.
- **`mode: "cuda"`** — `--gpu nvidia -dev <CUDAn> -ngl 99`; needs a
  CUDA composite. Device, check and fallback as for Vulkan.
- **`mode: "auto"`** — the pre-ch-33 flags (`-ngl 99`, the engine's
  own pick). Kept for configs that already say it.

`embedding.pci` names the device by PCI address instead
(`dddd:bb:dd.f`, or `bb:dd.f`); it wins over `device` and is the only
name that tells two identical cards apart.

Vulkan knobs: `vulkan_icd` hands the child one driver
(`VK_ICD_FILENAMES`), e.g. a private RADV build; `allow_old_driver`
sets `OPENCOTI_VK_ALLOW_OLD_DRIVER=1` (off: see below);
`verify_backend` turns the probe check off. The daemon's own
environment never decides: these variables are cleared for the child
unless set here.

The child always gets a private `HOME` and `TMPDIR` under
`~/.claude/claude-hooks-llamafile/`. The daemon's sandbox
(`ProtectSystem=strict`, `ProtectHome=read-only`) leaves the engine
nowhere to unpack its bundled `ggml-vulkan` library, and it then
loads whatever older copy sits in `~/.llamafile/v/<version>/` without
a word (on solidpc: the r8 library under an r19 engine). Cosmopolitan
also builds its `dlopen` helper in `TMPDIR` with the host's C
compiler, so Vulkan needs `cc` on Linux.

Measured on solidpc (Ryzen 5 5600G, Radeon Renoir iGPU, 2026-11-27):

| | min cos vs mozilla CPU | ~460-token embed |
|---|---|---|
| opencoti r19 CPU | 0.99894 (mean 0.99963) | — |
| opencoti r19 Vulkan, system Mesa 20.3.5 + override | −0.067 (mean 0.378) | wrong vectors, no error |
| opencoti r19 Vulkan, Mesa 25.2.8 | 0.99905 (mean 0.99953) | 0.65 s (vs 10.6 s on the loaded CPU) |

The engine refuses RADV from Mesa 20 or older for this reason, and
Debian bullseye ships nothing newer. `scripts/build_private_radv.sh`
builds RADV 25.2.8 (ACO only, no LLVM or GL) with its libdrm into a
prefix of its own; point `embedding.vulkan_icd` at its
`share/vulkan/icd.d/radeon_icd.x86_64.json`. The system Mesa is not
touched.

### Switching the backend at runtime

The daemon serves the embedder's control surface twice: as signed RPC
for `claude-hooks-daemon-ctl`, and as HTTP on
`http://127.0.0.1:38091/embedder/` (`embedding.control_host` /
`control_port`; 0 turns it off; switching is answered for loopback
clients only).

```bash
claude-hooks-daemon-ctl embedder status            # backend in force, device, PCI, fallback
claude-hooks-daemon-ctl embedder backends          # cpu / vulkan / cuda, every GPU + PCI
claude-hooks-daemon-ctl embedder switch cpu --reason "opencoti gate on the iGPU"
claude-hooks-daemon-ctl embedder switch vulkan --device RENOIR
claude-hooks-daemon-ctl embedder switch cuda --pci 0000:2d:00.0
claude-hooks-daemon-ctl embedder reset             # back to the configured backend

curl -s 127.0.0.1:38091/embedder/backends
curl -s -X POST 127.0.0.1:38091/embedder/backend -d '{"mode":"cpu","reason":"gate"}'
curl -s -X POST 127.0.0.1:38091/embedder/reset -d '{}'
```

A switch resolves the device first (nothing changes when it is not
there, 409), waits until the embedder is idle on two reads 0.25 s
apart (`--timeout`, default 120 s; `--force` restarts after it
anyway), stops the server and starts it on the new backend, with the
same probe check and CPU fallback as any start. Embeds that arrive
during the restart (a few seconds) wait on the daemon's lock; a LAN
consumer that talks to `:38092` directly sees one failed request and
its own fallback. A switch is kept in
`~/.claude/claude-hooks-embedder-backend.json` across daemon restarts
until `reset` (`--no-persist` keeps it for this daemon run only). A
stopped embedder is not started: the next embed starts it on the new
backend. Device lists are cached 5 minutes (`backends --refresh`).

The gate pattern: before a measurement on the GPU the embedder uses,
`embedder switch cpu --reason "<gate>"`; after it, `embedder reset`.

"Idle" is three readings, any one of which means busy:

- a client connection open to the embed port (`/proc/net/tcp` on Linux,
  `netstat` on Windows). A request is visible there from the moment it
  connects; `/slots` shows it only once a slot takes it, which for a
  14k-character text on a loaded CPU was ~3 s later — the first live
  switch cut such a request. Every embedder client here opens one
  connection per request, so an idle client holds none for long;
- a slot `is_processing`;
- `/slots` not answering within 2 s while the server is up: the mozilla
  engine answers it from its inference loop, so it stalls for the length
  of an embed (`claude_hooks/embedder_gate.py`).

PCI addresses: neither engine backend prints one in `--list-devices`, so
the listing fills them from the host — CUDA from `nvidia-smi` (the child
runs with `CUDA_DEVICE_ORDER=PCI_BUS_ID`, so `CUDAn` is nvidia-smi's
index n), Vulkan from `/sys/bus/pci/devices` by vendor when exactly one
display device of that vendor exists (else blank). The running device's
address is then taken from the server's `/props`
(`opencoti.devices[].pci`). On solidpc: `CUDA0` = `0000:01:00.0`,
`Vulkan0` (Renoir) = `0000:18:00.0`.

### Composites

| asset (release `embedder-c14-r19`) | backends | runs on |
|---|---|---|
| `qwen3-embedding-0.6b-16k-opencoti-c14-r19.llamafile` | cpu, vulkan | Linux, Windows, macOS |
| `…-opencoti-c14-r19-cuda-linux-x86_64.llamafile` | cpu, vulkan, cuda | Linux x86_64 |
| `…-opencoti-c14-r19-cuda-win-x86_64.llamafile` | cpu, vulkan, cuda | Windows x86_64 |
| `qwen3-embedding-0.6b-16k.llamafile` (release `v1.4.0`, legacy) | cpu, auto | any |

`install.py` asks which one (a re-run keeps the configured one, a
fresh install takes CPU + Vulkan) and which backend and device; the
CUDA builds carry the CUDA 13 library and the CUDA 12 one for GPUs
before compute capability 7.5, and are one per OS to stay under
GitHub's 2 GiB asset limit. SHAs are in
`vendor/llamafile/dist/SHA256SUMS.composite`.

The opencoti composites are built by `make -C vendor/llamafile/dist
fetch-opencoti opencoti opencoti-cuda` from the inputs pinned in
`vendor/llamafile/opencoti-c14-r19/SHA256SUMS` (bare engine + Linux
and Windows Vulkan libraries + the same GGUF; 932 MB against the
mozilla composite's 1.5 GB). Its baked `.args` name no backend, and a
test fails if they do.

### Failure modes

| Symptom | Cause | What you'll see |
|---------|-------|-----------------|
| Composite missing | Pre-release host, or `install.py` not re-run | `LlamafileEmbedder` raises `EmbedderError`; composite falls back to Ollama primary if configured |
| Port 38092 already bound | Another llamafile, or stale orphan | `EmbeddingManager.ensure_running` logs + raises; daemon restart will re-adopt the PID file at `~/.claude/embedding-server.pid` |
| GPU spawn timeout | VRAM exhaustion | manager reaps and re-spawns with `--gpu disable`; subsequent embeds use CPU |
| `fallback_reason: probe cosine … < 0.99` | The Vulkan driver computes wrong vectors (e.g. old Mesa) | CPU for the session; build a newer driver (`scripts/build_private_radv.sh`) and set `vulkan_icd` |
| `fallback_reason: the engine lists no usable Vulkan device` | Driver refused as too old, or none installed | CPU for the session; same fix |
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
