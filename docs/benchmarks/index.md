# Benchmark index

The single entry point for every model/eval benchmark in claude-hooks. Results
are spread across three families and a few result trees; **this page links
straight to each one** so you never have to hunt. If you landed somewhere deep
and want to get back here, every benchmark doc has an "↟ benchmark index"
up-link.

---

## Table of contents — jump straight to results

| # | Benchmark | What it decides | Results | Protocol |
|---|---|---|---|---|
| 1 | **Council-role sweeps** | Which cloud model to pin per council role (planner / researcher / critic / synthesizer), as whole-model sweeps + heterogeneous mixes | [`council-role-sweeps.md`](council-role-sweeps.md) | [`EVALUATION.md`](EVALUATION.md) |
| 2 | **Coder — Python** | Best default for `cfg.roles.coder.model` (Python HumanEval-style) | [baselines → Coder](../consultants-skill-eval-baselines.md#coder) · [report](../../benchmarks/consultants/results/2026-05-16/coder/report.md) | [skill-eval protocol](../consultants-skill-eval-protocol.md#coder-sub-protocol-v10) |
| 3 | **Coder — per-language (hard)** | Best coder per language (C / C++ / C# / Go / Python / Rust) + global fallback, on a deliberately-brutal suite | [`coder-mlang-results.md`](coder-mlang-results.md) | [mlang suite](../consultants-skill-eval-mlang-suite.md) |
| 4 | **Coder — per-language (easy)** | Per-language *reliability floor* (30 easy problems × 6 langs) + how the new `minimax-m3` / `nemotron-3-super` candidates rank | [`coder-easy-results.md`](coder-easy-results.md) | [skill-eval protocol](../consultants-skill-eval-protocol.md) |
| 5 | **Tool executor** | Best default for `cfg.roles.tool_executor.model` + the default-on bit | [baselines → Tool executor](../consultants-skill-eval-baselines.md#tool-executor) · [report](../../benchmarks/consultants/results/2026-05-17/tool_executor/report.md) | [skill-eval protocol](../consultants-skill-eval-protocol.md#tool_executor-sub-protocol-v10) |
| 6 | **Stall thresholds** | Per-model `(stall_threshold_s, hard_cap_s)` for the M3 stall detector | [baselines → Stall](../consultants-skill-eval-baselines.md#stall-thresholds) · [report](../../benchmarks/consultants/results/2026-05-17/stall-tier1/report.md) | [skill-eval protocol](../consultants-skill-eval-protocol.md#stall-sub-protocol-v10) |
| 7 | **Caliber-eval (grounding)** | Which model to use for `caliber init` agent-config generation | [`caliber-eval-results/`](../caliber-eval-results/README.md) | [`caliber-eval.md`](../caliber-eval.md) |

**Protocols & specs:** council-role grading →
[`EVALUATION.md`](EVALUATION.md) · skill-eval suites (coder / mlang / stall /
tool_executor) → [`consultants-skill-eval-protocol.md`](../consultants-skill-eval-protocol.md) ·
running ledger of every skill-eval score →
[`consultants-skill-eval-baselines.md`](../consultants-skill-eval-baselines.md) ·
canonical council query set →
[`consultants-benchmarks.md`](../consultants-benchmarks.md).

---

## What each family measures

- **Council-role sweeps** — runs the three canonical council queries (smoke,
  audit-medium, audit-high) against a model pinned to every role, then grades
  per-query (Q1/Q2/Q3) *and* per-role (P/R/C/S) so heterogeneous mixes can be
  composed. The output is the default council config.
- **Coder (Python)** — drives the sandboxed `coder` role node on HumanEval-style
  problems with pytest oracles + an LLM judge; picks the best Python coder.
- **Coder (per-language, hard)** — the deliberately-harder multi-language sibling
  (medium / hard / very_hard tiers, no easy questions) that picks a coder per
  language. **Read the [normalization note](coder-mlang-results.md) before
  trusting the per-language picks** — the very_hard tier defeats every model, so
  the classification is computed over the questions models could actually answer.
- **Coder (per-language, easy)** — the floor-fixing counterpart (30 easy
  problems × 6 languages, uniform stdin→stdout). Every question discriminates,
  but the top *saturates*: only **go** and **rust** separate on pass-rate;
  c/cpp/csharp/python are competence-floor + quality/cost calls. See
  [`coder-easy-results.md`](coder-easy-results.md) — also where the two new
  candidates (`minimax-m3`, `nemotron-3-super`) are scored.
- **Tool executor** — measures *reading + reasoning over a codebase via tool
  calls* (grep/read/glob/survey), not code writing; picks the tool_executor
  default.
- **Stall thresholds** — a pure measurement bench: per-token timing percentiles
  that derive each model's stall + hard-cap timers. No pass/fail.
- **Caliber-eval** — scores the CLAUDE.md / agent-config artefacts a model
  produces for `caliber init` against a `claude-cli` reference (rubric /100 +
  grounded-reference count).

---

## How to read the tables

Three benchmark families use three metric vocabularies. Quick glossary; the
authoritative definitions live in each family's protocol.

**Council-role sweeps** ([full rubric](EVALUATION.md#3-quality-criteria--per-query)):

| Label | Meaning |
|---|---|
| **Q1** | *Smoke* grade — `PASS` / `WEAK` / `FAIL`. Names the four roles from code. |
| **Q2** | *Audit-medium* grade — `A`–`F`. Cites the psycopg ground-truth sites. |
| **Q3** | *Audit-high* grade — `A`–`F`. Traces a failure path + proposes one fix; the v1.2 *actionability* sub-rubric checks the fix is real (`shape→correctness`). |
| **Mix** | Per-role grade string `P:_ R:_ C:_ S:_` (planner/researcher/critic/synthesizer), [§3.5](EVALUATION.md#35-per-role-quality-grading-the-key-to-building-a-model-mix). |
| **wall / MAD** | End-to-end seconds; **MAD** = median absolute deviation across the N=3 runs (cloud is flaky — [§5](EVALUATION.md#5-multi-run-requirement) requires N≥3). |
| **c-tok** | Completion tokens (incl. reasoning) — a coarse cost proxy. |
| **PROD-READY / EVALUATED-ONLY** | [§4 thresholds](EVALUATION.md#4-passfail-thresholds-for-a-label). |

**Skill-eval suites — coder / mlang / tool_executor** ([rubric](../consultants-skill-eval-protocol.md)):

| Label | Meaning |
|---|---|
| **pass_rate** | Fraction of trials whose pytest oracle passed (strict, full-oracle). Qualifying gate: `≥ 70%`. |
| **alg / `passes_algorithm`** | Looser axis — core algorithm correct even if a constraint/edge test failed. |
| **avg_quality** | Mean of the judge LLM's 1–5 score. Qualifying gate: `≥ 3.5`. |
| **normalized** | (per-language suites) pass_rate/quality recomputed over only the questions ≥1 model could answer — questions that defeated *every* model are excluded from the classification. On [mlang](coder-mlang-results.md) this drops 10/13; on [easy](coder-easy-results.md) nothing is dropped (full == normalized), and the saturation moves to the *top* instead. |
| **median tokens / wall** | Tie-breakers (cheaper / faster wins). |

**Stall suite** ([rubric](../consultants-skill-eval-protocol.md#stall-sub-protocol-v10)):
`p99 TTFT` (time-to-first-token), `p99 inter` (inter-token gap),
`p99 wall`; the derived `stall_threshold_s` / `hard_cap_s` are the timers baked
into [`stall_defaults.py`](../../consultants/engine/stall_defaults.py).

**Caliber-eval** ([README](../caliber-eval-results/README.md)): `score/100`
(rubric), `skills (proj/total)`, grounded `paths:`/`file:line` reference counts.

---

## Caliber-init verdict (quick pointer)

Separate benchmark, same model pool (2026-05-09): 6 labels at
[`docs/caliber-eval-results/`](../caliber-eval-results/README.md). **Keep
`claude-cli` as the caliber-init default**; `glm-5.1:cloud` is the recommended
non-claude-cli fallback (topped the rubric 96 vs 94, 0 hallucinated refs, but
produced fewer project skills). Full reasoning + the disqualified models are in
the caliber-eval [README](../caliber-eval-results/README.md).

---

## Adding a benchmark run

- **Council-role sweep** — `scripts/consultants_benchmark.sh <label>`; grade per
  [`EVALUATION.md`](EVALUATION.md); append the label row to
  [`council-role-sweeps.md`](council-role-sweeps.md). The subject codebase is
  pinned by [`CURRENT_BASELINE`](CURRENT_BASELINE).
- **Skill-eval suite** — see
  [`consultants-skill-eval-protocol.md`](../consultants-skill-eval-protocol.md)
  (dry-run → live → render → record the baseline row) and
  [`benchmarks/consultants/README.md`](../../benchmarks/consultants/README.md)
  for the harness.
