# Episodic search eval

Measures how well episodic search finds past work.

- `queries.json` — 28 queries (22 phrased as questions, 6 naming an id), each
  seeded by a `marker`: a string that appears in the conversations where that
  work happened.
- `eval.mjs` — runs the package's own `searchConversations()` against a
  database (the live index, or a copy via `--db`) and scores each mode by the
  marker: rank of the first result from a conversation containing it.
- `variant.mjs` — builds a subset index (every relevant conversation + seeded
  distractors) with a chosen text composition (`raw` / `clean` / `chunk`) and
  embedding model (local ONNX, or Ollama `--embed-url`), and scores vector /
  BM25 / RRF on it.
- `reembed.mjs` — re-embeds a copy of an index with a given embedder through
  the package's own migration (space switch + grouped re-embedding), without
  `sync`; `--pkg` points at a separate build to try unreleased code.
- `judge.py` — pools the top 10 of every run per query and has a model rate
  each pooled exchange once (2 exact, 1 related, 0 not; cached), then scores
  every run on the same judgments: P@10, nDCG@10, exact@10, MRR. The marker
  metric is lexical and undercounts semantic hits; the judged one is the one
  to decide on.

```bash
node benchmarks/episodic/eval.mjs --db <copy of db.sqlite> --modes vector,text,both --json out.json
python benchmarks/episodic/judge.py --db ~/.config/superpowers/conversation-index/db.sqlite \
    --cache judgments.json --model glm-5.3 out.json [more.json ...]
```

Run on a copy: the first open of an older database builds its keyword index.
Judging uses Ollama Cloud: claim `ollama-cloud` first.

## Results (2026-11-26, solidpc, full index: 71,807 exchanges)

| Default search | P@10 | nDCG@10 | exact@10 | MRR | marker hit@10 |
|---|---|---|---|---|---|
| bge-small, vector then unranked LIKE hits appended (before) | 0.26 | 0.24 | 0.36 | 0.52 | 0.32 |
| bge-small, vector + BM25 fused (RRF) | 0.41 | 0.41 | 0.61 | 0.68 | 0.64 |
| qwen3-embedding:0.6b, clean windows, vector only | 0.48 | 0.45 | 0.61 | 0.65 | 0.57 |
| qwen3-embedding:0.6b, clean windows, vector + BM25 fused | 0.52 | 0.55 | 0.68 | 0.77 | 0.71 |
| **+ unindexed conversations caught up, `claude -p` runs out, one result per conversation (fused)** | **0.53** | **0.55** | **0.82** | **0.95** | **0.89** |
| same, vector only | 0.41 | 0.41 | 0.75 | 0.78 | 0.82 |
| **the live index after its catch-up (2026-11-27, 84,496 exchanges), fused** | **0.54** | **0.55** | **0.82** | **0.95** | **0.89** |
| same, vector only | 0.42 | 0.42 | 0.75 | 0.78 | 0.82 |

The live rows are a SQLite backup of the production index taken after
`episodic_doctor --rebuild-embeddings` caught it up (0 transcripts owed),
queried through the production CPU llamafile (0.48 s fused, 0.38 s vector)
and judged in the same pool as the bench copy. Marker MRR on the live
index: 0.75 fused against the baseline's 0.24.

Judged in one pool (glm-5.3, 2026-11-27, with the rows above re-scored in
it: the old fused bge row reads 0.41 / 0.38 / 0.61 / 0.68 there). The last
two rows are a copy of the index after the one-time catch-up (17,838
exchanges that had never been indexed, 36 min on GPU Ollama) and the purge
of 5,193 spawned conversations: 84,365 exchanges, 129,529 vectors. The
qwen3 row before it scored partly on Caliber runs that quote the project's
docs: for 25 of the 28 queries those were the only indexed conversations
holding the marker, because the sessions where the work happened had never
been indexed. Without the catch-up, removing them alone drops the marker
hit@10 to 0.39.

All four rows judged in one pool (glm-5.3); nDCG depends on the pool, so the
fused bge row reads 0.47 against the smaller pool of its first run. Id
queries (`ch-28`, `c2f2caa`, …): 1 of 6 in the top 10 before fusion, 6 of 6
with it. The qwen3 rows are a copy of the live index re-embedded with
`reembed.mjs` on the GPU (Ollama, batch 64): 111,075 vectors in 108 min.
Queries went through the production CPU llamafile
(`EPISODIC_MEMORY_EMBEDDER` = `http://127.0.0.1:38092/embedding`): 0.36 s
per vector search, 0.79 s fused. Ollama GPU and the llamafile load the same Q8_0 weights; minimum
cosine between them over the probes is 0.998.

### Text and model variants (subset index: 15,144 exchanges, 45 relevant conversations)

| Variant (`variant.mjs`) | P@10 | nDCG@10 | exact@10 | MRR |
|---|---|---|---|---|
| bge-small, windows, fused | 0.43 | 0.48 | 0.61 | 0.77 |
| qwen3, raw exchange text, fused | 0.42 | 0.46 | 0.57 | 0.72 |
| qwen3, clean text (no harness turns / reminders / tool names), fused | 0.46 | 0.50 | 0.64 | 0.73 |
| **qwen3, clean text in overlapping windows, fused** | **0.52** | **0.53** | 0.57 | **0.77** |
| BM25 alone | 0.42 | 0.49 | 0.57 | 0.78 |
| qwen3, windows, vector only | 0.45 | 0.42 | 0.57 | 0.63 |
| bge-small, windows, vector only | 0.31 | — | — | 0.59 |

The windows (`src/exchange-text.ts`) won: one vector per question + answer
start, then answer windows tagged with the question, at most 8 per exchange.
Raw text loses to clean text because harness turns (`<system-reminder>`,
task notifications) dominate the embedding of short exchanges.
