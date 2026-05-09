# `gemma-native-tools-v3` — caliber-eval summary (2026-04-30)

**Headline:** Pre-cloud local-Ollama gemma4-98e Modelfile (Q6_K, 256k
ctx, 24 GB VRAM) running through the caliber-grounding-proxy.
**87/100, Grade A, wall 14m 32s.** Until 2026-05-09 the strongest
non-claude-cli result by score, and still the **fastest** caliber init
on this codebase. Filed upstream as
[`caliber-ai-org/ai-setup#205`](https://github.com/caliber-ai-org/ai-setup/issues/205)
where the 7-point gap is argued to be largely structural (caliber's
"Project grounding" metric uses every surveyed file as denominator —
~241 entries — capping local-context models at ~21%).

> Recovered from the workbench archive on 2026-05-09 — the run was
> never published into `docs/caliber-eval-results/` at the time.
> This summary backfills it.

## Run setup

| Field | Value |
|---|---|
| Workspace | `/srv/dev-disk-by-label-opt/dev/caliber-eval/gemma-native-tools-v3/` |
| Provider | `openai` (via grounding proxy) |
| Model | `gemma4-98e:native-tools` (custom Modelfile, Q6_K, 256k ctx, 24 GB VRAM) |
| Pre-resilience-port? | Yes — proxy was on pre-port code (no `upstream_flaps` counters yet) |
| Wall clock | **14m 32s** (vs claude-cli 37m 14s — 61% faster) |
| Caliber score | **87/100 (Grade A)** (vs claude-cli 94/100 — gap 7 pts) |
| Refinement | 40 → 87 (+47 pts), D → A in 14m 32s total |
| Generation completed | 872,797 ms (≈14m 32s) |

## Headline metrics — vs baseline + later cloud variant

Re-scored 2026-05-09 with the current `score.py` for parity with the
gemma4:31b-cloud row:

| Metric | claude-cli | **gemma-native-tools-v3** | gemma4:31b-cloud |
|---|---|---|---|
| Caliber score   | 94/100 | **87/100** | 85/100 |
| Wall            | 37m 14s | **14m 32s** | 28m 41s |
| Total skills    | 8 | 4 | 5 |
| Project skills  | 5 | 1 (`proxy-ops`) | 2 |
| `paths:` fm coverage | 5/5 (100%) | 1/1 (100%) | 2/2 (100%) |
| `paths:` fm entries | 25 | 5 | 4 |
| Bare file refs  | 48 | 6 | 8 |
| `file:line` refs | 0 | 0 | 0 |
| Body chars total | 56,031 | 15,891 | 18,316 |
| `CLAUDE.md` chars | 9,109 | 4,693 | 6,104 |
| `CLAUDE.md` bare refs | 54 | 6 | 25 |

## What v3 got right

1. **Cleanest non-claude-cli `paths:` fm to date.** The single project
   skill `proxy-ops` has **5** `paths:` entries (vs the cloud variant's
   2) and 5 bare file refs in the body — densest per-skill grounding
   in the cohort.
2. **Fastest end-to-end run on this codebase, period.** 14m 32s beats
   claude-cli (37m 14s) and gemma4:31b-cloud (28m 41s) by a wide
   margin. Local Ollama, no cloud RTT, fits in a single 256k ctx
   window.
3. **Score-refine convergence.** 40 → 87 in one pass, +47 pts —
   biggest delta the rubric has seen on this project.

## Where the 7-point gap lives (per issue #205)

Caliber's `Project grounding` rubric (12 pts) uses **every individual
file in the survey** as denominator — about 241 entries on this
codebase. Local models with smaller context windows physically
cannot reference all of them inline, capping coverage at ~21% no
matter how dense the output is. Cloud models with larger contexts
reach 50–60% and bank the difference. The author's upstream proposal
(`#205`) is to refine the rubric to grade by *config-file size*
rather than absolute file count.

The remaining ~2 pts are a JSON-formatting tax (intermittent escaped
backslashes / trailing characters from the Modelfile), partially
mitigated by the proxy's `sanitized N chars of trailing junk after
JSON in assistant content` log handler.

Other still-failing categories at 87/100:

| Category | Score | Note |
|---|---|---|
| MCP servers configured | 0/0 | N/A on this project |
| AGENTS.md exists | 0/1 | Caliber didn't generate AGENTS.md |
| Learned content present | 0/2 | No `caliber learn install` artefacts |
| External sources configured | 0/0 | N/A |
| Project grounding | 0/12 → still 3/12 after refine | The 12-point ceiling cited above |

## Skills produced

| Skill | Body chars | `paths:` | Bare refs | `file:line` | Built-in? |
|---|---|---|---|---|---|
| `proxy-ops`     | 4,218 | **5** | **5** | 0 | no (project-specific) |
| `find-skills`   | 2,053 | 0 | 0 | 0 | yes (caliber) |
| `save-learning` | 2,174 | 0 | 0 | 0 | yes (caliber) |
| `setup-caliber` | 7,446 | 0 | 1 | 0 | yes (caliber) |

Compare to gemma4:31b-cloud (later run, 2026-05-09): 2 project skills
(`consultants-management`, `setup-compile-aware`) at `paths:` 2 each.
v3's lone `proxy-ops` is denser per-skill but covers fewer
architectural areas — a 1-vs-2 skill gap in a sub-architecture sense.

## Why this is filed at the same level as cloud variants

v3 was a **production-screen-grade** local run (not a flaky
half-failure like v4/v5 which crashed at "Generating configs ...
Model produced no output for 12m4s"). It belongs in the published
results table alongside the cloud variants so the cross-axis
comparison (cloud-A 85, local-A 87, baseline-A 94, fastest=local-A
14m 32s) is visible at a glance.

## Reproduce

The `gemma4-98e:native-tools` Modelfile lives in
[`modelfiles/`](../../shared/dev/claude-hooks/modelfiles/) (see
[`docs/gemma4-tool-use-notes.md`](../../shared/dev/claude-hooks/docs/gemma4-tool-use-notes.md)
for the engineering history). Workspace + log preserved at
`/srv/dev-disk-by-label-opt/dev/caliber-eval/gemma-native-tools-v3/`.
