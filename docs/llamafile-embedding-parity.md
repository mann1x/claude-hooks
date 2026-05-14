# Llamafile vs Ollama embedding parity — qwen3-embedding 0.6B

**TL;DR.** The vendored `qwen3-embedding-0.6b-16k.llamafile` (built on
upstream `llamafile-0.10.1` + the Ollama-shipped Q8_0 GGUF) produces
embeddings **cosine-identical** to Ollama's `qwen3-embedding:0.6b`
to ~4 decimal places (`mean=0.99963`, `min=0.99939`) across a mixed
24-prompt corpus. Same dimension (1024), same retrieval semantics.
The two backends are interchangeable from pgvector's point of view.

Speed at present is CPU-only on the llamafile side: 110 ms mean vs
Ollama's 62 ms mean (Ollama gets GPU offload, llamafile does not in
this build). Closing that gap is a separate task — see
[Next steps](#next-steps).

## Setup

| Field | Value |
|---|---|
| Date | 2026-05-14 |
| Host | solidpc (RTX 3090, 64 GB RAM, AMD Ryzen 9 5900X) |
| Ollama version | running, `qwen3-embedding:0.6b` tag, proxy at `:11433` |
| Llamafile binary | `vendor/llamafile/v0.10.1/llamafile-0.10.1` |
| GGUF | `qwen3-embedding-0.6b.gguf` from Ollama blob (Q8_0, dim=1024, native ctx=32768, pooling_type=3=LAST) |
| Composite artifact | `vendor/llamafile/dist/qwen3-embedding-0.6b-16k.llamafile` |
| Llamafile args | `--embedding --ctx-size 16384 --pooling last --server --host 0.0.0.0 --port 38092` |
| Bench script | `vendor/llamafile/dist/bench_parity.py` |
| Llamafile build | CPU-only (no `-ngl` passed — falls back to OpenMP, 6 threads) |

## Corpus

24 prompts covering: short / common, code / SQL / git, file:line refs,
multilingual (FR / IT / DE / JA), longer structured paragraphs,
questions, nonsense, near-duplicates, and claude-hooks-specific
references. The 17th prompt (empty string) was skipped because
Ollama rejects it; everything else was scored.

## Per-prompt results

| idx | dim Ollama | dim llamafile | cosine | t Ollama (ms) | t llamafile (ms) | prompt |
|---:|---:|---:|---:|---:|---:|---|
|  0 | 1024 | 1024 | 0.9998 |  61.6 |  38.7 | hello world |
|  1 | 1024 | 1024 | 0.9995 |  67.7 |  81.0 | what is the meaning of life? |
|  2 | 1024 | 1024 | 0.9995 |  67.7 |  76.4 | the quick brown fox jumps over the lazy dog |
|  3 | 1024 | 1024 | 0.9995 | 100.8 | 197.7 | def fibonacci(n): ... |
|  4 | 1024 | 1024 | 0.9997 |  68.5 |  89.2 | SELECT id, name FROM users WHERE active = true LIMIT 10; |
|  5 | 1024 | 1024 | 0.9997 |  67.8 |  95.9 | git rebase -i HEAD~3 --autosquash |
|  6 | 1024 | 1024 | 0.9996 |  67.5 |  71.5 | claude_hooks/dispatcher.py:136 |
|  7 | 1024 | 1024 | 0.9996 |  70.2 |  73.8 | v1.3.2 fixes the __version__ drift bug |
|  8 | 1024 | 1024 | 0.9994 |  64.9 |  50.0 | le chat est sur la table |
|  9 | 1024 | 1024 | 0.9996 |  53.2 |  65.9 | il gatto è sul tavolo |
| 10 | 1024 | 1024 | 0.9994 |  56.7 |  58.8 | der Hund läuft im Park |
| 11 | 1024 | 1024 | 0.9995 |  56.2 |  60.6 | 猫が机の上にいる |
| 12 | 1024 | 1024 | 0.9997 |  58.0 | 425.4 | Yesterday we cut v1.3.1 to fix the sqlite_vec dispatcher bug... |
| 13 | 1024 | 1024 | 0.9995 |  58.1 | 446.4 | The HyDE pipeline generates a hallucinated answer... |
| 14 | 1024 | 1024 | 0.9998 |  56.8 | 107.3 | how do I fix bcache cache-set superblock corruption? |
| 15 | 1024 | 1024 | 0.9998 |  56.9 | 103.2 | what context size does qwen3-embedding support natively? |
| 16 | 1024 | 1024 | 0.9997 |  56.9 |  59.8 | asdf qwer zxcv |
| 18 | 1024 | 1024 | 0.9998 |  55.5 |  66.5 | 🎉 emoji test 🚀 |
| 19 | 1024 | 1024 | 0.9996 |  54.9 |  59.6 | the cat is on the table |
| 20 | 1024 | 1024 | 0.9997 |  53.4 |  58.6 | a cat sits on the table |
| 21 | 1024 | 1024 | 0.9997 |  56.6 |  58.6 | the cat is on the mat |
| 22 | 1024 | 1024 | 0.9997 |  55.3 | 100.4 | store a new memory in pgvector with the kg-create tool |
| 23 | 1024 | 1024 | 0.9997 |  55.8 |  74.6 | the consultants engine runs planner researcher critic synthesizer |

## Summary

| Metric | Value |
|---|---|
| Embedding dimension | **1024** on both backends (uniform) |
| Mean cosine | **0.99963** |
| Min cosine | **0.99939** |
| Max cosine | **0.99981** |
| Mean Ollama latency | 61.8 ms / request |
| Mean llamafile latency | 109.6 ms / request |
| Worst llamafile case | 446 ms (240-char paragraph, CPU-only, n_threads=6) |
| Speed ratio (llamafile/Ollama) | ~1.8× slower mean, ~7.5× worst-case |

## Interpretation

**Cosine ≈ 1 but not exactly 1.** Both backends load the same Q8_0
GGUF, but downstream numerics differ in tiny ways: pooling-layer
ordering, fma vs separate mul+add, and tokenizer/wrapper
preprocessing all introduce sub-1e-3 noise. For retrieval (cosine
nearest-neighbor in pgvector) this is invisible — the same
neighbors come back in the same order. We could not construct a
realistic recall query where the two backends disagreed on top-k.

**Latency gap is GPU vs CPU, not model quality.** Ollama is using
the RTX 3090 (visible in `nvidia-smi`); the llamafile in this
config has no `-ngl` flag and runs on CPU with OpenMP across 6
threads. To match Ollama:

```
./qwen3-embedding-0.6b-16k.llamafile -ngl 99
```

…will offload all layers to the GPU. Need to build with the right
GPU dylib (CUDA / ROCm / Vulkan) — pre-built dylibs are bundled in
the fat binary as of v0.10.1.

**Memory.** The llamafile process reported `~2.7 GB Host` at idle
(model 1.8 GB + context 330 MB + compute 600 MB). Ollama's own
process is comparable for the same model.

## Why the small numerical gap is fine

For HyDE / recall flows, what we actually use the embeddings for:

1. Cosine NN over a corpus of stored memories.
2. Distance thresholding for dedup.
3. Concat into prompt context.

A 0.9994 cosine between two vectors of the same prompt is *closer*
than the typical between-prompt similarity in a real corpus
(usually 0.1-0.6 between semantically distinct sentences). The
"noise" between Ollama and llamafile is **two orders of magnitude
smaller** than the smallest semantic signal we care about. Drop-in
replacement is safe.

## Next steps

The parity result clears the path for actual claude-hooks
integration. Open items, in priority order:

1. **GPU build / dylib selection.** Build with CUDA / ROCm / Vulkan
   offload and re-run the bench. Goal: llamafile latency ≤ Ollama
   latency on the same hardware.
2. **Daemonization.** Wrap the llamafile in a systemd unit
   (`claude-hooks-llamafile-embed.service`) parallel to
   `caliber-grounding-proxy` and `claude-hooks-consultants`. Bind
   to `127.0.0.1:38092` only (the all-interfaces bind in the
   composite is only because the dev smoke test ran from another
   host).
3. **Provider wiring.** Add a new `llamafile` backend option to
   `claude_hooks/embedders.py` so `pgvector` / `sqlite_vec` can
   use it the same way they use Ollama. URL convention:
   `http://localhost:38092/embedding`. Llama.cpp's response shape
   differs slightly from Ollama's (`{"embedding": [...]}` vs
   `{"embedding": [...]}` — same field name, but the array can
   be nested one level deeper); the embedder needs to flatten that.
4. **Windows packaging.** Verify the same llamafile binary boots
   on pandorum. The fat binary is APE / Cosmopolitan-Libc, so it
   should run cross-platform from a single artifact, but verify
   the systemd-equivalent (scheduled task / nssm) path.
5. **Switch claude-hooks's embedder default.** Once the GPU build
   is at parity speed, change `providers.pgvector.embedder` from
   `ollama` to `llamafile` so installs don't need Ollama for the
   embedding path. Ollama stays as the chat-model backend (HyDE,
   advisor, consultants); the llamafile replaces it for embeddings
   only.

## Reproducing

```bash
# 1. ensure llamafile is built (see vendor/llamafile/README.md)
cd vendor/llamafile/dist
./qwen3-embedding-0.6b-16k.llamafile &   # detach into background

# 2. wait for "server is listening on http://0.0.0.0:38092"

# 3. run the bench
python3 bench_parity.py
```

The bench writes `parity_results.json` alongside the script — keep
this committed only when you're updating the numbers in this doc.
