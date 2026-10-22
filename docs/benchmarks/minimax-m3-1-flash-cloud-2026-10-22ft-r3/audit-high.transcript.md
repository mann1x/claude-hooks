# Consultation transcript — csl-2026-10-22-2115-f841

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. Trace `CouncilState` channel merging in `consultants/engine/graph.py:50-80` and the `Send` payloads in `consultants/engine/graph.py:349-385`, verifying how successful lane deltas and the failed lane’s `research`, `turns`, `error`, and `_role_failed` updates merge, especially whether `error` is overwritten rather than additive.
2. Inspect `researcher_node`’s `loop_runner` call and exception boundary in `consultants/engine/council.py:546-573`, confirming whether an exception from `claude_hooks.agent_loop.runner.run_loop` is converted into a tombstone report `(researcher lane failed: ...)` instead of escaping the LangGraph node.
3. Inspect `consultants/server/runner.py` (currently unverified because tool access ended) for the `compiled.stream(...)` loop, its `try/except` boundaries, yielded event handling, final-state extraction, status construction, and artifact-writing calls, citing the exact exception propagation and `status=failed` decision points.
4. Trace `build_synthesizer_messages` in `consultants/engine/council.py:245-266` and its invocation in `synthesizer_node` at `consultants/engine/council.py:715-726`, verifying from the merged `research` list exactly which lane reports and failure tombstones become part of the synthesizer’s user message and whether `state["error"]` is exposed to it.
5. Inspect `consultants/engine/storage.py` (currently unverified because tool access ended) for the artifact writer’s handling of `final_answer`, `turns`, `error`, and `_role_failed`, then follow the runner’s status assignment to determine whether a researcher-lane tombstone yields a completed consultation with a degraded answer or a failed consultation, and identify the single best place to harden that behavior.

---

## Researcher (round 1)

## Findings — grounded trace of a failing researcher lane in the Send fan-out path

**1. The exception is caught at the node boundary, not in the runner.**
- `researcher_node` wraps the whole `loop_runner(...)` call in `try/except Exception` — `consultants/engine/council.py:547-555`. The runner is `claude_hooks.agent_loop.runner.run_loop` (default import at `consultants/engine/council.py:497-499`).
- A `tool_executor` that raises propagates out of `execute_tool_calls` (`claude_hooks/agent_loop/runner.py:122` → `claude_hooks/agent_loop/runner.py:299-301`) → out of `run_loop` → into that `except`. A **non-string** tool result doesn't raise at the executor (`claude_hooks/caliber_proxy/tools.py:439` coerces to `str`), but `len(output)` in the loop's log line (`claude_hooks/agent_loop/runner.py:126`) raises `TypeError` for `None` → same catch path. So: **the exception never reaches LangGraph**; the node returns normally.

**2. The failed lane returns a tombstone delta (this is already hardened).**
- Except-branch return: `consultants/engine/council.py:562-573` → `error="researcher failed: {e}"`, `_role_failed="researcher"`, `research=["(researcher lane failed: {e})"]`, `research_rounds_used=1`, one `RoleTurn`.
- Rationale is documented inline (`consultants/engine/council.py:557-561`) and pinned by a test: `tests/test_consultants_council.py:531-561`.
- **Gap:** the tombstone text carries **no lane identity** (`consultants/engine/council.py:562`), even though `lane_idx` is available in state and the empty-text fallback path does log it (`consultants/engine/council.py:586-591`). With `FANOUT_MAX_LANES = 3` (`consultants/engine/council.py:307`), neither the synthesizer nor the transcript can tell *which* of the up-to-3 plan items died.

**3. Reducer semantics: `research`/`turns` are additive; `error`/`_role_failed` are NOT.**
- `CouncilState` declares `research: Annotated[list, operator.add]` (`consultants/engine/graph.py:64`) and `turns: Annotated[list, operator.add]` (`consultants/engine/graph.py:68`); `error` (`consultants/engine/graph.py:74`) and `_role_failed` (`consultants/engine/graph.py:75`) are plain `Optional[str]` → default (last-write-wins) reducer, i.e. **overwritten, not additive**.
- Consequence A: if a later role also fails, its `error` **overwrites** the researcher's (`consultants/engine/council.py:684`, `consultants/engine/council.py:737`) — metadata keeps only the last role failure.
- Consequence B (unverified, flagged): two lanes failing in the *same* superstep would deliver two concurrent writes to the non-additive `error` channel; LangGraph's default last-value channel raises `InvalidUpdateError` on >1 value in a step, which would escape `compiled.stream(...)` and hit the graph-crash handler. LangGraph is not vendored in this repo (`consultants/pyproject.toml:17` pins it as an external dep), so I could not verify this from source — treat as a real risk, not a confirmed bug.
- Also note the failed lane inflates the additive counter `research_rounds_used` to the lane count (3 at medium), which trips `route_after_critic`'s round cap (`consultants/engine/council.py:383-389`) → any critic-requested re-route is silently cancelled.

**4. What the synthesizer actually sees.**
- The tombstone is just another entry of `state["research"]`; `build_synthesizer_messages` renders each as `"\nRESEARCHER REPORT (round {i}):\n{r}"` (`consultants/engine/council.py:245-266`, loop at `:253-254`). Surviving lanes' reports are present too.
- The synthesizer's system prompt forbids mentioning the process/roles and demands a bottom-line answer (`consultants/engine/council.py:156-168`, esp. `:164-167`). Nothing in the prompt says "a report that starts with `(researcher lane failed:` is a hard gap you must surface". So the model sees the tombstone as a degenerate report and most plausibly **answers from the surviving lanes and silently omits the failed lane's investigation area**. The answer is coherent-sounding but incomplete — a "confidently degraded" answer (exactly the risk the tombstone comment at `consultants/engine/council.py:559-561` was written to prevent, only partially mitigated).

**5. The runner marks the whole session failed — even though a real answer exists.**
- `compiled.stream(initial, stream_mode=["updates","values"])` is wrapped in `try/except Exception` at `consultants/server/runner.py:136-161`; the graph-level crash branch sets `state.status="failed"` and calls `_write_failed_artifacts` (`consultants/server/runner.py:155-161`, `:211-237`).
- But a lane failure does **not** take that branch: the node returned normally, the stream drained, and the *node-level* error is read at `consultants/server/runner.py:172-174` → `node_error = final_state.get("error")` is truthy → `terminal_status = "failed"`.
- Artifacts are still written with the real synthesizer output: `consultants/server/runner.py:177-199` — `final_answer=final_state.get("final_answer","")` (`:186`), `status="failed"` (`:189`), `error=node_error` (`:190`), then `storage.write_consultation(...)` (`:199`).
- Progress lies in the meantime: the failed researcher node still emits an update, so `state.progress["researcher"]` flips to `"done"` (`consultants/server/runner.py:144-146`).

**6. Storage artifact writer.**
- `storage.write_consultation` writes `summary.md`, `transcript.md`, `metadata.json` atomically (`consultants/engine/storage.py:200-208`).
- `summary.md` = YAML front-matter + the synthesizer's real answer body (`consultants/engine/storage.py:97-120`); front-matter carries `status: failed` and `error: researcher failed: ...` (`consultants/engine/storage.py:108-110`).
- `metadata.json` = `asdict(result)` (`consultants/engine/storage.py:166-169`) → `status="failed"`, `error` set.
- `transcript.md` iterates `turns` (`consultants/engine/storage.py:129-136`) → the failed lane appears as a `## Researcher` section whose body is the tombstone text; lane identity is absent (see finding 2). Round numbering is also ambiguous: all fan-out lanes use `round=1` (`consultants/engine/council.py:507-508`), and `_is_multi_round` (`consultants/engine/storage.py:161-163`) then labels every researcher section "(round 1)".

**7. What the user actually perceives.**
- The engine produces a **coherent-but-silently-incomplete** `final_answer` and writes it to disk — it does **not** abort the council.
- But the session surfaces as `status="failed"`, `error="researcher failed: <exc>"` (`consultants/server/runner.py:201-202` → `consultants/server/app.py:61-75`).
- The `/consultants` skill only fetches the result on `status: completed` (`.claude/skills/consultants/SKILL.md:104-109`); on `status: failed` it instructs the agent to surface the role error and suggest switching models (`.claude/skills/consultants/SKILL.md:134-147`). **Net user experience: "the consultation failed", and the answer that was actually produced is never surfaced** — recoverable only by manually running `claude-consultants result <sid>` / `show` (`consultants/cli.py:148-151`, `:162-180`), and that artifact is itself labelled `status: failed`.
- Secondary inconsistency: the CLI exit-code contract (`consultants/cli.py:4`) has no path for "degraded but usable", and `sessions.json` records `status: failed` (`consultants/engine/sessions_index.py:116-138`).

## Answer to the question
The council does **not** fail as a whole: the exception is absorbed at `consultants/engine/council.py:555`, the lane contributes a tombstone (`consultants/engine/council.py:562-573`), the other lanes' `research`/`turns` merge additively (`consultants/engine/graph.py:64`, `:68`), and the synthesizer runs and writes a real answer. The failure is a **status-classification** failure, not a pipeline failure: `consultants/server/runner.py:174` maps any node-level `error` to `status="failed"`, so the user is told the consultation failed and (per the skill) never receives the degraded answer — and the degraded answer they *would* receive is missing the failed lane's topic silently, because the tombstone carries no lane identity and the synthesizer prompt tells it not to mention process (`consultants/engine/council.py:162-167`).

## Recommended single hardening change
**`consultants/server/runner.py:172-174`** — stop conflating "a role degraded" with "the consultation failed". Compute the terminal status from whether the synthesizer actually produced an answer, and report role failures as a separate degraded field instead of forcing `status="failed"`:

```python
node_error = final_state.get("error")
node_failed = final_state.get("_role_failed")
final_answer = final_state.get("final_answer") or ""
# A role-level tombstone (researcher lane failed, critic defaulted)
# must not masquerade as a failed consultation: the synthesizer still
# produced an answer. Reserve "failed" for a missing/unusable answer.
terminal_status = (
    "failed" if (not final_answer.strip() or node_failed == "synthesizer")
    else "completed"
)
```
and surface the role failure in the artifacts (metadata `error=` is already wired at `consultants/server/runner.py:190`; add e.g. `degraded_role=node_failed` to `ConsultationResult`, `consultants/engine/storage.py:54-72`) so the user sees "answer delivered, one lane failed" instead of a false total failure.

Secondary observations (not recommendations, evidence-backed): add `lane_idx` to the tombstone text at `consultants/engine/council.py:562` so the synthesizer/transcript can name the dead lane; and make `error`/`_role_failed` non-clobbering (`consultants/engine/graph.py:74-75`) so multiple role failures don't lose each other or risk a concurrent-write `InvalidUpdateError`.

---

## Researcher (round 1)

## Failure path (grounded)

**Step 0 — the exception reaches `researcher_node`'s handler.** `run_loop` has no try/except around `chat_fn` (`claude_hooks/agent_loop/runner.py:230`) or around the tool executor (`claude_hooks/agent_loop/runner.py:122`); a `RuntimeError` from the exhausted retry budget (`claude_hooks/get_advice/chat_client.py:285`, `:302`; budget `max_retries=8` at `:31`) or a `TypeError` from `len(output)` on a non-string tool result (`claude_hooks/agent_loop/runner.py:125`) propagates out of `run_loop` into `consultants/engine/council.py:555`. (The production executor cannot produce a non-string: `claude_hooks/caliber_proxy/tools.py:438-439` coerces to `str` and wraps tool errors; a custom `tool_executor` can, and `traced_tool` re-raises — `consultants/engine/trace.py:222`.)

**Step 1 — reducers.** The handler returns a tombstone delta, not an exception (`consultants/engine/council.py:563-573`): `"error"` (`:564`), `"_role_failed"` (`:565`), `"research": [err_text]` (`:566`), `"research_rounds_used": 1` (`:567`), one `RoleTurn` (`:568-572`). `research`/`turns`/`research_rounds_used` are additive (`consultants/engine/graph.py:64`, `:68`, `:69`) so the tombstone merges cleanly with the surviving lanes; `error`/`_role_failed` are **plain (non-additive)** channels (`consultants/engine/graph.py:74-75`).

**Step 2 — `compiled.stream`.** No exception reaches the runner: the node returned normally, so the graph continues to critic/synthesizer. The runner's `try/except` around the stream loop (`consultants/server/runner.py:136-161`) is *not* entered. The only path that sets `status="failed"` there is a genuine graph crash (`:157-158`).

**Step 3 — what the synthesizer sees.** `synthesizer_node` passes only question, plan, `state["research"]`, and `state["critique"]` (`consultants/engine/council.py:718-723`) → `build_synthesizer_messages` (`consultants/engine/council.py:245-266`) renders each merged `research` entry as a `RESEARCHER REPORT (round N)` block (`:253-254`). So the synthesizer sees the string `"(researcher lane failed: <exc>)"` as one of its "researcher reports". It does **not** see `state["error"]` or `state["_role_failed"]` (never passed at `:718-723`), does **not** see which lane/sub-plan died (the tombstone text at `council.py:562` carries no `lane_idx` and no `plan_item`), and is instructed "Do NOT mention the council, the roles, or the process" (`consultants/engine/council.py:164-165`) — so it is told to *not* disclose the gap.

**Step 4 — storage.** `final_answer` (the synthesizer's real text), `turns` (including the tombstone turn), `status`, `error` are all persisted: `consultants/server/runner.py:177-199` → `storage.write_consultation` (`consultants/engine/storage.py:200-208`) writes `summary.md` (front-matter `status: failed` at `storage.py:108`, `error:` at `:109-110`, body = `final_answer` at `:119-120`), `transcript.md` (iterates `turns`, `storage.py:129-158`), `metadata.json` (`storage.py:166-169`).

**The status flip.** `node_error = final_state.get("error")` → `terminal_status = "failed" if node_error else "completed"` (`consultants/server/runner.py:172-174`), then `state.status = terminal_status` and `state.error = node_error` (`:201-202`). Because the tombstone set `error` (`:564`), the tombstone's `error` channel value survives (no later node clears it) → **`status="failed"`** even though a complete, coherent `final_answer` was produced and written to disk.

## Answer to the question

The council **does** produce a coherent-but-degraded answer; it does **not** fail at the graph level. But the session is still surfaced as `status=failed`. Two distinct degraded behaviors:

- **Coherent answer, failed label.** The synthesizer's text is in `summary.md` and `metadata.json` (`runner.py:186-190`), and `/v1/consult/{sid}/result` returns it regardless of status (`consultants/server/app.py:263-279`). But the `/consultants` skill only fetches the result "Once `status: completed`" (`.claude/skills/consultants/SKILL.md:106`) and on `failed` instructs the agent to surface the role + error instead (`.claude/skills/consultants/SKILL.md:136-147`). **Perceived failure mode:** the user is told "consultation failed: researcher failed: HTTP timeout exhausted retries" and never sees the answer that was already produced; on disk the answer is filed under `status: failed` front-matter (`storage.py:108`), so downstream consumers that read YAML front-matter (`storage.py:6-9`) treat it as a failed consultation.
- **Silently incomplete answer.** If the user does read `summary.md`, the answer omits the failed lane's sub-question with no marker: the tombstone is visible in the synthesizer's *input* (`council.py:253-254`) but carries no lane identity (`council.py:562`) and the system prompt forbids mentioning the process (`council.py:164-165`). Secondary effect: the failed lane's `research_rounds_used: 1` (`council.py:567`) is added to each surviving lane's `1` (`council.py:660`) via the additive reducer (`graph.py:69`), so a 3-lane fan-out at effort=high consumes all 3 rounds (`council.py:72` cap = 3) and `route_after_critic` blocks any re-route (`council.py:383-389`) — the failed lane is never retried.

## Recommended single hardening change

**`consultants/server/runner.py:174`** — make the terminal status role-aware instead of error-presence-based:

```
terminal_status = "failed" if node_error else "completed"
```

should be derived from the *role* that failed, which is already available one line above at `consultants/server/runner.py:173` (`node_failed = final_state.get("_role_failed")`, populated at `consultants/engine/council.py:565`): only a `synthesizer` failure (terminal — no answer exists, `council.py:736-747`) or a graph-level crash (`runner.py:155-161`) should be `failed`; a `researcher` / `critic` / `planner` tombstone should yield `status="completed"` with the failure retained in `state.error` and a `degraded`/partial marker, so the answer in `summary.md` is actually surfaced to the user instead of being withheld behind a `failed` status. One-line change at `runner.py:174`; the `_role_failed` channel it needs already exists (`consultants/engine/graph.py:75`).

Adjacent risk (flagged, **not** the recommendation): `error` and `_role_failed` are non-additive channels (`consultants/engine/graph.py:74-75`) while every other key written by parallel `Send` lanes is additive (`:64-72`). If **two** lanes fail in the same superstep, both write `error`; under langgraph's `LastValue` channel semantics that can raise `InvalidUpdateError` (concurrent-update error), which would be caught at `consultants/server/runner.py:155` and escalate to `status="failed"` with *no* answer at all. I could not verify this locally — langgraph is not vendored in this repo and is only pinned (`consultants/pyproject.toml:17`, `langgraph>=0.2,<0.4`).

---

## Critic (round 2)

DECISION: ready

The evidence is sufficient for the requested trace. A researcher exception is caught at the node boundary and converted into a normal tombstone delta containing `error`, `_role_failed`, a synthetic research report, and a turn (`consultants/engine/council.py:547-573`); additive reducers merge `research`, `turns`, and round counters, while `error` and `_role_failed` are ordinary last-write channels (`consultants/engine/graph.py:60-75`). The exception therefore does not escape through `compiled.stream`’s graph-crash handler (`consultants/server/runner.py:136-161`); the synthesizer receives the tombstone as one of the rendered researcher reports, without the separate `error` field (`consultants/engine/council.py:245-266,715-727`). The runner nevertheless maps the surviving node error to `status="failed"` (`consultants/server/runner.py:171-174`) and writes both the synthesizer’s real `final_answer` and the failed status/error to the result (`consultants/server/runner.py:177-199`; `consultants/engine/storage.py:97-120,129-169`). Thus the pipeline is not aborted: a usable but potentially incomplete answer can be produced, but the user-facing consultation is classified as failed and may be withheld by status-gating consumers. The single best hardening change is at `consultants/server/runner.py:172-174`: distinguish terminal failure from a researcher-lane tombstone, marking the consultation completed/degraded when an answer exists and reserving `failed` for graph crashes or synthesizer failure. The exact downstream wording is unverified because the cited skill file was unavailable: `.claude/skills/consultants/SKILL.md:104-147`; checking that file would confirm whether the answer is explicitly withheld.

---

## Synthesizer

The exception is contained, but the outcome is contradictory: the council continues to the synthesizer and writes a real, potentially coherent but incomplete answer, while the server classifies the entire consultation as `status="failed"` (`consultants/engine/council.py:547-573`; `consultants/server/runner.py:171-199`).

1. **Reducer merge and lane failure**
   - Any exception escaping `run_loop` is caught by `researcher_node`’s `except Exception`; it does not escape the LangGraph node (`consultants/engine/council.py:547-555`). A tool executor exception propagates through `run_loop` into that handler (`claude_hooks/agent_loop/runner.py:122 [no run_loop at this line; line is in execute_tool_calls]`; `claude_hooks/agent_loop/runner.py:299-301`).
   - The failed lane returns a normal delta containing:
     - `error="researcher failed: …"`
     - `_role_failed="researcher"`
     - `research=["(researcher lane failed: …)"]`
     - one additive research-round count
     - one `RoleTurn` containing the tombstone  
     (`consultants/engine/council.py:562-573`).
   - `research`, `turns`, and `research_rounds_used` use additive reducers, so the tombstone merges with successful lanes’ reports and turns (`consultants/engine/graph.py:60-69`). `error` and `_role_failed` are ordinary, non-additive fields, so they follow last-write semantics rather than preserving a list of failures (`consultants/engine/graph.py:73-75`). In the single-failed-lane scenario, the researcher’s values remain present.

2. **`compiled.stream` and status**
   - Because the exception was converted into a normal node result, `compiled.stream` drains without entering its graph-crash handler; that handler is reserved for an exception escaping the graph itself and separately sets `status="failed"` (`consultants/server/runner.py:136-161`).
   - The failed researcher node still emits an update, so the runner can mark researcher progress as `"done"` (`consultants/server/runner.py:144-146`).
   - After the stream completes, the runner reads the surviving node-level `error` and applies `terminal_status = "failed" if node_error else "completed"` (`consultants/server/runner.py:171-174`). Thus the lane exception does not abort the council, but it does cause the whole consultation to be reported as failed.

3. **What the synthesizer sees**
   - The synthesizer receives the question, plan, every merged `research` entry, and optional critique (`consultants/engine/council.py:718-723 [no _role_failed at this line; line is in synthesizer_node]`).
   - `build_synthesizer_messages` renders every research entry—including the tombstone—as a `RESEARCHER REPORT (round N)` block (`consultants/engine/council.py:245-266`). It does **not** pass the separate `error` or `_role_failed` fields (`consultants/engine/council.py:718-723 [no _role_failed at this line; line is in synthesizer_node]`).
   - The tombstone does not identify the failed lane or its plan item (`consultants/engine/council.py:562-573`). Moreover, the synthesizer prompt says not to mention the council, roles, or process (`consultants/engine/council.py:156-167`).
   - Consequently, the synthesizer can produce a normal-looking answer from the surviving reports, but it has no structured reason or instruction to identify the missing investigation area. The degraded answer may therefore silently omit that sub-question rather than clearly disclose the gap.

4. **Artifact and user-visible failure**
   - The runner constructs a result containing the synthesizer’s actual `final_answer`, the merged turns, `status="failed"`, and the researcher error, then writes it (`consultants/server/runner.py:177-199`).
   - `summary.md` contains `status: failed` and the error in YAML front matter, followed by the synthesized answer body (`consultants/engine/storage.py:97-120`). `transcript.md` contains the merged turns, including the researcher tombstone (`consultants/engine/storage.py:129-158 [no md at this line; line is in render_transcript]`), and `metadata.json` records the failed status and error (`consultants/engine/storage.py:166-169`). The write itself is atomic (`consultants/engine/storage.py:200-208`).
   - Therefore, the direct result contains a usable answer despite being labelled failed (`consultants/server/app.py:263-279`). The definitive user-facing symptom is nevertheless **“consultation failed: researcher failed: …”**; status-gated clients may withhold the already-generated answer. Whether the specific `/consultants` client suppresses it could not be verified because its skill file was unavailable.

**Exactly one hardening change:** make terminal status role-aware at `consultants/server/runner.py:172-174`. Reserve `failed` for a graph crash, synthesizer failure, or missing final answer; classify a researcher-lane tombstone with a nonempty final answer as `completed` while retaining the error for diagnostics:

```python
final_answer = final_state.get("final_answer") or ""
node_error = final_state.get("error")
node_failed = final_state.get("_role_failed")
terminal_status = (
    "failed"
    if node_failed == "synthesizer" or not final_answer.strip()
    else "completed"
)
```

A synthesizer failure is already represented explicitly by `_role_failed="synthesizer"` and an incomplete placeholder answer (`consultants/engine/council.py:727-747`).

---
