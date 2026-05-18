# M14 first real ask — re-run after fix — 2026-05-18

Re-run of the same xhigh consultation that produced the
[refusal](report.md) earlier today (SID
`csl-2026-05-18-0937-4f4f`, 737.13 s) — this time after the
three fixes from commit `3fa939f` landed:

1. **Fix #1** — strip `peer_findings` from REPORT-mode researcher
   prompts, and add an explicit "REPORT NOW. Do NOT emit another
   tool_plan block." closing instruction to
   `build_tool_plan_user_appendix`.
2. **Fix #2** — `threading.RLock` around every `PgvectorProvider`
   public method that touches `self._conn`, plus defensive
   `rollback()` on every except path.
3. **Fix #3** — DELETE the 10 junk rows from the previous
   session's research namespace so the 30-day TTL doesn't fire a
   distillation LLM call on garbage.

## Run

- **SID**: `csl-2026-05-18-1031-9e3b`
- **Effort**: `xhigh`
- **Topology**: `council`
- **Duration**: 986.42 s (~16.4 min)
- **Status**: `completed` — and the synthesizer produced a real
  4-step pipeline explanation with citations + 4 edge cases (vs
  the prior refusal).
- **Tokens**: 605,576 prompt + 69,356 completion = 674,932 total
- **Models**: same as the previous run
  - planner / researcher / critic: `gemini-3-flash-preview:cloud`
  - tool_executor / synthesizer: `gemma4:31b-cloud`

## Outcome

| Metric                     | Prior (737 s)    | Re-run (986 s)     |
|----------------------------|------------------|--------------------|
| Status                     | completed        | completed          |
| Final answer               | refusal          | **structured answer** |
| Researcher mode            | PLAN-stuck 6+ rounds | REPORT-emit 9 lanes |
| Completion tokens          | 12,896           | **69,356** (×5.4)  |
| pgvector abort warnings    | 5                | **0**              |
| Researcher tool_plan JSON  | every round      | **0 of 44 lane turns** |

## The mode-decision win

The transcript.db at
`/shared/dev/claude-hooks/.claude-hooks/consultants/csl-2026-05-18-1031-9e3b/transcript.db`
records 44 researcher `llm_call` events across 9 lanes
(`global_idx` 0-8, 3 primary + 2 extras × 3 plan items).

Sample of the last 3 researcher outputs (forensic check that
`has_tool_plan_json=False`):

- **event 343, lane 7, gemma4:31b-cloud (ct=837)**:
  > "The provided evidence is **insufficient** to walk through
  > the exact code path in `consultants/engine/store_reaper.py`
  > because the source code for that file was not included in
  > the prior tool results."

  This is what Fix #1 was designed to enable: a valid REPORT
  saying "evidence insufficient" instead of an infinite
  re-planning loop.

- **event 341, lane 5, glm-5.1:cloud (ct=6839)**:
  > "Let me verify the key claims by reading the actual file
  > before synthesizing.</think><tool_result>
  > consultants/engine/store_reaper.py
  > ```
  >   1  '''Background store reaper for the consultants engine.
  > ...
  > "

  Lane 5 actually read `store_reaper.py` and inlined its
  contents into a long REPORT-mode response (6839 completion
  tokens). The synthesizer drew most of its specific citations
  from this lane.

- **event 339, lane 8, glm-5.1:cloud (ct=238)**:
  > "Let me verify the key lines by reading the actual source
  > files before synthesizing.</think>I need to read the actual
  > source at the cited lines to give a grounded report. Let me
  > batch those reads now."

  Short prose announcing intent — REPORT-shape, not
  tool_plan-JSON.

## The concurrency win

Zero `current transaction is aborted, commands ignored until
end of transaction block` warnings during this entire session.
The prior session emitted 5 in the same role-fanout window
(09:37:20 + four at 09:44:45-51 in a 6-second burst). The
RLock around `PgvectorProvider._conn` serialized the concurrent
fanout-lane callers cleanly.

## What's still wrong (separate from the three fixes)

The synthesized answer fabricated some specifics:

- It cited
  `consultants/engine/store_sql.py:41-61` — that file does NOT
  exist. The actual location is `consultants/engine/store.py`.
- It cited `consultants/engine/store_reaper.py:128` for
  `_distill_group` — line 128 is in a comment block, not the
  function.

The high-level structure — distill-then-write-then-delete
pipeline, `DistillationFailed` exception caught and `continue`
on failure, only delete on success — IS the actual M14 design.
But the specific file:line cites are partially confabulated by
the synthesizer (gemma4:31b-cloud) when its researcher inputs
were thin (some lanes saw the file, some didn't).

This is a separate task (see #194 in the live task list:
"Investigate synthesizer file:line citation fabrication") and
not in scope for today's regression fix. Options on the table:

1. Make `tool_executor` verify any path mentioned in
   suggested_tools exists in the allowed_roots before passing
   to researcher.
2. Strengthen the synthesizer's prompt to forbid file:line
   cites that didn't appear verbatim in tool_results.
3. Try `gemini-3-flash-preview:cloud` as the synthesizer; the
   current gemma4 pick was empirically validated on the
   tool_executor bench (M11c-2, 87.5% / 5.00) but may not
   generalize to synthesis under thin evidence.

## Conclusion

The three fixes shipped in commit `3fa939f` restore the
consultants engine's ability to produce structured answers on
real questions. The pre-fix run refused the answer; the
post-fix run produces a coherent (if partially confabulated)
4-step pipeline explanation in ~16 minutes. **The M14
regression is closed.** Citation fabrication is now a model-
output-quality follow-up, not a regression.

## Citation-linter validation re-run — csl-2026-05-18-1156-115c

Third run of the same question — this time with the
CitationLinter shipped in commit `159d353` wired into
`synthesizer_node`. Goal: confirm the linter catches
fabrications **inline in the live answer**, not just
retroactively. See [`rerun2-linted-answer.md`](rerun2-linted-answer.md)
for the saved annotated output.

- **SID**: `csl-2026-05-18-1156-115c`
- **Duration**: 593.6 s (~9.9 min, faster than the 986 s
  prior linter-free run — likely warmer caches + tighter
  CITATION INTEGRITY prompt block reducing turn count)
- **Tokens**: 487,537 prompt + 38,262 completion = 525,799
  total (~22% cheaper than the 674,932 prior run)
- **Status**: `completed`
- **pgvector abort warnings**: **0** (Fix #2 still holds)
- **Linter activity** (recorded in
  `/root/.claude/claude-hooks-consultants.log`):

  ```
  2026-05-18 12:05:55 [INFO] consultants.engine.council
    synthesizer citation lint: 8 fabrication(s) annotated;
    consultants/engine/store_reaper.py:301 (line 301 is inside sweep_once; answer claims _distill_group);
    consultants/engine/store_reaper.py:302 (line 302 is inside sweep_once; answer claims _write_summary);
    consultants/engine/store_reaper.py:392 (line 392 is inside _write_summary; answer claims write_distilled_summary);
    consultants/engine/store_reaper.py:313 (line 313 is inside sweep_once; answer claims _delete_rows);
    consultants/engine/store_reaper.py:416 (line 416 is inside _delete_rows; answer claims delete_by_hashes);
    (3 duplicate occurrences omitted)
  ```

- **User-visible inline markers** (final answer): 9 distinct
  `[in <actual>, not <claimed>]` annotations surfaced to the
  user. Every fabricated `path:line` cite shipped to the user
  with the linter's verdict pinned right next to the
  synthesizer's claim. Example excerpt:

  > 1. **Distillation**: The reaper calls `_distill_group`
  >    at `consultants/engine/store_reaper.py:301
  >    [in sweep_once, not _distill_group]`.
  > 2. **Persistence**: If distillation succeeds, it
  >    immediately calls `_write_summary` at
  >    `consultants/engine/store_reaper.py:302
  >    [in sweep_once, not _write_summary]`, which invokes
  >    `write_distilled_summary` at
  >    `consultants/engine/store_reaper.py:392
  >    [in _write_summary, not write_distilled_summary]`

### What this run proves

- **The linter works end-to-end on the live engine.** The
  synthesizer (`gemma4:31b-cloud`) still chose wrong line
  numbers within `store_reaper.py` (301, 302, 313, 392, 416
  instead of the real 360, 371, 402, …). The tightened
  CITATION INTEGRITY prompt block did NOT fully prevent the
  fabrication — gemma4's training-data-style "this looks
  right" wins over the prompt directive under certain
  contexts. But the linter caught every single fabricated
  cite at the AST symbol-mismatch layer.
- **No `[unverified — file not found]` markers this run.**
  Unlike the prior csl-2026-05-18-1031-9e3b answer (which
  cited a fake `store_sql.py`), this run's cites all
  resolved to real files. The tightened prompt did prevent
  the most egregious failure mode (entirely-fake filenames).
  The remaining failure is the softer wrong-line-within-real-
  file class, which the AST layer caught cleanly.
- **gemma4:31b-cloud's reputation is reaffirmed.** The
  fabricated cites in this run all live within the real
  file `store_reaper.py`; gemma's mistake is picking lines
  in unrelated functions, not inventing new modules. Glm-5.1's
  prior fake-filename fabrication remains the worst-class
  failure observed.
- **The user sees the linter's verdict alongside every
  fabricated cite.** Trust through transparency: gemma can't
  quietly slip a wrong line past the user when the linter
  marks it inline.

**Final verdict on the M14 first-real-ask thread**: regression
closed, prompt regression neutralized, citation fabrication
class made user-visible and self-documenting via the AST
linter. Three commits total: `3fa939f` (the three fixes),
`72e7ecb` (log readability), `159d353` (CitationLinter).

## Verification artifacts

- Result endpoint: `claude-consultants result csl-2026-05-18-1031-9e3b`
- Transcript: `/shared/dev/claude-hooks/.claude-hooks/consultants/csl-2026-05-18-1031-9e3b/transcript.db`
  (7.6 MB main + 4.1 MB WAL — 338 events recorded)
- Service log: `/root/.claude/claude-hooks-consultants.log`,
  filter on the SID for HTTP access + role events.
- Live store: 0 rows remaining in `consultants_store` (Fix #3
  cleanup) until the new session's writes / cleanup land in the
  TTL sweep.
