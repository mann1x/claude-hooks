# `deepseek-v4-flash-cloud` — caliber-eval summary (2026-05-09)

**Headline: 90/100 (Grade A) in 16m 6s.** Highest non-claude-cli score
ever (beats gemma-native-tools-v3's 87 and gemma4:31b-cloud's 85), and
the **only model in the cohort that emits `file:line` references** in
skill bodies (25 of them in the lone `get-advice` skill — first non-zero
count). But the rubric flagged **0/8 on "References point to real
files"**: spot-check shows ~5 of every 8 file refs are hallucinated or
mis-rooted (`cli.py:347` instead of `claude_hooks/get_advice/cli.py:347`),
which costs the rubric points but doesn't drag the headline score below A.

## Run setup

| Field | Value |
|---|---|
| Workspace | `/srv/dev-disk-by-label-opt/dev/caliber-eval/deepseek-v4-flash-cloud/` |
| Source | `claude-hooks` @ `db448b6` (post-resilience-port + thought_signature passthrough) |
| Caliber | `1.49.6` |
| Provider | `openai` (via grounding proxy on `127.0.0.1:38091`) |
| Model | `deepseek-v4-flash:cloud` |
| Wall clock | **16m 6s** |
| Caliber score | **90/100 (Grade A)** |
| Refinement | 35 → 90 (+55 pts), F → A — biggest score-refine delta in the cohort |
| Validating phase | "Passed" (no further refinement needed; vs others "Refined" / "Fixing N issues") |

## Phase breakdown

| Phase | Wall | Notes |
|---|---|---|
| Detecting project stack | 1m 38s | ✓ Python, Markdown, JSON, Shell, … |
| Generating configs      | 4m 24s | ✓ "Ensuring all paths in output…" |
| Generating skills       | **7m 24s** | ✓ 1 skill (`get-advice`) |
| Validating & refining   | 2m 39s | ✓ Passed (no fixes required) |
| **Total** | **16m 6s** | rc=0 |

## Headline metrics — full cohort

| Metric | claude-cli | gemma-v3 | gemma4-31b | gemini-flash | **deepseek-v4-flash** |
|---|---|---|---|---|---|
| Caliber score | **94** (A) | 87 (A) | 85 (A) | 77 (B) | **90 (A)** |
| Wall          | 37m 14s | 14m 32s | 28m 41s | **4m 44s** ⚡ | 16m 6s |
| Total skills  | 8 | 4 | 5 | 7 | 4 |
| Project skills | 5 | 1 | 2 | 4 | **1** |
| `paths:` fm coverage | 5/5 | 1/1 | 2/2 | 4/4 | **1/1** (100%) |
| `paths:` fm entries | 25 | 5 | 4 | 15 | 1 |
| Bare file refs | 48 | 6 | 8 | 9 | 3 |
| **`file:line` refs** | **0** | **0** | **0** | **0** | **25** ⭐ |
| Body chars total | 56,031 | 15,891 | 18,316 | 18,948 | 20,412 |
| Hallucinated refs | 0 | 0 | 0 | 4 | **~16** (spot-check 5/8 hit rate × 25) |
| `CLAUDE.md` chars | 9,109 | 4,693 | 6,104 | 4,476 | **12,839** ⭐ |
| `CLAUDE.md` bare refs | 54 | 6 | 25 | 6 | **44** |
| Tokens | n/a | n/a | 364k / 11.9k / 8 | 373k / 16.5k / 10 | **571k / 36k / 9** |

⭐ first non-zero `file:line` count, densest CLAUDE.md, most output tokens

## What deepseek got right

1. **Score 90/A — best non-claude-cli result.** Validation passed without
   needing any score-refine fixups (vs gemma-v3's `score-refine` 3-issue
   loop, gemini's `Fixing 3 scoring issues`, gemma4-31b's "Refined"
   rewrites). The first-pass output was already at A grade.

2. **Densest CLAUDE.md in the cohort by far.** 12,839 chars + 44 bare
   file refs vs claude-cli baseline's 9,109 / 54. That's a *bigger*
   CLAUDE.md than the reference, with comparable density of grounded
   references. Project-grounding rubric apparently liked it (no
   "Project grounding" deduction in the still-failing list).

3. **Only model emitting `file:line` references.** The `get-advice`
   skill body has 25 of them — `claude_hooks/get_advice/state.py:5`,
   `claude_hooks/get_advice/config.py:41-46`, etc. **No other model in
   the cohort hits non-zero on this metric.** Caliber's rubric doesn't
   currently grade for `file:line` precision but a downstream consumer
   (a code-review agent, an auditor) gets visible benefit from this
   shape.

4. **571k input tokens — heaviest project ingestion in the cohort.**
   The model actually *reads* the project deeply rather than skimming.
   Reflected in CLAUDE.md depth and skill specificity.

## Where the 10-point gap to baseline lives

| Category | Score | Note |
|---|---|---|
| **References point to real files** | **0/8 (−8)** | The rubric checked the cited paths and found them missing. Spot-check on `get-advice`'s 25 `file:line` refs: of 8 unique paths sampled, **3 exist (`ctx_probe.py`, `state.py`, `cli.py` under `claude_hooks/get_advice/`) and 5 are hallucinated or bare-rooted** (`cli.py:347` without dir prefix, `scripts/train.yaml`, `chat_client.py`, `ctd/alignment.py`). A model with the *capability* to emit precise refs but not the discipline to verify them. |
| MCP servers configured | 0/0 | N/A |
| AGENTS.md exists | 0/1 | not generated |
| Learned content present | 0/2 | no `caliber learn install` |
| External sources configured | 0/0 | N/A |

The 8-pt loss on `References` is the entire 10-pt gap to a 100/A score
(the other categories are 0/0 N/A or 0/1 minor). **If deepseek learned
to verify refs before emitting them, it would hit ~98/100.**

## Skill produced

| Skill | Body chars | `paths:` | Bare refs | `file:line` | Built-in? |
|---|---|---|---|---|---|
| `get-advice` | 8,874 | 1 | 2 | **25** ⭐ | no |
| `find-skills` | 1,963 | 0 | 0 | 0 | yes |
| `save-learning` | 2,129 | 0 | 0 | 0 | yes |
| `setup-caliber` | 7,446 | 0 | 1 | 0 | yes |

Only one project-specific skill (vs gemini's 4, gemma4-31b's 2). But that
one skill is MASSIVE — 8,874 chars, 25 file:line refs. Deepseek picked
**depth over breadth**: detailed coverage of one feature (`get-advice`,
the LLM-to-LLM advisor module) instead of shallow coverage of multiple.

`get-advice` is a defensible pick: it's the second-newest project
feature (added v1.1, May 2026), uses the `agent_loop` runner shared
with consultants, and has its own `chat_client.py` which is exactly
the file deepseek needed to talk about cloud resilience.

## Cloud weather during the run

- **89 chat completions**
- **0 5xx**, **0 retryable 4xx**, **0 thought_signature 400s**, **0 empty content**
- Cleanest cloud weather of any cohort run — no resilience-layer
  intervention needed at all.

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

A negative result for the resilience layer in the sense that none of it
fired — but a positive verification that the wrappers are zero-overhead
on a clean run.

## Verdict

**PROD-screen** for caliber init at this baseline.

- Pick over gemma4-31b-cloud when **score matters more than skill
  count** — deepseek lands 90 vs 85 with one skill, gemma lands 85
  with two; neither hallucinates much in skill bodies.
- **Don't trust deepseek's `file:line` refs without verification.**
  ~60% hit rate on the spot check is too low for a hardening PR
  reviewer. Use the `get-advice` skill as a starting point and
  hand-fix the refs.
- **The 12.8 KB CLAUDE.md is genuinely better than baseline.** Worth
  diff-comparing against the claude-cli output to see if any phrasing
  could be backported into a caliber-prompt update for other models.
- Operationally: 16m wall is in the middle of the cohort — not the
  fastest (gemini 4m44s, v3 14m32s) but the highest score from any
  cloud model.

## Cross-references

- Resilience port: claude-hooks commit `aeca4ae`
- thought_signature passthrough: claude-hooks commit `8734716`
- Consultants benchmark on this same model:
  [`docs/benchmarks/deepseek-v4-flash-cloud-2026-05-09/`](../../shared/dev/claude-hooks/docs/benchmarks/deepseek-v4-flash-cloud-2026-05-09/)
  (PROD-READY at smoke + audit-medium; Q3 reco lands C+ on
  v1.2 actionability — same UX-flip pattern observed elsewhere)
