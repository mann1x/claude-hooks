# What's new in v1.4.0

> **Released:** 2026-05-14 · cut from `dev` after the llamafile
> integration arc · prior release was
> [v1.3.2](https://github.com/mann1x/claude-hooks/releases/tag/v1.3.2)
> on 2026-05-13
>
> **Migration:** drop-in for existing installs. Nothing in your
> running config breaks. Re-run `python install.py` on each host
> to pick up the new prompts and (optionally) enable the
> llamafile embedding engine. See
> [`docs/RELEASING.md`](RELEASING.md) for the upgrade procedure
> and [`docs/llamafile-integration.md`](llamafile-integration.md)
> for the architecture deep-dive.
>
> **Older release notes** (kept verbatim for the record):
> [v1.1](whats-new-v1.1.md). v1.0/v1.2/v1.3 highlights live in
> [`CHANGELOG.md`](../CHANGELOG.md).

This is the human-readable v1.4 highlights doc. For the full,
release-engineered, "every commit accounted for" record see
[`CHANGELOG.md`](../CHANGELOG.md).

---

## The one-paragraph summary

claude-hooks v1.4 turns the embedding side of the recall pipeline
into something that **can survive an Ollama outage without losing
recall**. We vendored Mozilla's
[`llamafile`](https://github.com/mozilla-ai/llamafile) (a single
APE binary that runs across Linux + macOS + Windows), bundled
`qwen3-embedding-0.6b` into a 1.5 GB composite, and taught the
existing `claude-hooks-daemon` to spawn it on demand with the
same 5-minute idle-reap that Ollama uses by default. A new
`CompositeEmbedder` tries Ollama (or an OpenAI-compatible
endpoint) on every embed and drops to llamafile on
`EmbedderError`. The installer dialog grew a real
embedder-engine sub-flow that drives both `pgvector` and
`sqlite_vec` providers, plus three more long-standing gaps
finally closed: interactive HyDE / `/reflect` / `/consolidate`
prompts, a dedicated `sqlite_vec` setup helper, and validate-only
connectivity checks for the server-side-embedding MCPs (Qdrant
and memory_kg).

---

## Why this exists — the failure mode

Pre-v1.4, every claude-hooks install with `pgvector` or
`sqlite_vec` had a hard dependency on the configured embedder
endpoint. The embedder was almost always Ollama on the same LAN,
and Ollama on a busy host can decide to unload your model, fail
to load it back (RAM pressure, model file missing, daemon
restart mid-pull), or just be unreachable for the duration of an
upgrade. The symptom was always the same:

```
[WARNING] claude_hooks.providers.pgvector: pgvector embed
failed: ollama unreachable at http://192.168.178.2:11433/api/
embeddings: timed out
```

Recall returns zero hits, the model proceeds without prior
context, and the user only finds out 30 turns later when memory
that "should have been there" wasn't.

v1.4 fixes this by making the embedding tier **structurally
redundant**: a primary that talks to your existing Ollama (or
OpenAI-compatible endpoint), and a fallback that runs
side-by-side as a daemon-managed local process. Both speak the
same 1024-dim `qwen3-embedding` vector space, so the failover is
transparent to the rest of the pipeline. Parity bench shows mean
cosine 0.99963 over 23 mixed prompts between Ollama and
llamafile serving the same GGUF — they are interchangeable.

---

## Llamafile as a fallback-capable embedding engine

The shipped composite is `qwen3-embedding-0.6b-16k.llamafile`
(1.52 GB, dim 1024, native 32 k ctx — we bake 16 k as the
default for cache friendliness, override with the installer's
custom-context prompt). It is APE
([Cosmopolitan-Libc](https://github.com/jart/cosmopolitan)) so
the **same file** runs across Linux, macOS, and Windows. The
host detects CUDA / ROCm / Vulkan at runtime and picks the best
GPU backend, transparently falling back to CPU on VRAM
exhaustion.

### How it's supervised

The user's strong preference here was "no new sibling service"
— and the existing `claude-hooks-daemon` already had a proven
detached-subprocess lifecycle (`consultants_forwarder.EngineManager`
spawning the consultants engine, with idle-reap and signal
ladder). We extracted that pattern as
[`claude_hooks.embedding_manager.EmbeddingManager`](../claude_hooks/embedding_manager.py)
and attached it to the daemon:

```
pgvector / sqlite_vec
   └─ CompositeEmbedder(primary=Ollama, fallback=Llamafile)
                          │ embed("foo")
                          ▼
                 LlamafileEmbedder.embed("foo")
                   1. RPC → claude-hooks-daemon "_embedding_ensure"
                   2. daemon spawns llamafile (lazy) or returns
                      "already up on :38092"
                   3. embedder POSTs to :38092/embedding
                   4. on HTTP error → re-ensure, retry once
                          │
                          ▼
                 claude-hooks-daemon (existing TCP RPC :47018)
                   └─ EmbeddingManager
                        ├─ subprocess.Popen of the composite
                        │     --port 38092 --pooling last
                        │     --ctx-size 16384 [-ngl 99 if GPU]
                        ├─ last_activity_at touched on every embed
                        ├─ reaper thread (60s tick, 300s idle)
                        │   → SIGTERM, 10s grace, SIGKILL
                        └─ next ensure_running re-spawns transparently
```

Three new daemon RPC ops (`_embedding_ensure`,
`_embedding_status`, `_embedding_shutdown`) ride on the same
HMAC-signed wire protocol as the existing
`_ping`/`_shutdown`/hook-dispatch ops. Typed best-effort
wrappers live in `claude_hooks/daemon_client.py` — daemon-down
returns `None`, `{ok: false}` returns `{available: false}`, so
callers never need to special-case the supervision path.

### GPU vs CPU — one knob, transparent fallback

The installer asks **one** question: `GPU mode [auto/cpu]`. The
default flips on `gpu_probe.probe()`:

- **`auto`** — spawn with `-ngl 99` (offload all layers); the
  APE runtime auto-detects CUDA / ROCm / Vulkan and picks the
  best backend. On spawn failure or first-embed timeout, the
  manager reaps and respawns with `--gpu disable`, marks
  `gpu_offload_failed=True` for the rest of the session.
- **`cpu`** — `--gpu disable` from the start. Useful when you're
  running on a shared box where VRAM is reserved for other
  workloads (e.g. a host that hosts both claude-hooks and an
  unrelated ML training run).

There is no need for multiple fat binaries or per-platform dylib
selection in v1.4. The APE dispatcher inside the upstream binary
handles all that for free.

### Cold-spawn cost — what to expect

Measured on the two deployment hosts after the v1.4 cut, with
the composite already cached in page cache (cold-start cost is
roughly half I/O + half kernel exec + APE shell bootstrap):

| Host | Platform | First spawn | Warm embed |
|---|---|---|---|
| solidpc | Linux 6.2, RTX 3090, CPU mode | 1.2 s | 50 ms |
| pandorum | Windows 10 19045, RTX 5080, CPU mode | 7.5–12 s | 80 ms |

The Windows cold-spawn is higher because of how cmd / pythonw
launch APE binaries through the embedded shell bootstrap. After
the first spawn the binary sits in memory and every subsequent
embed is sub-100 ms. With the 5-minute idle-reap default, a
steadily-used host pays the cold-spawn cost once per 5 min of
quiet — fine for active sessions, tunable via
`embedding.idle_timeout_seconds` for sporadic single-prompt
work.

### Distribution — GitHub Release asset, SHA-verified at install

The composite is **not** committed (clones would balloon from
~10 MB to ~1.5 GB) and **not** rebuilt at install time. The
release-cut workflow:

1. `make -C vendor/llamafile/dist` builds the composite from the
   upstream slim binary + a symlink to your local Ollama
   `qwen3-embedding` blob. The recipe is byte-reproducible (two
   consecutive `make clean && make` produce identical SHA
   `414f6166...`).
2. `gh release upload v1.4.0 vendor/llamafile/dist/qwen3-embedding-0.6b-16k.llamafile`
   attaches the asset; the SHA file
   (`vendor/llamafile/dist/SHA256SUMS.composite`) is committed
   in-tree.
3. On a fresh install, `install.py` fetches the asset via
   `gh release download` (or falls back to `urllib.request`),
   verifies the SHA against the committed checksum, places it at
   the standard path, chmods +x. Mismatch is a hard error with a
   `redownload or rebuild` breadcrumb.

For custom-GGUF setups (you want a different embedding model),
the installer fetches only the slim binary from upstream and
builds a composite locally with your GGUF via the same Makefile.

---

## The installer dialog finally feels v1-shaped

Until v1.4, three configuration sections had **zero interactive
prompts** — they were hard-coded defaults in `config.py` that
you had to override by hand-editing JSON after the install ran.
v1.4 closes all three:

### `_setup_ollama_chat` (new)

A single dialog covering:

1. Use Ollama as a chat backend? (Validates `/api/tags`.)
2. Ollama URL (`/api/generate` endpoint).
3. **HyDE**: enabled, model, fallback model, `num_ctx`.
4. **Shared-skills shortcut**: same model + ctx for `/reflect`
   and `/consolidate`? Default Y — the common case.
5. Or separate model + ctx per skill if you want them to
   differ.

Writes `hooks.user_prompt_submit.hyde_*`, `reflect.*`, and
`consolidate.*` with the chat URL mirrored across all three so a
model swap is a one-line edit rather than three.

### `_setup_sqlite_vec_mcp` (new)

The sqlite_vec provider had been a registered scaffold for ~5
versions but `install.py` had no helper for it — users hit
`embedder` keys that needed manual completion. v1.4 adds a
proper setup helper that mirrors the pgvector flow:
db_path / table prompts, embedder choice delegated to the shared
engine dialog, extension-availability probe.

### `_setup_embedding_engine` (new — drives both pgvector and sqlite_vec)

The same dialog is asked once per local-embed provider you
enable, with a "same as previous?" shortcut on the second
invocation so the common case is one dialog total. The choices
in order:

1. **Use Ollama for embeddings?** Model (default
   `qwen3-embedding:0.6b`), `num_ctx` (default 16384), with an
   `ollama pull` offer if the model isn't present.
2. **OpenAI-compatible primary instead?** Mutually exclusive
   with Ollama-primary. URL + model + API key (env-var
   references like `${OPENAI_API_KEY}` accepted). This is the
   **embeddings** endpoint, not chat — common targets are
   `https://api.openai.com/v1/embeddings`, an LM Studio
   instance, or a vLLM `--embed` deployment.
3. **Use llamafile as fallback?** Default Y when a primary is
   set; mandatory primary when both Ollama and OpenAI are
   declined. The dialog warns that model + ctx must match the
   primary so the vector space stays stable across failover.
4. **llamafile sub-dialog** — default model + ctx, or custom
   GGUF path (validated against the GGUF magic bytes); GPU mode
   `auto` vs `cpu` (default flips on `gpu_probe`).

The composite fetch happens once across providers (not per
provider) if the default model is chosen anywhere.

### `_validate_qdrant_embedding` + `_validate_memory_kg_embedding` (new)

Both Qdrant and memory_kg embed **server-side** (Qdrant uses
FastEmbed inside its MCP image; memory_kg has a bundled
embedder). claude-hooks doesn't override their embedders from
the client. The new validators only:

- Probe connectivity via the existing `Provider.verify()`.
- Print a one-line `Probing http://h:32775/mcp ... OK / FAILED`.
- On OK, surface a short note about where the embedding model
  lives (`FastEmbed inside the MCP container` /
  `bundled in the MCP server`).

No prompts beyond connectivity; no mutation of `cfg`. The point
is to make the install summary report something useful instead
of silently skipping the server-side providers.

---

## Operational details worth knowing

### The 5-minute idle reap, matched to Ollama

`OLLAMA_KEEP_ALIVE=5m` is the de-facto convention on the Ollama
side. v1.4's `EmbeddingManager` defaults to the same window
(`idle_timeout_seconds=300`) so the user-visible model-residency
behavior is consistent across primary + fallback. The reaper
ticks every 60 s, checks `time.time() - last_activity_at`
against the threshold, and runs SIGTERM → 10 s → SIGKILL on
miss. Tunable per-install.

### Windows console-window detachment

Without explicit creationflags, the spawned llamafile inherits
the parent's console on Windows — visible as a stray `cmd`
prompt on the user's desktop. v1.4 ships with the right pattern
borrowed from `claudemem_reindex._spawn_reindex` and
`lsp_engine.client`: `CREATE_NO_WINDOW | DETACHED_PROCESS` plus
`stdin=DEVNULL`. Verified on pandorum: the spawned process
reports `Window Title: N/A` and no window appears.

### APE binary bootstrap on POSIX

llamafile binaries start with `MZqFpD='` magic that the Linux
kernel doesn't recognize as a binfmt directly (you'd need
`binfmt_misc` registration for APE, which the upstream README
recommends but most claude-hooks installs don't have). The bytes
are simultaneously a valid POSIX shell script whose first action
is to re-exec the kernel-level entry point — so on POSIX,
`EmbeddingManager` invokes the binary through `/bin/sh`, which
runs the shell prefix and lets the embedded `exec` jump to the
actual program. Windows direct-exec works unchanged (the binary
is also a valid PE).

### PID-file re-adoption across daemon restarts

The spawned llamafile lives in its own session
(`start_new_session=True` on POSIX, `DETACHED_PROCESS` on
Windows) — it survives daemon crashes. On daemon restart,
`EmbeddingManager` reads `~/.claude/embedding-server.pid`, tries
to adopt the existing process, and only spawns fresh if the
adoption fails. The cost of a daemon restart drops from "one
cold-spawn per restart" to "zero".

---

## Verification — what we ran before the cut

1. **Parity bench** (`vendor/llamafile/dist/bench_parity.py`):
   23 mixed prompts embedded through both Ollama and llamafile,
   pairwise cosine similarity. Mean 0.99963, min 0.99928. The
   vectors are interchangeable for recall purposes.
2. **Unit tests**: 2333 passed + 24 skipped (up from v1.3.2's
   2146). The +187 new tests cover the new embedder classes
   (single + batch + error shapes), spawn lifecycle + APE-wrap +
   detachment, daemon RPC ops, GPU probe, and the four
   installer-dialog branches (Ollama / OpenAI / llamafile /
   no-primary).
3. **End-to-end on solidpc**: pgvector store + recall through
   the composite embedder, sentinel string round-tripped, daemon
   log confirms each embed advanced `last_activity_at`.
4. **End-to-end on pandorum** (Windows): same flow as solidpc
   plus the windowless-spawn confirmation.

---

## Out of scope (deliberately)

A few things v1.4 does **not** change, to keep the blast radius
bounded:

- **Chat-model migration.** llamafile's `--tools all` mode could
  in principle replace Ollama for HyDE / `/get-advice` /
  `/consultants`. Embedding-only is the cautious entry — the
  v1.4 HyDE prompts ask for **Ollama** chat models only.
  Swapping HyDE / advisor / consultants to llamafile chat is a
  separate track.
- **Qdrant / memory_kg client-side embedding.** Both MCPs embed
  server-side today. Moving them onto our llamafile would need
  the MCP images to support a "raw vector ingest" mode, which is
  a separate change to the vendored MCP images.

---

## See also

- [`docs/llamafile-integration.md`](llamafile-integration.md) —
  architecture + installer flow + ops runbook.
- [`docs/llamafile-embedding-parity.md`](llamafile-embedding-parity.md)
  — the bench detail that motivated the integration.
- [`docs/daemon.md`](daemon.md) — daemon RPC protocol + HMAC
  wire format.
- [`docs/hyde.md`](hyde.md) — the HyDE recall pipeline the new
  chat-side prompts feed into.
- [`docs/pgvector-runbook.md`](pgvector-runbook.md) — full
  pgvector backend guide; the embedder-choice section now
  references the v1.4 dialog.
- [`docs/RELEASING.md`](RELEASING.md) — release-cut procedure.
- [`CHANGELOG.md`](../CHANGELOG.md) — every commit accounted
  for.
- [`docs/whats-new-v1.1.md`](whats-new-v1.1.md) — prior
  human-readable highlights doc (kept verbatim for the record).
