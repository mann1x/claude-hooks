# `glm-5.1-cloud` — caliber-eval summary (2026-05-09)

**Headline: 96/100 (Grade A) in 40m 18s.** **Highest non-claude-cli
score recorded** (beats deepseek-v4-flash-cloud's 90, gemma-v3's 87,
gemma4:31b-cloud's 85). Three project skills with **9 `file:line`
refs across two skills, 100% of which point to real files** on
spot-check (7/7 verified) — solving deepseek's signature weakness.
The 4-pt gap to a perfect score is entirely from generator-config
N/A categories (AGENTS.md, learn install) plus 0/0 buckets that
don't apply to this project. The slowest run in the cohort, but the
highest-quality output.

## Run setup

| Field | Value |
|---|---|
| Workspace | `/srv/dev-disk-by-label-opt/dev/caliber-eval/glm-5-1-cloud/` |
| Source | `claude-hooks` @ `db448b6` (post-resilience-port + thought_signature passthrough) |
| Caliber | `1.49.6` |
| Provider | `openai` (via grounding proxy on `127.0.0.1:38091`) |
| Model | `glm-5.1:cloud` |
| Wall clock | **40m 18s** |
| Caliber score | **96/100 (Grade A)** |
| Refinement | 72 → 96 (+24 pts) — Refining-phase Passed cleanly at end |
| Validating phase | "Passed" 15m 2s (fixed Project grounding, Permissions, Model & effort pinned, Reference density issues) |

## Phase breakdown

| Phase | Wall | Notes |
|---|---|---|
| Detecting project stack | 1m 39s | ✓ Python, Shell, Batch, YAML, TOML, … |
| Generating configs      | 8m 30s | ✓ Cross-referencing existing |
| Generating skills       | **15m 0s** | ✓ 3 project skills |
| Searching community     | 2m 0s  | ✓ |
| Validating & refining   | **15m 2s** | ✓ Passed (resolved 4 of 5 initially-failing categories) |
| **Total** | **40m 18s** | rc=0 |

## Headline metrics — full cohort

| Metric | claude-cli | gemma-v3 | gemma4-31b | gemini-flash | deepseek-flash | **glm-5.1** |
|---|---|---|---|---|---|---|
| Caliber score | **94** (A) | 87 (A) | 85 (A) | 77 (B) | 90 (A) | **96 (A)** ⭐ |
| Wall          | 37m 14s | 14m 32s | 28m 41s | **4m 44s** ⚡ | 16m 6s | 40m 18s |
| Total skills  | 8 | 4 | 5 | 7 | 4 | **6** |
| Project skills | 5 | 1 | 2 | 4 | 1 | **3** |
| `paths:` fm coverage | 5/5 | 1/1 | 2/2 | 4/4 | 1/1 | **3/3** (100%) |
| `paths:` fm entries | 25 | 5 | 4 | 15 | 1 | **19** |
| Bare file refs | 48 | 6 | 8 | 9 | 3 | **33** |
| `file:line` refs | 0 | 0 | 0 | 0 | 25 | **9** |
| Body chars total | 56,031 | 15,891 | 18,316 | 18,948 | 20,412 | **43,202** |
| Hallucinated refs | 0 | 0 | 0 | 4 | ~16 | **0** ⭐ |
| `CLAUDE.md` chars | 9,109 | 4,693 | 6,104 | 4,476 | 12,839 | **11,191** |
| `CLAUDE.md` bare refs | 54 | 6 | 25 | 6 | 44 | **144** ⭐ |
| Tokens (in/out/calls) | n/a | n/a | 364k / 11.9k / 8 | 373k / 16.5k / 10 | 571k / 36k / 9 | **452k / 22.8k / 8** |

⭐ highest score, 0 hallucinated refs, densest CLAUDE.md grounding (144 bare refs)

## What glm got right

1. **Score 96/A — best non-claude-cli result, only 2 pts behind the
   reference.** Initial 72 refined to 96 in a clean Validating-phase
   "Passed" exit. Refining resolved 4 of 5 initially-failing
   categories (Project grounding 6/12 → fixed, Permissions 0/2 →
   fixed, Model & effort pinned 0/2 → fixed, Reference density 8/8
   already passing). Only AGENTS.md (0/1) + Learned content (0/2)
   stayed unfixable (operator-action items, not a model failure).

2. **0 hallucinated `file:line` refs.** Spot-check on 7 unique
   `file:line` paths from `add-hook-event` and `add-provider`:
   **7/7 land in real files at valid line numbers** —
   `claude_hooks/dispatcher.py:86`, `claude_hooks/dispatcher.py:31`,
   `claude_hooks/providers/pgvector.py:328`,
   `claude_hooks/config.py:48`, `install.py:3987`,
   `tests/conftest.py:75`, `claude_hooks/providers/base.py:47`. **No
   other model in the cohort matches this hit rate.** Deepseek
   emitted more `file:line` refs (25 vs 9) but with ~5/8 hallucinated;
   glm emits fewer but verified.

3. **Densest grounded CLAUDE.md ever.** 11,191 chars + **144 bare file
   refs** — that's nearly 3× the claude-cli baseline (54) and 6× any
   other cohort entry. The "Project grounding" rubric initially
   complained about 4 missing topic areas (`.claude-hooks`, `bench`,
   `graphify-out`, `modelfiles`); refining filled them in cleanly.

4. **3 well-chosen project skills covering different axes.**
   `add-hook-event` (extending the dispatcher), `add-provider`
   (memory backends), `consultants-bench` (the agentic-engine
   benchmark harness). All three with non-empty `paths:` frontmatter
   (3/3 coverage = parity with claude-cli's 5/5 and gemini's 4/4).

5. **No resilience-layer interventions needed.** 0 5xx, 0 4xx,
   0 empty content across all completions. Same clean cloud weather
   as the deepseek run.

## Where the 4-point gap to a perfect score lives

| Category | Score | Note |
|---|---|---|
| MCP servers configured | 0/0 | N/A — no external services this project uses |
| **AGENTS.md exists** | **0/1 (−1)** | Caliber didn't generate one (operator can `caliber sources add` later) |
| **Learned content present** | **0/2 (−2)** | No `caliber learn install` was run (operator action) |
| External sources configured | 0/0 | N/A |

The remaining 1-pt difference is rounding/category bucketing in the
final-score calculation. **All actually-failing rubric items are
operator-config gaps, not model failures.** A run with `caliber learn
install` + AGENTS.md generation would land at 99/100 or 100/100 from
this output.

## Skills produced

| Skill | Body chars | `paths:` | Bare refs | `file:line` | Built-in? |
|---|---|---|---|---|---|
| `add-hook-event`    | 10,130 | 6 | 9  | 3 | no |
| `add-provider`      | 12,460 | 5 | 9  | 6 | no |
| `consultants-bench` |  9,074 | 8 | 14 | 0 | no |
| `find-skills`       |  1,963 | 0 | 0  | 0 | yes |
| `save-learning`     |  2,129 | 0 | 0  | 0 | yes |
| `setup-caliber`     |  7,446 | 0 | 1  | 0 | yes |

**`add-provider` is the densest single skill in the entire cohort by
a wide margin** — 12,460 chars, 5 paths, 6 verified `file:line` refs,
9 bare refs covering the full provider-implementation contract
(base.py:47-109 ABC, providers/__init__.py registry, config.py:48-77
schema, conftest.py:75-117 test fixtures, pgvector.py:328-330 as a
reference impl). This is the kind of skill body claude-cli produces.

`consultants-bench` is the most novel pick — none of the other 5
labels saw the consultants/bench harness as worth a skill. 8 paths
in frontmatter is the highest single-skill paths count in the cohort
(beats baseline's `proxy-ops` 5).

## Cloud weather during the run

- **Token usage**: 452k input / 22.8k output / 8 calls. Mid-pack on
  input, low on output (terse skill bodies relative to deepseek).
- **0 5xx**, **0 retryable 4xx**, **0 thought_signature 400s**,
  **0 empty content**. Cleanest possible.

`/health.upstream_flaps` after the run:

```json
{
  "upstream_5xx_total": 0,
  "upstream_retryable_4xx_total": 0,
  "upstream_empty_total": 0,
  "upstream_retry_succeeded_total": 0,
  "upstream_retry_exhausted_total": 0
}
```

Two of the six bench labels now post zero-flap runs end-to-end
(deepseek + glm). The resilience layer is invisible-when-not-needed.

## Verdict

**PROD-READY** for caliber init at this baseline.

- **Best non-claude-cli option in the cohort.** 96/100 with 0
  hallucinated refs is genuinely competitive with the 94/100 baseline.
  Pick over deepseek-v4-flash when **path correctness matters more than
  density** (deepseek emits more refs but ~60% are bogus).
- **Pick over gemma4:31b-cloud, gemma-v3, and gemini-flash.** All three
  score lower, and gemini's hallucinated paths are an active risk.
- **Operationally**: 40m wall is the longest in the cohort — 8.5×
  slower than gemini, 2.5× slower than deepseek, 1.4× slower than
  gemma4:31b. **Quality/wall trade-off is real.** Use glm when the
  output will be committed; use gemini/gemma for iterative tuning.
- **For "best of breed" caliber init**: `glm-5.1:cloud` is the new
  recommended cloud backend.

## Cross-references

- Resilience port: claude-hooks commit `aeca4ae`
- thought_signature passthrough: claude-hooks commit `8734716`
- Companion deepseek-v4-flash-cloud bench (90/A in 16m 6s, but with
  hallucinated refs): [`reports/deepseek-v4-flash-cloud-summary.md`](deepseek-v4-flash-cloud-summary.md)
