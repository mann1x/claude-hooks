---
suite: tool_executor
suite_version: "1.0"
released: 2026-05-17
manifest:
  - trivial-01-find-symbol
  - trivial-02-read-section
  - easy-01-grep-read-chain
  - easy-02-listfiles-glob
  - medium-01-multifile-audit
  - medium-02-redundancy-test
  - hard-01-ambiguous-survey
  - hard-02-cite-correct-line
rubric:
  pass_rate_floor: 0.70
  quality_score_floor: 3.5
  tie_breaker: median_tokens
---

# Tool Executor Skill-Eval Suite v1.0

This file is the **manifest** for the tool_executor sub-protocol of
the **Consultancy Skill-Eval Protocol**. See
[`docs/consultants-skill-eval-protocol.md`](../../../../docs/consultants-skill-eval-protocol.md)
for the methodology, when-to-rerun rules, and the decision-rubric
context.

## What this suite measures

A candidate model's fitness for the M6 `tool_executor` role: can
it execute a one-sentence semantic intent by chaining tool calls
(`survey_project`, `list_files`, `read_file`, `glob`, `grep`,
`recall_memory`) and return a citation-grounded answer? Trials are
binary on correctness (pytest oracle passes/fails on the response
text + tool-call log) and 1-5 on quality (LLM-judged on intent
fulfillment + citation accuracy + tool-call efficiency).

Unlike the coder suite (where the role writes new code), this
suite measures **reading + reasoning over an existing codebase**.
The same tool stack the researcher uses, but driven by the
tool_executor agent loop's single-intent contract.

## Fixture corpus

All questions reference files under
[`fixtures/<subdir>/`](fixtures/). Each question's frontmatter
declares `fixtures_subdir: <name>` and the harness `chdir`s into
that subdir before driving the node so `read_file("auth.py")`
resolves relative to the fixture cohort.

Fixtures are hand-authored synthetic code/docs, NOT the
claude-hooks repo itself. This keeps the bench stable against
unrelated refactors — a fixture file is only edited when the
question explicitly needs new behavior.

## Manifest (8 questions × 4 tiers)

| Tier    | ID                          | Fixture cohort   | Trap / what it tests                                                |
|---------|-----------------------------|------------------|---------------------------------------------------------------------|
| trivial | trivial-01-find-symbol      | simple_constants | Single-file grep; cite path:line for a constant definition         |
| trivial | trivial-02-read-section     | readme_basic     | Read a specific section from a markdown file                       |
| easy    | easy-01-grep-read-chain     | auth             | grep → read; return function body + line refs                      |
| easy    | easy-02-listfiles-glob      | multipkg         | List `*.py` under a subdir, describe 3 with one line each          |
| medium  | medium-01-multifile-audit   | todos            | Find all TODO comments across 3 files; summarise with locations    |
| medium  | medium-02-redundancy-test   | redundant        | The answer is already in the `why` block — should NOT re-read      |
| hard    | hard-01-ambiguous-survey    | serializers      | "How is serialization done?" — no path; requires survey → glob → read |
| hard    | hard-02-cite-correct-line   | configdrift      | Find the EXACT line where a default is set; off-by-one penalty     |

## Rubric (the decision)

A model **qualifies for the tool_executor role default** iff:

1. `pass_rate ≥ 0.70` — at least 6 of 8 oracle test suites pass.
2. `avg_quality_score ≥ 3.5` — LLM judge averages 3.5 or better
   across compiled trials.

Among qualifying models, the **recommended default** is the one
with the highest `pass_rate`. Ties break on `median_tokens`
(cheaper wins). If no model qualifies, the role stays disabled
and `tool_executor_defaults.RECOMMENDED_DEFAULT_ON` remains
`False`.

The **default-on bit flip** (`DEFAULT_ENABLED_BY_ROLE
["tool_executor"]` from `False` to `True`) is a separate decision
gated by:

1. M11c-2 picks a qualifying model.
2. Task #103 (x-tier proper composition) resolves — either the
   engine refactor lands so the role composes safely under
   multi-model researcher fanout, OR the role is explicitly
   documented as base-tier-only with config-layer gating.

Until both conditions clear, the role stays opt-in.

## Per-trial flow

1. Harness loads question + fixtures_subdir.
2. `chdir`s into `fixtures/<subdir>/`.
3. Builds a `ToolPlanItem(intent=task, why=why,
   suggested_tools=suggested_tools, lane_idx=0, parent_round=1)`
   from the frontmatter.
4. Drives `tool_executor_node` directly with this item, the
   model's ChatClient, the production tool stack, and a
   per-trial recorder.
5. Captures the resulting `ToolResult.text` + ordered tool-call
   log.
6. Runs the oracle pytest with `TOOL_EXEC_OUTPUT=<text>` and
   `TOOL_EXEC_CALLS=<json-log>` env vars.
7. If the trial completed (no exception), calls the LLM judge
   with a strict 1-5 rubric.
8. Writes trial result to `trials.jsonl` immediately.

## Oracle contract

Every oracle file (`<id>-oracle.py`) is a pytest module that reads
the captured output via env vars and asserts expected facts. The
harness sets:

- `TOOL_EXEC_OUTPUT` — the final assistant text from the lane.
- `TOOL_EXEC_CALLS` — JSON list of
  `[{"tool": name, "args": json_args, "result_excerpt": str}, ...]`
  in call order.
- `TOOL_EXEC_FIXTURE_DIR` — absolute path to the fixture
  subdirectory (read-only; oracle uses this for path-absolute
  citation checks).

Oracles validate any combination of:

- **Citation correctness**: response mentions specific `path:line`
  refs that exist.
- **Content correctness**: response contains expected substrings.
- **Tool-call efficiency**: certain tools were called (or not);
  no redundant re-reads.
- **Intent fulfillment**: response answers the asked question.

## Reproducibility

Each results file records `suite_version: "1.0"` + the manifest
hash. Re-running v1.0 against a model previously scored should
produce statistically similar results (modulo proxy-side flap /
model-side temperature drift).

## Versioning

- **PATCH** (1.0 → 1.0.1): oracle assertions tightened but the
  intent + fixture corpus are unchanged. Old baselines stay
  comparable.
- **MINOR** (1.0 → 1.1): a new question is added OR a fixture
  cohort grows. Existing baselines are NOT comparable.
- **MAJOR** (1.0 → 2.0): rubric thresholds change OR the
  per-trial flow changes shape.

## Adding a new question

1. Pick a tier (trivial/easy/medium/hard).
2. Create a new fixture subdirectory under `fixtures/` if needed.
3. Add `<id>.md` with frontmatter (id, tier, source, task, why,
   suggested_tools, oracle, fixtures_subdir).
4. Add `<id>-oracle.py` with the pytest assertions.
5. Append the id to the `manifest:` list above.
6. Bump the suite version per the rules above.

## Out of scope (deferred)

- **Live cloud calls** happen only in M11c-2 with explicit user
  opt-in via `--accept-cost`.
- **x-tier composition** is a separate decision (task #103) made
  after M11c-2 baseline lands.
