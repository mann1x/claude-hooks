# `gemini-3-flash-preview-cloud` — caliber-eval summary (2026-05-09)

**Headline:** Fastest caliber init ever measured on this codebase by a
3× margin (4m 44s, vs gemma-native-tools-v3's 14m 32s and
gemma4:31b-cloud's 28m 41s). Most skills produced (4 project + 3
built-in = 7 total). But **77/100, Grade B** — knocked from A by
**hallucinated file paths** in skill bodies (caliber's
"References point to real files" rubric flagged 4 non-existent paths).
Same model that lands `C+ (off-topic)` on Q3 in the consultants
benchmarks — same grounding weakness shows up here.

This run is the first bench against the **resilience-aware proxy**
+ the **`thought_signature` passthrough fix**: Gemini's required
provider extra is preserved across the request/response round-trip
(`claude_hooks/caliber_proxy/ollama.py` commit `8734716`). Without
the fix, the bench died at 1m 38s with three `400 missing
thought_signature` errors. With the fix, **zero 4xx flaps** across
83 chat completions.

## Run setup

| Field | Value |
|---|---|
| Workspace | `/srv/dev-disk-by-label-opt/dev/caliber-eval/gemini-3-flash-preview-cloud/` |
| Source | `claude-hooks` @ `aeca4ae` (post-resilience-port; engine HEAD live during run was `8734716` once `thought_signature` patch landed) |
| Caliber | `1.49.6` |
| Provider | `openai` (via grounding proxy on `127.0.0.1:38091`) |
| Model | `gemini-3-flash-preview:cloud` (planner + fast model) |
| Proxy CWD | workspace dir (not live repo) |
| HOME | per-bench `fake-home-gemini-3-flash-preview-cloud/` |
| Wall clock | **4m 44s** ⚡ |
| Caliber score | **77/100 (Grade B)** |
| Refinement | 35 → 77 (+42 pts), F → B in 4m 44s total |

## Phase breakdown

| Phase | Wall | Notes |
|---|---|---|
| Detecting project stack | 52s   | ✓ markdown, python, shell, model… (5 langs) |
| Generating configs      | 1m 46s | ✓ Codified `pgvector-mcp` |
| Generating skills       | **1m 5s** | ✓ 4 project skills — fastest skill-gen recorded |
| Searching community     | 1m 28s | ✓ 10 found (caliber registry) |
| Validating & refining   | 1m 0s  | ✓ Refined |
| **Total** | **4m 44s** | rc=0 |

## Headline metrics — full cohort

| Metric | claude-cli | gemma-v3 | gemma4-31b | **gemini-3-flash-preview** |
|---|---|---|---|---|
| Caliber score | **94** (A) | 87 (A) | 85 (A) | **77 (B)** |
| Wall          | 37m 14s | 14m 32s | 28m 41s | **4m 44s** ⚡ |
| Total skills  | 8 | 4 | 5 | **7** |
| Project skills | 5 | 1 | 2 | **4** |
| `paths:` fm coverage | 5/5 | 1/1 | 2/2 | **4/4** (100% — parity with baseline) |
| `paths:` fm entries | 25 | 5 | 4 | **15** |
| Bare file refs | 48 | 6 | 8 | 9 |
| `file:line` refs | 0 | 0 | 0 | 0 |
| Body chars total | 56,031 | 15,891 | 18,316 | 18,948 |
| **Hallucinated path refs** | 0 | 0 | 0 | **4** ❌ |
| `CLAUDE.md` chars | 9,109 | 4,693 | 6,104 | 4,476 |
| Token usage | n/a | n/a | 364k in / 11.9k out / 8 calls | **373k in / 16.5k out / 10 calls** |

## What gemini got right

1. **Speed.** 4m 44s end-to-end is **6× faster than gemma4:31b-cloud**
   and **8× faster than the claude-cli baseline**. Skill generation in
   1m 5s for 4 skills is unprecedented in this benchmark.
2. **Skill density.** 4 project-specific skills (manage-hooks,
   manage-memory, proxy-stats, run-consultant) — most by any
   non-claude-cli backend. **All 4 have populated `paths:` frontmatter
   (100% coverage).** 15 total `paths:` entries — almost as dense as
   claude-cli's 25, in **a 6× shorter run.**
3. **Score-refine convergence.** 35 → 77 (+42 pts) in 1m 0s of
   validation phase. Fastest convergence recorded.
4. **Resilience layer + thought_signature fix held.** 0 thought_signature
   400s, 0 5xx, 0 4xx-retryable. The 10 empty-content responses (cloud
   throat-clearing on heavy initial prompts) all caught and bounded by
   the new retry layer (2 recovered, 8 hit the 5-attempt cap and
   shipped empty — agent loop's `force_first` then injected corrective
   user messages and the model recovered).

## Where the 17-point gap to baseline lives

The largest single deduction is **`References point to real files`
6/8 (−2 pts)** — caliber's verifier flagged **4 non-existent paths**
in the generated config:

> These references don't exist: `bench_parallel_providers.py`,
> `claude-hooks.example.json`, `apply-caliber-patch.sh` (+1 more)

This matches gemini's documented **"off-topic on hardening"** failure
mode from the consultants Q3 benchmarks: the model produces
plausible-shaped citations but doesn't always ground them in the
actual filesystem. caliber's verifier catches it; the consultants
v1.2 actionability sub-rubric catches the same class of failure on Q3.

Other still-failing categories at 77/100:

| Category | Score | Note |
|---|---|---|
| References point to real files | **6/8** | **Hallucinated paths — the dealbreaker** |
| Executable content (code blocks) | 3/8 | Sparse code blocks in CLAUDE.md |
| Project grounding | 3/12 | Only `systemd` missing — much closer than gemma4-31b's 9-miss |
| MCP servers configured | 0/0 | N/A on this project |
| AGENTS.md exists | 0/1 | Caliber didn't generate one |
| Learned content present | 0/2 | No `caliber learn install` |
| External sources configured | 0/0 | N/A |

## Skills produced

| Skill | Body chars | `paths:` | Bare refs | Built-in? |
|---|---|---|---|---|
| `manage-hooks`   | 2,114 | 4 | 4 | no |
| `manage-memory`  | 1,588 | 5 | 3 | no |
| `proxy-stats`    | 1,828 | 3 | 0 | no |
| `run-consultant` | 1,880 | 3 | 1 | no |
| `find-skills`    | 1,963 | 0 | 0 | yes |
| `save-learning`  | 2,129 | 0 | 0 | yes |
| `setup-caliber`  | 7,446 | 0 | 1 | yes |

These cover **different architectural axes** than gemma's picks
(`consultants-management` + `setup-compile-aware`) and v3's
(`proxy-ops`). Notable: gemini chose 4 narrower skills covering
hooks/memory/proxy/consultants instead of 1–2 broader skills. That's
a stylistic choice rather than a quality difference, but it's worth
noting if you want to compose a max-coverage `.claude/skills` from
multiple bench outputs.

## Cloud weather during the run

- **83 chat completions** to upstream
- **0 5xx**, **0 retryable 4xx**, **0 thought_signature 400s**
- **10 empty-content responses** caught — same heavy-initial-prompt
  pattern observed across all cloud runs. **2 fully recovered**
  via the empty-retry budget; 8 hit the cap and the agent loop's
  existing `force_first` mechanism took over (corrective user
  message → model retry).

`/health.upstream_flaps` after the run:

```json
{
  "upstream_5xx_total": 0,
  "upstream_retryable_4xx_total": 0,
  "upstream_empty_total": 10,
  "upstream_retry_succeeded_total": 2,
  "upstream_retry_exhausted_total": 0
}
```

## Verdict

**EVALUATED-ONLY** for caliber init at this baseline.

- Pick over gemma4-31b-cloud when **wall time matters more than path
  correctness** — gemini is 6× faster but produces ~4 hallucinated
  paths per init that the operator must verify and clean up before
  the config is trustworthy.
- Don't pick over gemma4-31b-cloud or v3 when **path correctness
  matters** — the hallucinated-path failure mode is gemini's
  signature weakness across both consultants benchmarks (Q3 off-topic
  → C+) and now caliber-eval (References 6/8 → −2 pts).
- **For real caliber init runs**: prefer `gemma4:31b-cloud` or
  `gemma4-98e:native-tools` (v3 Modelfile). For **smoke / iterative
  tuning** where the operator will hand-fix the output anyway, gemini's
  4m 44s wall is hard to beat.

## Reproduce

`run-bench.sh` in this workspace's directory, PROTOCOL.md §1–§5 for the
full recipe; PROTOCOL.md §7 for the publish step.

## Cross-references

- thought_signature passthrough fix: claude-hooks commit `8734716`
- Resilience port: claude-hooks commit `aeca4ae`
- Consultants Q3 weakness: [`docs/benchmarks/Q3-actionability-audit-2026-05-09.md`](../../shared/dev/claude-hooks/docs/benchmarks/Q3-actionability-audit-2026-05-09.md)
