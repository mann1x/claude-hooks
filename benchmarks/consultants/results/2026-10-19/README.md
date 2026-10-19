# 2026-10-19 — G6 (typed role messages) and C §11.2

## G6 — prose against typed (`[protocol] typed`)

Both tiers ran on dev after 8611375 (G3–G5) on an isolated bench engine
(scratch HOME, own in-flight file / council index / sandbox registry, store
reaper off). Same models in both arms; the critic is `glm-5.3-flash:cloud`.

**`g6-detect/`** — planted-false-claims set, critic only, 36 trials, 0 errors.

| | tooled (prose) | tooled-typed |
|---|---|---|
| recall | 100% | 100% |
| precision | 34.9% | 38.5% |
| false positives | 28 | 24 |
| completion tokens / trial | 1614 | 2002 (+24%) |
| prompt tokens / trial | 2300 | 2471 (+7%) |
| wall | 212 s | 241 s (+14%) |

The false positives are mostly an oracle artefact: glm writes "✓ Correct"
lists on the all-true control, and naming a true claim inside a
`CORRECTIONS:` block scores as a false positive.

**`g6-cost/`** — full council, effort high, stale-doc question, 3 trials per arm.

| | typed off | typed on |
|---|---|---|
| LLM calls | 27 | 31 (+17%) |
| prompt tokens | 454k | 723k (+59%) |
| completion tokens | 15.3k | 25.0k (+64%) |
| wall | 1998 s | 2019 s |
| stale-doc facts right (of 12) | 12 | 11 (`base_delay_s` repeated the doc once) |

Per role, typed on raises the researcher's prompt tokens per call from
21.5k to 31.2k and its completion per call from 505 to 798, and the critic
runs 4.7 calls instead of 3.3. xOllama's motivating gain (cheaper councils)
does not reproduce here: on this model mix, typed costs more and is no more
correct.

The typed reports also exposed two G4 false notes (bug-1086): a quote that
spans `#` comment lines read as "not in the file", and a prose `symbol`
("`X comment`", "module docstring") read as an identifier.

**Reading:** keep `[protocol] typed = false` as the default. The owner
decides; nothing was flipped.

## C §11.2 — compaction message placement (`compaction-v2/`)

`compaction_bench.py`, all five arms × 4 trials, qwen3.6 27B on eleven2go
xOllama. See its `report.md` once written.
