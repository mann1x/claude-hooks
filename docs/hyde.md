# HyDE query expansion

HyDE — **Hy**pothetical **D**ocument **E**mbeddings — is the trick that
turns a vague user prompt into something that actually matches the way
memories are stored. Stock vector recall searches the prompt's
embedding directly; HyDE first asks a small local LLM to *hallucinate
a short factual answer* in the shape of a stored memory, then searches
with that answer's embedding instead. The hallucination lives in the
same "answer space" as the real memories, so cosine similarity finds
the right ones much more reliably.

claude-hooks ships HyDE off by default because it adds a 0.5–4 s LLM
call per `UserPromptSubmit`. Turn it on when recall quality matters
more than latency, or when you've already paid for the LLM via
Ollama keep-alive.

## When it fires

`UserPromptSubmit` runs the recall pipeline in
[`claude_hooks/recall.py`](../claude_hooks/recall.py). The pipeline:

1. **Raw recall** — query every active provider with the user's literal
   prompt. Always runs. The hits land in `additionalContext` even if
   HyDE later does nothing.
2. **HyDE** — only if `hyde_enabled: true` AND the raw recall returned
   at least one hit. If raw returns nothing, HyDE is skipped on
   purpose: there is no memory to ground against, and an ungrounded
   LLM on niche project jargon (`109e Gemma`, `bcache`, `pgvector
   qwen3 schema`) reliably hallucinates something useless.
3. **Refined recall** — if the expansion produced a string different
   from the raw prompt, query each provider again with the expansion
   and merge results raw-first into the existing hit list (deduped on
   text).

So HyDE is opt-in *and* short-circuits when it can't help.

## Two modes: grounded vs plain

`hyde_grounded` (default `true`) selects between them. Both are
implemented in [`claude_hooks/hyde.py`](../claude_hooks/hyde.py).

### Plain (`hyde_grounded: false`)

`expand_query()` — feeds only the raw prompt to the LLM with a
"write this as a stored memory entry" system prompt. Cheap, but the
LLM only knows what's in its training data. For project-specific
jargon it has never seen, expansions can drift wildly:

> User: "Why does our 109e Gemma run slow on q6_K?"
> Expansion: "Tesla Model Y 109e accelerates from 0–60 in ..."

Useful when your queries are *general* (Python/Linux/networking
questions) and stored memories cover broad topics.

### Grounded (`hyde_grounded: true`, default)

`expand_query_with_context()` — runs the raw recall first, takes the
top `hyde_ground_k` hits (default 3, capped at `hyde_ground_max_chars`
characters total), feeds them to the LLM as factual context, then
asks for the hypothetical answer. The system prompt explicitly tells
the model to anchor its answer in the supplied memories and not
invent facts.

The grounding source is picked in this order:

1. `hyde_grounding_provider` from config, if it returned hits.
2. `qdrant`, then `pgvector`, then `sqlite_vec` — first one with hits wins.
3. Whichever provider returned the most raw hits.

Grounded HyDE is the right default. It catches niche jargon (the
memories themselves anchor the LLM in your terminology) and still
helps on broad queries (the memories add specificity the raw prompt
lacks).

## The expansion cache

Same `(prompt, model, grounding)` input always produces the same
expansion, and the LLM call is the slowest step in the pipeline — so
we cache.

The cache lives in
[`claude_hooks/hyde_cache.py`](../claude_hooks/hyde_cache.py):

| Property | Default | Notes |
|---|---|---|
| Path | `~/.claude/claude-hooks-hyde-cache.json` | Plain JSON, atomic writes via `os.replace` |
| Key | `sha256(model || \x00 || grounding || \x00 || prompt)` | Grounding context is part of the key, so two raw-recall snapshots don't collide |
| TTL | 24 h (`hyde_cache_ttl_seconds`, 86400 s) | Past TTL is treated as miss |
| Capacity | 200 entries (`DEFAULT_MAX_ENTRIES`) | LRU eviction by `ts` when exceeded |
| Concurrency | last-writer-wins | Two hooks racing on the same key: file never corrupts (atomic replace), one expansion is dropped, both turns succeed |

Cache hits are logged at DEBUG (`hyde cache HIT` / `hyde (grounded)
cache HIT`); turn on `logging.level: debug` in
`config/claude-hooks.json` to see them.

### Invalidating the cache

The cache is grounded by raw memory snippets — so when the underlying
memories change in a way that should change expansions, blow it away:

```bash
rm ~/.claude/claude-hooks-hyde-cache.json
```

The pipeline regenerates entries on the next miss. Reasons you might
want to clear:

- After a `/reflect` or `/consolidate` run that rewrote stored
  memories — old expansions ground against deleted text.
- After switching `hyde_model` — the same key under a different
  model would still hit (model is in the key) but old entries
  with the previous model linger and waste the LRU slot.
- After deciding the cache picked up a bad expansion that's hurting
  recall quality.

`hyde_cache.clear()` is also exported for tests / future
self-cleaning hooks.

## Interaction with `metadata_filter`

`metadata_filter` (port 2 from thedotmack/claude-mem) gates recall by
metadata: `cwd_match`, `observation_type`, `max_age_days`,
`require_tags`. When enabled, the pipeline:

1. Asks each provider for `recall_k * over_fetch_factor` hits (default
   `4`) instead of the plain `recall_k`.
2. Filters by metadata.
3. Caps at `recall_k`.

HyDE runs **after** metadata filtering, on the survivors. That means:

- Grounded HyDE's grounding context only contains memories that
  matched the metadata filter — expansions inherit your CWD/type/age
  scope automatically.
- If the metadata filter is too narrow and returns zero hits, HyDE
  is skipped entirely (because `total_raw == 0`). Loosen the filter
  before suspecting HyDE.
- The refined recall in step 3 also goes through metadata filtering
  — the over-fetch factor is reapplied per recall pass.

In short: `metadata_filter` constrains the candidate pool; HyDE
re-ranks within the pool. They compose cleanly.

## Configuration knobs

> **v1.4+**: the keys below are set interactively by
> `install.py`'s `_setup_ollama_chat` dialog (Ollama chat URL,
> HyDE model + fallback + `num_ctx`, plus the shared-skills
> shortcut for `/reflect` and `/consolidate`). Pre-v1.4 they were
> hard-coded `config.py` defaults; v1.4 closes that gap. Hand-edit
> the JSON below for anything the dialog doesn't expose.

Under `hooks.user_prompt_submit` in `config/claude-hooks.json`:

| Key | Default | Purpose |
|---|---|---|
| `hyde_enabled` | `false` | Master switch |
| `hyde_grounded` | `true` | Grounded expansion (recommended) vs plain |
| `hyde_ground_k` | `3` | Top-N raw memories to feed the LLM as grounding |
| `hyde_ground_max_chars` | `1500` | Cap on the grounding block (per-entry cap = `max_chars / k`, min 200) |
| `hyde_grounding_provider` | unset | Force a specific provider (e.g. `pgvector`); auto-pick by default |
| `hyde_model` | `gemma4:e2b` | Primary Ollama model |
| `hyde_fallback_model` | `gemma4:31b-cloud` | Tried if primary fails. Cloud model on Ollama's free tier so no quota cost; strict capability bump from the local primary |
| `hyde_url` | `http://localhost:11434/api/generate` | Ollama generate endpoint |
| `hyde_timeout` | `30.0` | **Per-model** timeout in seconds — applied independently to the primary and the fallback call (see hook-cap note below) |
| `hyde_max_tokens` | `150` | Cap on expansion length (`num_predict`) |
| `hyde_keep_alive` | `"15m"` | How long Ollama keeps the model resident after the call. `"-1"` = never unload |
| `hyde_cache_enabled` | `true` | Set `false` to disable cache (e.g. when comparing expansions) |
| `hyde_cache_ttl_seconds` | `86400` | TTL for cached expansions |

### Hook cap must cover the whole chain

`hyde_timeout` is **per model**, but the expansion runs the primary and the
fallback *sequentially* inside one synchronous hook. The hook's wall-clock cap
(`timeout` on the `UserPromptSubmit` / `SessionStart` entries in
`~/.claude/settings.json`) is what bounds the *total*, and Claude Code SIGTERMs
the hook the moment it elapses. So the cap must satisfy:

```
hook timeout  >=  hyde_timeout * 2  +  recall overhead (~3-5 s)
```

With the default `hyde_timeout = 30`, both recall events therefore ship a
**65 s** cap (`install.py` `HOOK_TEMPLATE`). A tighter cap silently strangles
the fallback: the primary eats the budget, the fallback gets near-zero, and you
see `hyde (grounded): all models failed` even though the local model was healthy
and would have answered given its own window (notably on a cold start, which
needs well over 10 s). The warm/cache path is unaffected — the hook returns as
soon as the primary answers (~0-4 s); the long cap only bites when the cloud
primary actually stalls. If you lower `hyde_timeout`, you can lower the caps to
match; if you raise it, raise the caps too.

### The "recall overhead" term is only bounded by `max_query_chars`

The inequality above quietly assumes recall overhead is a small constant.
Until v1.14.1 it wasn't: nothing capped the *length* of the query, so the
term scaled with whatever the user pasted. `min_prompt_chars` was the only
length check in the pipeline, and neither `max_total_chars` (which bounds
the injected **output**) nor `hyde_ground_max_chars` (which bounds the
**grounding memories**) touches the prompt.

That made a large prompt a total recall loss, and a silent one. Step 1 of
`run_recall` embeds the raw query *before* HyDE is reached, so the timeout
was rarely HyDE's fault at all. Measured on solidpc against the CPU
llamafile embedder:

| query | embed | vs the 65 s cap |
|---|---|---|
| 1 000 chars | 1.4 s | fine |
| 5 000 chars | 9.0 s | fine |
| 16 000 chars | 67.6 s | **blown** |
| 30 000 chars (embedder `max_chars`) | ~300 s | blown 5× over |

The embedder's own timeout is **180 s**, far above the hook cap, so it
never gives up first — Claude Code SIGTERMs the hook and discards the
output. You lose the entire recall for that turn and see only
`hook timed out after 65s`.

`max_query_chars` (default **3500**, `0` disables) closes it, sized for
the worst of the density range rather than for average prose: ~1 100
tokens of prose at 3.2 chars/token, ~2 600 tokens of base64 at 1.35 —
both comfortably inside 65 s even on pandorum (~2× slower).

The clamp is deliberately not `query[:3500]`. A long prompt carries its
framing at the top and its actual ask at the bottom, with pasted bulk in
between, so it keeps **both ends** (65 % head / 35 % tail joined by `…`),
cuts on paragraph → line → sentence → word boundaries rather than
mid-word, and **squeezes** long fenced blocks to their opening lines
instead of dropping them — for "here is the traceback, what is it?" the
exception line at the top of the block is the highest-signal string in
the prompt. Dropping the middle also helps recall *quality*: a 20 KB
query collapses into one mushy centroid vector whose nearest neighbours
drift generic.

A clamp logs at INFO (`query clamped for recall: 46604 -> 3338 chars`),
so thin recall on a long prompt is explainable rather than mysterious.

## Failure modes

The whole HyDE step is best-effort:

- Ollama unreachable → fall back to the fallback model → if that also
  fails, fall back to the raw prompt. The raw recall has already
  populated `additionalContext`, so the user still gets memories,
  just without the boost.
- LLM returns < 10 chars or empty → treated as failure, fall back.
- Cache file corrupt or unreadable → silently treated as miss; the
  fresh expansion overwrites the corrupt file.
- Cache write fails (read-only filesystem, full disk) → logged at
  DEBUG, recall continues uncached.

The hook always exits 0. HyDE failure never blocks the prompt.

## Tuning

A small playbook for picking knobs:

- **Latency tight, recall ok** — leave `hyde_enabled: false`. Raw
  recall is already useful with `recall_k: 5`.
- **Recall quality > latency** — set `hyde_enabled: true`,
  `hyde_grounded: true`, `hyde_keep_alive: "-1"`. Cache hits make
  most turns latency-neutral; misses pay 0.5–4 s once.
- **Niche / jargon-heavy project** — keep grounded mode. Plain HyDE
  on a jargon-heavy project is worse than no HyDE.
- **Very high prompt diversity** — bump `DEFAULT_MAX_ENTRIES` (edit
  `hyde_cache.py`; not yet exposed as config) to 500–1000 if you see
  the LRU evict useful entries before TTL.
- **Want to confirm HyDE is helping** — run `/reflect rerank` (or
  whatever your recall-quality gate is) before/after toggling. The
  cache makes A/B latency comparisons noisy; clear it between runs.
