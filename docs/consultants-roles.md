# `/consultants` roles reference

Companion to [`docs/consultants.md`](consultants.md). That doc tells
you how to drive the engine; this one tells you what each role
actually does, when it's enabled, the model defaults, and — for
opt-in roles — when flipping the default-off bit is worth it.

## Role registry — quick map

| Role           | Default | Default model                     | When enabled (effort tiers)         | Doc anchor                  |
|----------------|--------:|-----------------------------------|-------------------------------------|-----------------------------|
| `planner`      |    on   | global `DEFAULT_MODEL`            | all tiers                           | [planner](#planner)         |
| `researcher`   |    on   | global `DEFAULT_MODEL`            | all tiers (×N fan-out at xtier)     | [researcher](#researcher)   |
| `critic`       |    on   | global `DEFAULT_MODEL`            | medium / high / max / x* tiers      | [critic](#critic)           |
| `synthesizer`  |    on   | global `DEFAULT_MODEL`            | all tiers                           | [synthesizer](#synthesizer) |
| **`tool_executor`** | **off** (2026-05-18) | `gemma4:31b-cloud` (M11c-2 bench) | opt-in, all tiers      | [tool_executor](#tool_executor) |
| **`coder`**         | **off** | `glm-5.1:cloud` (M11b bench)      | opt-in, all tiers                   | [coder](#coder)             |

"Default" means what a fresh `ConsultantsConfig` produces with no
TOML overrides. Every role's enabled bit is one TOML line to flip
in `~/.claude/consultants-config.toml` (user-global) or
`<project>/.claude-hooks/consultants.toml` (project-scope):

```toml
[role.tool_executor]
enabled = true

[role.coder]
enabled = true
```

The role's model is set the same way:

```toml
[role.tool_executor]
model = "qwen3-coder-next:cloud"   # override the M11c-2 winner
```

Both opt-in roles also accept `claude-consultants config set-role
<role> --enabled true|false --project` if you prefer the CLI.

---

## planner

**Purpose:** decompose the user's question into a numbered plan of
1–N concrete investigation steps. Each step names files / paths /
symbols the researcher should ground in. The planner does NOT
call tools; it produces a plan only.

**Why it exists:** without a planner the researcher tends to wander
across the entire codebase on broad questions. The planner pulls
the question into a concrete shape (3-5 numbered items typical at
`medium` effort, 5-12 at `xhigh`) so the researcher's tool calls
are targeted.

**Output contract:**
- A numbered markdown list (`1. ...`, `2. ...`).
- Each item names at minimum one file or symbol to ground in.
- Items are independent — the fan-out logic at xtier splits them
  across parallel researcher lanes (`Send` multiplex).

**Failure modes:**
- Empty plan → the council falls through to the single-researcher
  v1 path. Not fatal; the answer quality drops.
- Plan asks for files that don't exist → researcher catches it via
  tool_results returning empty, and the citation linter would flag
  any cite the researcher emits.

**When to swap the model:** rarely. The planner is the cheapest
role (single short turn, ≤2 k tokens typical) so the global
default is usually fine. Override at `[role.planner]` if you want
a model with stronger structured-output reliability for
hard-to-decompose questions.

---

## researcher

**Purpose:** the workhorse. Reads the plan, calls
`read_file` / `grep` / `glob` / `list_files` / `survey_project` /
`recall_memory` against the project's allowed_roots, and writes a
research report with `path:line` citations.

**Two modes**, decided by whether `tool_executor` is enabled:

### Mode A — inline tool-loop (`tool_executor` disabled, default)

The researcher runs `claude_hooks.agent_loop.runner.run_loop` as
its own inner loop: emit tool_calls → see results → emit more
tool_calls → write a REPORT. One LLM "researcher" persona per
lane handles everything end-to-end. The lane's model sees raw
tool output and can call follow-up tools as needed.

This is the M14 first-real-ask tool_executor on/off A/B's
**WITHOUT** variant — faster, fewer LLM calls, fewer tokens,
because there's no inter-role aggregation barrier. The researcher
that calls grep is the one that interprets it.

### Mode B — PLAN/REPORT split (`tool_executor` enabled, opt-in)

The researcher operates in two phases:

1. **PLAN mode** — emits a `{"tool_plan": [...]}` JSON block
   listing intents + suggested tools. No inline tool calls.
2. **REPORT mode** — after the `tool_executor` lanes complete
   the suggested work, the researcher re-enters and writes a
   prose report citing the EVIDENCE blocks the executor produced.

This was introduced in M6 (v2) and made cleanly x-tier-composable
in M11c-3 (commit `e62fd85`, per-lane `parent_lane_idx`
threading + `_fanout_after_tool_executor` conditional edge).

**xtier fan-out:** when `effort` is `xmedium` / `xhigh` / `xmax`,
the researcher role fans out across N models (primary +
`extra_models`). Each plan item × each model = one lane. So an
xhigh question with 3 plan items and 2 extras runs 3 × 3 = 9
researcher lanes in parallel via LangGraph `Send`. The
synthesizer aggregates all 9 lanes' reports.

The `[[feedback_xtier_diversity_priority]]` memory marks this
multi-model researcher fan-out as the council's **defining
advantage** — never compromise it for other features.

**Output contract:**
- A prose research report. Sectioned bullets are fine.
- Every concrete claim cites `path:line`. The linter at the
  researcher boundary (commit `ef79a30`, #204) verifies cites
  before they flow to peer findings + critic + synthesizer.
- Failure tombstones: `(researcher lane failed: <error>)`. The
  synthesizer prompt explicitly treats these as gap signals, not
  evidence.

**Failure modes:**
- **Source-listing fabrication** (worst class): a lane emits a
  60-line numbered code block for a file it didn't read. Seen
  twice today in glm-5.1 researcher lanes — the
  `_lint_research_text` closure (#204) + the source-listing
  guard in `build_tool_plan_user_appendix` (#207) catch this in
  prose form and in the load-bearing REPORT-mode appendix.
- **Wrong-line drift**: cite to line N±2 of a real symbol. The
  text-at-cited-line linter (#205) marks these inline.
- **Tool-spin** at high effort: researcher keeps requesting more
  tools without writing the REPORT. Truncated by the stall
  detector (M11a) when present.

**When to swap the model:** model diversity is the point at
xtier. Pick 2–3 cloud models with different training distributions
for the `extra_models` list (we use gemini-3-flash, gemma4:31b,
glm-5.1 by default). Single-tier runs use just the primary.

---

## critic

**Purpose:** read the researcher's reports + the question, emit a
verdict on whether the evidence is sufficient for the synthesizer.

**Output contract:**
- Line 1: `DECISION: ready` OR `DECISION: needs_more_research`.
- Line 2+: justification or specific gaps (`path:line` style when
  pointing at code).

The critic's `needs_more_research` verdict re-routes back to the
researcher for another round, up to the effort-tier round cap.
Default bias is **toward `ready`** — extra rounds cost a full
agent loop each (~30-90 s).

**When it's enabled:** medium / high / max / xmedium / xhigh /
xmax. Skipped at `low` effort (a `synthesizer_self_critic`
variant takes over inside the synthesizer to save the round).

**Meta-critic at xmax:** at the `xmax` tier the role fans out
across multiple critic models and a `meta_critic` consolidates
their verdicts. See `META_CRITIC_SYSTEM` in
`consultants/engine/council.py`.

**When to swap the model:** the critic benefits from a strong
reasoning model. Default uses the global `DEFAULT_MODEL` but at
xhigh the council typically pairs it with the same gemini /
gemma / glm rotation as the researcher.

---

## synthesizer

**Purpose:** write the final user-visible answer from the
planner's plan, researcher's reports, and critic's verdict.

**Output contract:**
- Bottom line on the first line of prose.
- `path:line` cites for every codebase-dependent claim. The
  citation linter (#204) checks them at output time and inserts
  `[unverified — file not found]` / `[no <symbol> at this line; …]`
  markers inline for any fabrication.
- Match the question's shape: list-shaped questions → bulleted;
  yes/no → one decisive sentence + one paragraph; walk-me-through →
  numbered code path + edge cases.

**At low / medium effort:** a `synthesizer_self_critic` prompt
variant runs instead of the dedicated critic. The synthesizer
identifies the weakest claim in the research and either bolsters
or downgrades it before answering.

**Failure modes:**
- **Fabrication relay**: the synthesizer can faithfully relay a
  fabricated cite from a researcher's report. The pre-#204
  history of the M14 first-real-ask is the case study; #204 wired
  the linter at the researcher boundary so the synthesizer's
  input is pre-annotated.
- **Truncation**: gemma4:31b-cloud has been observed stopping
  mid-bullet under tightened CITATION INTEGRITY prompts. Rare,
  not a structural problem.

**When to swap the model:** at the M11c sweep we pinned
`gemma4:31b-cloud` as the M14 synthesizer model — it matched the
M11c-2 tool_executor bench winner and was the M6 fallback. The
2026-05-18 forensic confirmed gemma is a faithful relay and not a
fabrication originator.

---

## tool_executor

> **Status — 2026-05-18:** default **disabled**. The M14
> first-real-ask tool_executor on/off A/B
> (`benchmarks/consultants/results/2026-05-18/tool-executor-ab/`)
> showed the role net-negative on grep-shaped questions. The
> M11c-2 bench result still validates the role on tool-heavy
> reasoning corpora; the default-off recognizes most operator
> questions don't look like the bench corpus.

**Purpose (when enabled):** a specialist role that takes the
researcher's `tool_plan` JSON and runs the tool calls in its own
Send-multiplexed lanes. Each plan item fans out to a separate
tool_executor lane; results are collected and routed back to the
researcher in REPORT mode.

**Why it exists:** intuition was that separating *tool execution*
from *prose reasoning* would help the researcher stay focused on
the reasoning prompt and let the executor specialist optimize for
tool throughput. The M11c-2 bench (`benchmarks/consultants/
questions/tool_executor/`, 8 questions × 4 model candidates,
HumanEval-style tool-call corpus) validated this on questions
where tool work is heavy and the researcher would otherwise
saturate context. `gemma4:31b-cloud` won the bench at 87.5% pass
rate + 5.00 avg quality.

### Why default-off

The 2026-05-18 A/B against the M14 store-reaper walk-me-through
question shows the cost-benefit on a question that **isn't** in
the bench corpus shape:

|                | WITH tool_executor | WITHOUT |
|----------------|-------------------:|--------:|
| Wall time      | 1121 s             | **403 s (−64%)** |
| Total tokens   | 2.72 M             | **1.54 M (−43%)** |
| LLM calls      | 142                | 55 |
| Edge cases identified | 4           | **5** |
| Linter annotations shipped | 0     | 1 (line drift) |

The role cost **+12 minutes wall + 1.2 M tokens** AND identified
**fewer edge cases**. That's net-negative.

**The architectural read:**

1. The fanback barrier (`_fanout_after_tool_executor` →
   researcher REPORT mode) serializes per-lane state. Information
   that was implicit in the researcher's working memory during
   PLAN gets re-presented as text-only EVIDENCE blocks on the
   re-entry.
2. Peer findings aggregate before synth. At xhigh the synth sees
   N × M lane reports merged — most of the prompt is redundant
   restatement.
3. Without tool_executor, each researcher lane sees raw tool
   output and reasons end-to-end. The synthesizer sees N coherent
   reports instead of N × M.

The bench's tool-call corpus picks questions where this
serialization is worth it; the M14-style walk-me-through is the
opposite — tool work is light, reasoning is the bulk, and the
fanback is pure overhead.

### When to enable tool_executor anyway

Turn it on when:

- Tool work is **heavy and structured** — 5+ files across deep
  call chains, or systematic grep sweeps where each lane would
  otherwise burn its context.
- Sub-questions match the M11c-2 corpus profile (see
  `benchmarks/consultants/questions/tool_executor/`). Look for
  bench-corpus shapes: cross-file refactor risk, identify-the-
  bug-in-this-stack-of-files, multi-module trace.
- You're running at `xhigh` / `xmax` and willing to trade wall
  time for the chance that the specialist's lane structure beats
  the inline tool-loop on context discipline.

Leave it off when:

- Grep-and-interpret questions (the M14 case study). Inline tool
  loop is faster and sharper.
- Wall time is the binding constraint.
- The question can be answered with 1–3 file reads — fanback
  overhead exceeds the actual investigation.

### Shortcomings to know about when enabled

- **Wall time cost**: ~3× on M14-shape questions. The fanback
  barrier waits for ALL tool_executor lanes before the researcher
  can REPORT, so the slowest lane bounds total time.
- **Token cost**: ~1.4–1.8× the inline-loop variant for
  equivalent answer quality. The aggregation re-renders peer
  findings, padding the synth prompt.
- **Aggregation lossiness**: peer findings serialize to text
  and lose the model's working-memory context. Edge cases the
  inline-loop variant catches because the model "still has the
  grep output in head" can disappear in the round trip.
- **Researcher contamination amplification**: when a researcher
  lane fabricates (the 2026-05-18 glm-5.1 source-listing case),
  the fabrication propagates through tool_executor lanes (no
  effect there) AND through peer findings aggregation (where
  it's hardest to remove). The citation linter at the researcher
  boundary (#204) mitigates this, but the contamination surface
  area is larger with tool_executor in the loop.

**Model default:** `gemma4:31b-cloud` (M11c-2 bench winner).
Override at `[role.tool_executor] model = ...` when the bench
gets re-run with a new candidate.

---

## coder

**Status:** default **disabled**. Operator opts in to allow
sandboxed file writes.

**Purpose (when enabled):** a sandboxed `write_file` role that
the planner can gate to. Planner emits a `requires_code_generation
= true` + a `coder_tasks` JSON block; the coder executes the
writes within configured limits:

- **Per-file cap:** 50 KB
- **Per-lane cap:** 1 MB total
- **Files per lane:** 16

Cap violations are NOT exceptions — they come back as tool-result
errors so the LLM self-corrects, mirroring the M10 design choice
to keep the engine tolerant of partial coder failures.

**Output contract:**
- File writes are sandboxed to the session's allowed_roots.
- Coder runs as a `synthesizer-target redirect` — every edge that
  would have landed on the synthesizer now lands on the coder
  router when the role is enabled. When the role is disabled, the
  redirect is bypass-free and the M12 parity guarantees hold.

**When to enable:** when you actually want files written. The
default-off state means the council answers a question but never
touches your filesystem; opting in lets you ask "implement X" and
get a patch back. Read
[`docs/consultants-skill-eval-baselines.md`](consultants-skill-eval-baselines.md)
for the M11b coder rubric pass rates per model.

**Shortcomings to know about when enabled:**

- **Sandbox failures are visible**: if the coder tries to write
  outside allowed_roots, the tool result is an error message the
  next coder turn sees. The LLM may or may not recover gracefully
  (model-dependent).
- **No transactional rollback**: a multi-file coder turn that
  fails partway leaves the successful writes in place. There is
  no undo. Use git status before-and-after.
- **Model picks matter a lot here**: the M11b bench (8 mlang
  HumanEval-style questions × 4 languages) found `glm-5.1:cloud`
  as the global default (pass=100%, avg_quality=4.88, median
  tokens=1841). Per-language overrides via `routes_by_language`
  let you route Rust to a different model than Python if a future
  bench result warrants. See the seeded routes in
  `consultants/engine/coder_defaults.py`.

**Model default:** `glm-5.1:cloud` (M11b coder rubric winner).
Routes can be overridden per-language at
`[role.coder.routes_by_language]`.

---

## See also

- [`docs/consultants.md`](consultants.md) — the engine driver doc:
  install, service modes, CLI subcommands, configuration TOML.
- [`docs/consultants-benchmarks.md`](consultants-benchmarks.md) —
  the bench suite index pointing at every M11/M14 result record.
- [`docs/consultants-skill-eval-baselines.md`](consultants-skill-eval-baselines.md)
  — model-pick ledger; what the bench picked + the cost of each
  re-baselining.
- [`docs/consultants-skill-eval-protocol.md`](consultants-skill-eval-protocol.md)
  — how to add a new role bench when a future role is proposed.
- [`docs/consultants-transcript-db-schema.md`](consultants-transcript-db-schema.md)
  — the on-disk SQLite sidecar schema; how to query a session's
  raw turns.
- `benchmarks/consultants/results/2026-05-18/tool-executor-ab/`
  — the A/B that drove the tool_executor default flip-back.
