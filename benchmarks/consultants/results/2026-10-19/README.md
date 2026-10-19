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

`compaction_bench.py` task v2, all five arms × 4 trials, qwen3.6 27B on
eleven2go xOllama, window pinned at 32,768, commit 5b60e8a. Every trial
folded 3+ times except two.

| arm | fixed | decisecond unit | ×100 fix | ticket |
|---|---|---|---|---|
| reduced/system | 0/4 | 2/4 | 0/4 | 4/4 |
| reduced/user | 0/4 | 0/4 | 0/4 | 4/4 |
| full/system | 2/4 | 3/4 | 2/4 | 4/4 |
| full/user | 1/4 | 1/4 | 1/4 | 1/4 |
| basic/system | 1/4 | 1/4 | 1/4 | 3/4 |

**Placement:** `system` is at least as good as `user` in both flows (full
2/4 vs 1/4; reduced 0/4 vs 0/4 but 2/4 vs 0/4 on the unit). n = 4, so this
is weak evidence, and it points the same way as the default: keep `system`.

**Not asked, but found:** the default `reduced` flow fixed 0 of 8 trials,
below `full` (3/8) and below `basic` (no model call, 1/4). Its replay
keeps the follow-up ticket (8/8) but loses the once-readable ×100
conversion. That puts §11.1 (reduced by default, 2026-10-10) back in front
of the owner; nothing was changed.
