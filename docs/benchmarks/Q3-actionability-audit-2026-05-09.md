# Q3 hardening recommendation actionability audit (2026-05-09)

The protocol's §3 Q3 grading rubric (v1.0/v1.1) graded the *shape* of a
hardening recommendation — "code-shaped change with `path:line`" — but
not its *correctness* against the live codebase. This audit re-grades
every Q3 recommendation across both the 2026-05-07 and 2026-05-09 sweeps
against actionability: would the proposed change actually compile,
apply cleanly to the live code at the current engine HEAD, and address
the failure mode the recommendation just described?

The result reshuffles the leaderboard. Several "frontier-class A" answers
proposed changes that turn out to be **redundant** (already covered by an
existing layer), **regressive** (revert a deliberate prior fix), or
**off-topic** (target a different bug than the question asked about).

## Scoring criteria (correctness sub-rubric)

For each Q3 recommendation, two independent dimensions:

1. **Compiles cleanly against live code** — does the suggested edit, as
   stated, apply to the current `consultants/engine/*.py` and
   `consultants/server/runner.py`? Stale `path:line` references are OK if
   the *intent* is unambiguous; fabricated function/parameter names are
   not.

2. **Addresses the failure mode the answer described** — the Q3 prompt
   asks the model to trace a specific failure path then propose a fix.
   A recommendation that compiles but targets a different bug fails
   this dimension.

The new effective grades:

| Old shape grade | New correctness grade | Definition |
|---|---|---|
| A | A | Compiles AND addresses the failure mode |
| A | B+ | Compiles AND partially addresses (covers ≥1 sub-issue but not the main one) |
| A | C+ | Compiles AND addresses *an* issue but not the one the answer described |
| A | D | Compiles but is REDUNDANT — the protection it adds already exists at another layer |
| A | F | Recommendation is REGRESSIVE — would revert a deliberate prior fix, or its referenced parameter/symbol does not exist |

## 2026-05-07 sweep (re-graded)

| Label | Original Q3 | Recommendation | Actionability verdict | New Q3 |
|---|---|---|---|---|
| `kimi-k2.6-cloud` | A | At `runner.py:174`, replace binary `terminal_status` with three-way `completed`/`degraded`/`failed` (emit `"degraded"` when error+answer both exist) | UX-level change. Compiles. But it **changes the meaning of `status=failed`** for every existing test, dashboard, and consumer. UX choice, not bug fix. | **C+** |
| `gemma4-31b-cloud` | A | Modify `build_synthesizer_messages` (`council.py:245`) to check for the `error` key and append a warning to the synthesizer's prompt | Compiles (would need `state` to be passed in or detect tombstone strings from `research_rounds`). **Directly addresses the failure mode** — synthesizer learns the lane failed. | **A** |
| `glm-5-1-cloud` | A | Make `error` and `_role_failed` additive list reducers at `graph.py:74-75` + update tombstones + runner truthy check | Compiles. **Addresses multi-lane error visibility** (currently last-write-wins clobbers earlier failures), but doesn't fix the single-lane `status=failed`-with-good-answer paradox. | **B+** |
| `minimax-m2-7-cloud` | F | "add tombstone detection to the synthesizer prompt" — but the answer body claims the cited files **do not exist** ("uses `engine/graph.py`, `server/loop.py`, `server/storage.py`") | Hallucinated. Files do exist. The recommendation is bypassed by the answer's own denial of the codebase structure. | **F** (unchanged) |
| `qwen3-5-397b-cloud` | F | "depends on LangGraph's non-annotated field merge semantics, which the codebase does not explicitly control" — no concrete change | Hand-wave; no concrete change to apply. | **F** (unchanged) |
| `qwen3-5-cloud` | B | Remove the `"error"` key from the lane-level tombstone in `council.py:563-566` | Compiles. **But it silences the failure signal** — `status=completed` would always appear even when lanes crash. UX-regressive: hides degradation from dashboards/tests. | **C** |

## 2026-05-09 sweep (re-graded)

| Label | Original Q3 | Recommendation | Actionability verdict | New Q3 |
|---|---|---|---|---|
| `gemini-3-flash-preview-cloud` (N=3) | A− | Change `research_rounds_used: Annotated[int, operator.add]` → `Annotated[int, max]` at `graph.py:69` | Compiles, but **off-topic** — the answer just traced a lane-exception failure, then proposed fixing an unrelated rounds-counting concern. The change doesn't help the user-perceived `status=failed` paradox. | **C+** |
| `deepseek-v4-flash-cloud` (N=3) | A | Flip `runner.py:172-179` status logic to `"completed" if final_answer non-empty else "failed"` | Same UX-level change as kimi-k2.6's recommendation. Same caveats: changes the meaning of `status=failed` for every consumer. | **C+** |
| `nemotron-3-super-cloud` (N=3) | A | Wrap `_wrap_researcher` in `graph.py:145-157` with try/except | **Redundant**. `researcher_node` (`council.py:854-873`) already has the try/except and returns a tombstone. The outer wrap has nothing to catch except programmer errors in the tombstone code itself. The model didn't trace through to see the protection it proposed already exists one layer in. | **D** |
| `deepseek-v4-pro-cloud` | A | Same `runner.py:172-179` status flip as deepseek-v4-flash | UX flip; same caveats as kimi/deepseek-v4-flash. | **C+** |
| `qwen3-coder-next-cloud` | A | Change `return tombstone` → `raise` at `council.py:554-573` | **Regressive**. The in-code comment at `council.py:856-862` documents the current tombstone-return as the deliberate fix to a previous audit-high finding. Reverting to `raise` re-introduces the prior bug (silent confidently-wrong answers when a lane crashes). | **F** |
| `gemma4-31b-cloud` (N=3, r2) | A | Augment `SYNTHESIZER_SYSTEM` (`council.py:227+`) to instruct: *"if research reports contain failure markers, explicitly state which part of the plan could not be verified"* | Pure prompt addition. Doesn't change graph behavior, doesn't break tests, doesn't change state semantics. **Directly addresses the visible UX failure** — user gets confidently-degraded answer with no mention of the failed lane. Easily reversible. Lowest-risk, highest-leverage. | **A** |
| `gemma4-31b-cloud` (N=3, r3) | A | Change `error: Optional[str]` → `Annotated[list[str], operator.add]` at `graph.py:74` | Compiles. **Addresses multi-lane error overwrite** — currently last-write-wins clobbers earlier failures. Same insight as `glm-5.1-cloud` from 2026-05-07 but more focused. | **A** |
| `mistral-large-3-675b-cloud` | C/F | Add `return_exceptions=True` to `compiled.stream` at `runner.py:137` | **Fabricated parameter**. LangGraph's `.stream()` does not accept `return_exceptions`. A user-facing recommendation that misleads anyone who trusts it. | **F** (confirmed) |
| `nemotron-3-nano-30b-cloud` | C | No concrete recommendation; recommendation thin | No actionable change proposed. | **C** (unchanged) |

## Headline finding

**Of 14 Q3 recommendations across both sweeps, only 3 are clean A-grade actionable fixes:**

1. **`gemma4-31b-cloud` (2026-05-07)** — modify `build_synthesizer_messages` to check for error and inject warning
2. **`gemma4-31b-cloud` (2026-05-09 r2)** — augment `SYNTHESIZER_SYSTEM` prompt with failure-marker handling
3. **`gemma4-31b-cloud` (2026-05-09 r3)** — make `error` an additive list reducer

All three are gemma4. Both at the old engine HEAD and at the v1.1.0 engine HEAD. Across two sweeps and three runs at the new HEAD, gemma4 is the **only model that consistently proposes Q3 fixes that actually solve the bug it diagnosed** without being redundant, regressive, or off-topic.

Two more land at B+:

4. **`glm-5-1-cloud` (2026-05-07)** — additive reducers for `error`/`_role_failed` (covers multi-lane case)

Several frontier-class answers that originally graded A or A− degrade significantly:

- `nemotron-3-super-cloud` (2026-05-09): A → **D** (redundant)
- `qwen3-coder-next-cloud` (2026-05-09): A → **F** (regressive)
- `gemini-3-flash-preview-cloud` (2026-05-09): A− → **C+** (off-topic)
- `kimi-k2.6-cloud` (2026-05-07), `deepseek-v4-flash-cloud` (2026-05-09), `deepseek-v4-pro-cloud` (2026-05-09): all A → **C+** (UX flip, contentious)

## Implications for role recommendations

- **Synthesizer for code-diff hardening** — `gemma4:31b-cloud` is now the cheapest A-pick, replacing the prior single-run claim. The other PROD-READY models produce hardening recommendations that look impressive (concrete `path:line`, code-shaped) but don't survive an actionability check.
- **Default for all-roles** — `gemini-3-flash-preview:cloud` still wins per-fire wall and the smoke/audit-medium grades. Its Q3 weakness is specific to the hardening-recommendation sub-task.
- **For real hardening PRs** — don't blindly trust any single model's Q3 recommendation. Cross-check against the live code; the protocol's shape-grading was insufficient signal.

## Protocol implications

The Q3 rubric in `EVALUATION.md §3 Q3` should be augmented with a **correctness sub-criterion** alongside the existing shape criterion. See protocol changelog for the v1.2 bump.
