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
| vector, then unranked LIKE hits appended (before) | 0.26 | 0.26 | 0.36 | 0.52 | 0.32 |
| vector + BM25, reciprocal rank fusion | 0.41 | 0.47 | 0.61 | 0.68 | 0.64 |

Same embeddings (bge-small-en-v1.5) in both rows. Id queries (`ch-28`,
`c2f2caa`, …): 1 of 6 in the top 10 before, 6 of 6 after.
