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

## C — why `reduced` lost the fact (`compaction-v3/`, `compaction-v4/`)

**v3** (task v3: the `grep` leak closed, `answered` reported apart from
`fixed`, 4 trials × reduced / full / basic, on a first fix since
reverted): **0/12** in every arm, `basic` included. The v2 `basic` 1/4 was
the leak. Traced fold by fold, the cause was what the writer was fed,
not the model:

1. **The writer was never shown the task.** The cut keeps the head
   (system + question) verbatim, so the question is never folded and
   was never quoted to the writer. The fork pins its first prompt and
   shows it for exactly this reason (`agentic-compaction.ts:579-605`).
   With no task the writer took the prompt's example ("I am fixing the
   collision"), read the generated modules as corruption, planned to
   stub them, and dropped the decisecond unit as noise. Generation 2
   narrated 16 edits that were never made.
2. **The writer's key dropped calls of the span it was reading.** The
   fork evicts the merged ledger to `max(2000, 0.1 × target)` characters,
   about 6 entries at a 32k window, priced at the results the key does
   not show, then renumbers. Given 9 calls and a 6-entry key starting at
   the third, the writer narrated reading 20 modules it never read and
   quoted a `backoff.py` that does not exist.
3. The writer read 0.5 × the target (the fork: the summarizer's input
   limit), the half-critics had half the summary cap (the fork: all of
   it), and the generation was `1 + markers in history`, which is 2 at
   every fold after the first (the fork: previous + 1).

The first attempt (numbered transcript lines, a span-only key, result
shrinking, a harness note, prompt edits) was reverted. Fixes: the task
shown first, the span sized from the window, the full critic cap, the
key keeping every call of the span (budget applied only to what is
carried, owner-approved, sent to the fork), the generation chained.

**v4** (same setup, `unit_carried` per generation recorded):

| arm | fixed | decisecond unit | ×100 fix | ticket |
|---|---|---|---|---|
| full/system | **3/4** | 4/4 | 3/4 | 3/4 |
| reduced/system | 1/4 | 2/4 | 2/4 | 3/4 |
| basic/system | 0/4 | 0/4 | 0/4 | 4/4 |

`full` now carries the unit through every fold (5 of 5 in trial 1).
`reduced` still loses it: trial 1 carried it at generation 1 and lost it
at generation 2's rewrite (17 folds, 61 invalid citations, 72 minutes);
trial 3 lost it at generation 1. Open: trials with long runs of no-op
folds (the history over the trigger, the cut folding nothing) — trial 0
of the killed first v4 run went 17 turns without a fold, the prompt
growing to ~24.4k of 32k. `compaction-v4-killed/` is that run (2 trials,
stopped at a 30-min background limit).

**Reading:** `full` 3/4 against `reduced` 1/4 on the same fixes puts the
default flow (§11.1) in front of the owner; nothing was flipped.
