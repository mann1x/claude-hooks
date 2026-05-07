# Consultation transcript — csl-2026-05-07-0958-04c1

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. Inspect `consultants/engine/graph.py` for the `CouncilState` TypedDict and all annotated reducers (likely `operator.add` or `Annotated[list, add]`); note which fields are additive vs. overwritten, and whether any reducer has a fallback/wrapper that catches exceptions from upstream nodes.

2. Find the fan-out edge from the planner node to researcher lanes in `consultants/engine/graph.py`; determine whether the graph uses LangGraph's `Send()` API or conditional edges, and whether each lane's output writes to the *same* additive state key or separate keys.

3. Read the `runner.py` compiled-graph execution (`compiled.stream` or `compiled.ainvoke` loop) — look for `try/except` around chunk iteration, any `on_error` or `interrupt_before` config, and whether a per-node exception terminates the stream or is swallowed/logged.

4. Inspect the synthesizer node's function body: enumerate which state keys it reads, whether it assumes a fixed-length researcher list, and how it concatenates/merges messages — specifically test what happens if one researcher's key is missing or is an empty list.

5. Read `consultants/engine/storage.py` (or wherever the artifact writer lives) for the post-synthesis persistence call: does it guard against `None`/empty `answer` fields, and what status does it write when the graph completes with partial data versus when the graph raises?

6. Search for any LangGraph `retry_policy`, `max_retries`, or `error_handler` configuration applied at node or graph construction time in `graph.py` — this determines whether a failing researcher lane is retried, skipped, or propagates the exception to the stream.

---

## Researcher (round 1)

Now I have everything needed. Here's the focused report:

---

**1. CouncilState additive reducers** (`consultants/engine/graph.py:50-80`):
- Additive (list concat): `research: Annotated[list, operator.add]` (:64), `turns: Annotated[list, operator.add]` (:68)
- Additive (int sum): `research_rounds_used` (:69), `critic_reroutes_used` (:70), `total_prompt_tokens` (:71), `total_completion_tokens` (:72)
- Non-additive (default dict-merge, last-write-wins): `error` (:74), `_role_failed` (:75), `critique` (:65), `critic_decision` (:66), `final_answer` (:67), `plan` (:56), `plan_items` (:59)
- No reducer has any fallback/wrapper that catches upstream exceptions; exception handling lives inside each node function.

**2. Fan-out edge** (`consultants/engine/graph.py:348-386`):
- Uses LangGraph `Send("researcher", {...})` — each lane is an independent researcher invocation. All lanes write to the **same** additive state keys (`research`, `turns`, `research_rounds_used`, `total_*_tokens`), which merge via `operator.add`/`operator.add`. Non-additive keys (`error`, `_role_failed`) are not present in the Send payload (:362-382), so they start absent per-lane.

**3. Researcher exception handling** (`consultants/engine/council.py:547-573`):
- `run_loop` exception is caught at `:555`. Returns a tombstone dict containing:
  - `"research": [err_text]` — **additive**, merges into the list alongside successful lanes
  - `"error": f"researcher failed: {e}"` — **non-additive**, persists because no subsequent node clears it
  - `"_role_failed": "researcher"` — **non-additive**, same problem
  - `"turns": [RoleTurn(...)]` — **additive**, merges normally
- The comment at `:557-561` explicitly acknowledges this was added after "the audit-high (csl-...-faa2) finding" where a crashed lane silently contributed nothing.

**4. Synthesizer input** (`consultants/engine/council.py:715-722`):
- `build_synthesizer_messages` at `:245-266` reads `state.get("research") or []` (:720). This is the additive-concatenated list of ALL lane outputs. The failed lane's `"(researcher lane failed: <exc>)"` tombstone is mixed in with successful lane reports. The synthesizer sees it as a legitimate research round and will attempt to work around it — producing a degraded answer, not an error message.

**5. Runner stream loop** (`consultants/server/runner.py:136-161`):
- The try/except at `:155-161` catches **graph-level** crashes only (the entire `compiled.stream()` raises). Node-level exceptions caught by node handlers (like the researcher tombstone) do NOT trigger this path; the stream continues.
- `final_state` is captured from `mode == "values"` events (:139-140), reflecting the fully-merged state after each superstep.

**6. Terminal status logic** (`consultants/server/runner.py:171-174`):
```python
node_error = final_state.get("error")
node_failed = final_state.get("_role_failed")
terminal_status = "failed" if node_error else "completed"
```
The `error` key was set by the failed researcher lane. **No subsequent node (critic, synthesizer) clears it** — their success paths (`council.py:705-712`, `:753-758`) omit `error` from their return dicts, so the researcher's `error` persists via last-write-wins semantics. Result: `terminal_status = "failed"`.

**7. Storage** (`consultants/engine/storage.py:117-120`, `:200-207`):
- `render_summary` writes `result.final_answer` as the body + YAML front-matter with `status: "failed"` and `error: "researcher failed: ..."`.
- The user sees a **failed** consultation in metadata, but the `final_answer` field contains the synthesizer's real (degraded) answer.

**Conclusion**: The council still produces a coherent-but-degraded answer (synthesizer saw successful lanes' findings + failure tombstone), but the consultation is marked `status: "failed"` because the non-additive `error` field from the failed researcher lane is never cleared by subsequent successful nodes. The user perceives a failed consultation even though a partial answer exists in `final_answer`.

**Recommended hardening change** — `consultants/server/runner.py:171-174`:

Replace the binary `failed/completed` with a three-tier check. If `error` is set but `final_answer` is non-empty (synthesizer actually produced output), downgrade to `"degraded"` so the user sees a partial answer rather than a full failure:

```python
node_error = final_state.get("error")
node_failed = final_state.get("_role_failed")
if node_error and final_state.get("final_answer"):
    terminal_status = "degraded"
elif node_error:
    terminal_status = "failed"
else:
    terminal_status = "completed"
```

This is the most surgical fix. A deeper alternative (making `error` an `Annotated[list, operator.add]` field in CouncilState at `graph.py:74`) would require changing all node return dicts and the runner's error-detection logic — higher blast radius for the same outcome.

---

## Researcher (round 1)

Now I have everything needed. Let me trace the exact failure path.

**Findings report:**

1. **CouncilState additive reducers** (`consultants/engine/graph.py:64-72`):
   - `research: Annotated[list, operator.add]` — list concatenation across parallel Send lanes
   - `turns: Annotated[list, operator.add]` — same
   - `research_rounds_used: Annotated[int, operator.add]` — int sum
   - `total_prompt_tokens`, `total_completion_tokens`: `Annotated[int, operator.add]`
   - `error` and `_role_failed` are plain `Optional[str]` — **no reducer**. Last-write-wins across parallel lanes.

2. **Researcher failure path** (`consultants/engine/council.py:547-573`):
   - `run_loop` throws → caught in `researcher_node`'s `except Exception` at line 555
   - Returns tombstone dict: `{"research": [err_text], "error": "researcher failed: ...", "_role_failed": "researcher", "research_rounds_used": 1, "turns": [RoleTurn(...)]}`
   - The `research` list gets `operator.add`-merged with other lanes. A failed lane contributes `"(researcher lane failed: ...)"` as one entry in the merged `research` list.
   - `error` and `_role_failed` are **non-additive** — if lane 0 fails (sets `error`) and lane 1 succeeds, lane 1's return doesn't include `error`/`_role_failed`, so **LangGraph's default merge overwrites the failed lane's error with the success lane's absence-of-key**. The `error` field gets clobbered.

3. **Runner's `compiled.stream` loop** (`consultants/server/runner.py:136-161`):
   - The `try/except Exception` at line 155 catches **graph-level** crashes (if `run_loop`'s exception somehow propagated out of the node and LangGraph re-raised it, terminating the stream). When caught: `state.status = "failed"`, `_write_failed_artifacts()` called, early return.
   - If the node catches the exception internally (as it does), the stream continues normally — no graph-level crash.

4. **Synthesizer's input** (`consultants/engine/council.py:715-723`):
   - `synthesizer_node` reads `state.get("research") or []` at line 720
   - It passes this to `build_synthesizer_messages()` which iterates with `for i, r in enumerate(research_rounds, start=1)` at line 253, emitting `"\nRESEARCHER REPORT (round {i}):\n{r.strip()}"`
   - **No check** for tombstone content. The synthesizer sees `"(researcher lane failed: <exception>)"` as if it were a real research report and synthesizes an answer based on the surviving lanes' findings plus this error string as "evidence."
   - It does **not** check `state.get("error")` or `state.get("_role_failed")` before building its output.

5. **Runner's post-stream logic** (`consultants/server/runner.py:172-174`):
   - `node_error = final_state.get("error")` — but as noted, `error` is non-additive and likely **clobbered** by the surviving lane that doesn't set it. So `node_error` is likely `None`.
   - `terminal_status = "failed" if node_error else "completed"` → **"completed"** even when a lane failed.
   - `state.status = terminal_status` → **"completed"**

6. **Storage artifact writer** (`consultants/engine/storage.py:200-207`):
   - `ConsultationResult.status = terminal_status` = `"completed"`
   - `ConsultationResult.final_answer` = synthesizer's text (which includes the error tombstone as "evidence")
   - `ConsultationResult.error = node_error` = likely `None`
   - Writes `summary.md`, `transcript.md`, `metadata.json` — all say "completed" with no error field.

**Conclusion: The council produces a degraded answer, NOT a failure.** The user sees status=`completed` with a synthesizer answer that was fed `"(researcher lane failed: ...)"` as if it were research. The `error`/`_role_failed` fields are silently lost due to non-additive reducer semantics on parallel Send lanes. The synthesizer has no logic to detect or surface partial-lane failure.

**Recommended hardening change:** Make `error` and `_role_failed` additive reducers so parallel-lane failures accumulate rather than clobber each other, and add a lane-failure check in `synthesizer_node`:

In `consultants/engine/graph.py:74-75`, change:
```python
error: Annotated[list[str], operator.add]
_role_failed: Annotated[list[str], operator.add]
```
Then in `synthesizer_node`, before building messages, check `state.get("error")` and prepend a warning. And in `runner.py:172`, check `isinstance(node_error, list) and len(node_error) > 0` instead of truthiness on a single string.

---

## Researcher (round 1)

Now I have all the code needed. Here's the complete trace:

---

**1. CouncilState additive reducers (`consultants/engine/graph.py`)**

When a researcher lane raises inside `run_loop`, `researcher_node` catches it at `consultants/engine/council.py:555` and returns a tombstone dict. The additive reducers merge it:

- `research: Annotated[list, operator.add]` (`graph.py:64`) — the failed lane's `["(researcher lane failed: ...)"]` is **concatenated** with surviving lanes' research lists. The synthesizer sees the failure marker as just another research round entry.
- `turns: Annotated[list, operator.add]` (`graph.py:68`) — the `RoleTurn` with the error text is appended to the turns list.
- `research_rounds_used: Annotated[int, operator.add]` (`graph.py:69`) — the failed lane contributes `+1`, inflating the round count.
- `error: Optional[str]` (`graph.py:74`) — **not additive**, default dict-merge reducer (last-write-wins). The tombstone sets `error = "researcher failed: ..."`. If a second lane also fails, only the **last** lane's error survives; the first is silently overwritten.
- `_role_failed: Optional[str]` (`graph.py:75`) — same problem: non-additive, only one role name survives from multiple failing lanes.

**2. `runner.py`'s `compiled.stream` loop (`consultants/server/runner.py:136-161`)**

The exception is caught **inside** `researcher_node` (council.py:555), so `run_loop`'s exception never reaches `compiled.stream`. The graph continues through all remaining nodes normally. The `except Exception` at `runner.py:155` is only hit if the **graph itself** crashes (e.g., LangGraph internal error). For a per-lane researcher failure, the stream loop completes without entering the except block.

After the stream drains (`runner.py:171-174`):
```python
node_error = final_state.get("error")          # "researcher failed: ..."
node_failed = final_state.get("_role_failed")   # "researcher"
terminal_status = "failed" if node_error else "completed"
```
`terminal_status` becomes `"failed"` because the tombstone set `error`.

**3. Synthesizer's input messages (`consultants/engine/council.py:715-723`)**

`synthesizer_node` reads `state.get("research") or []` at `council.py:720`, which now contains both real research from surviving lanes AND the tombstone string `"(researcher lane failed: <exc>)"`. `build_synthesizer_messages` at `council.py:253-254` renders all entries as `"RESEARCHER REPORT (round N): ..."`. The synthesizer has **no structural signal** that one report is a failure marker — it's just text. It may cite the failure phrase or silently ignore it, producing an answer based on partial evidence without acknowledging the gap.

**4. Storage artifact writer (`consultants/engine/storage.py:200-208`)**

`write_consultation` is called at `runner.py:199` with `ConsultationResult(status="failed", final_answer=<synth output>)`. It writes all three artifacts atomically. `render_summary` at `storage.py:117-120` puts `final_answer` as the body and `status: "failed"` in YAML front-matter. `storage.py` has **no guard** against empty/None `final_answer` — it just calls `result.final_answer.rstrip() + "\n"` at line 119, which would raise `AttributeError` if `final_answer` were `None`. In practice this doesn't happen because the synthesizer always produces text (even its own tombstone at `council.py:739-741`).

---

**Conclusion: The council produces a degraded answer, not a hard failure.** The user sees `status="failed"` in the API response and `metadata.json`, but `summary.md` still contains a real (incomplete) synthesizer answer. The failure mode is **confusing**: the user gets both a failure flag and a partial answer. The synthesizer never knows *which* lane failed — it sees a text marker mixed into the research list with no structural distinction.

**No `retry_policy`, `max_retries`, or `error_handler`** is configured anywhere in `graph.py` — grep confirms zero hits for those terms. LangGraph's default is to propagate exceptions, but each node catches its own exceptions and returns tombstones instead.

---

**Recommended hardening change:** Make `error` and `_role_failed` additive reducers so all lane failures are preserved, not just the last one.

At `consultants/engine/graph.py:74-75`, change:
```python
error: Annotated[list[str], operator.add]       # was Optional[str]
_role_failed: Annotated[list[str], operator.add]  # was Optional[str]
```

Then update the tombstone returns in `council.py` (e.g., line 564: `"error": ["researcher failed: ..."]` instead of a scalar), and update the runner's detection at `runner.py:172-174` to check `if final_state.get("error")` (non-empty list). This ensures that when two lanes fail in a 3-lane fan-out, both errors appear in the final state and the synthesizer can see exactly how many lanes are missing, rather than getting a single overwritten error string that hides the extent of the degradation.

---

## Critic (round 3)

DECISION: ready

The trace is well-supported with path:line citations. One critical contradiction across rounds must be flagged for the synthesizer: Round 1 & 3 claim `error` persists (non-additive, only failed lane writes it → status="failed"), while Round 2 claims the succeeding lane's return dict *overwrites* `error` to None via LangGraph's default merge (→ status="completed"). This is the difference between "user sees failed + partial answer" vs "user sees completed + silently degraded answer." The resolution hinges on LangGraph's runtime merge semantics for Send() parallel lanes when one lane omits a key from its return dict — this is a framework-level question, not resolvable by reading more project code. The synthesizer should reason from LangGraph docs/source: a node that doesn't return a key typically doesn't update it (no-op), so Round 1/3's reading (error persists, status="failed") is likely correct, but the non-additive `error` field is still broken for multi-lane failures (only last-failing-lane's error survives). The recommended hardening (make `error`/`_role_failed` additive) addresses both problems.

---

## Synthesizer

The council produces a degraded answer that the user perceives as `status="failed"`, but a partial synthesizer answer still exists in the artifact body — creating a confusing failure mode where the user sees both a failure flag and real (incomplete) content.

**1. CouncilState additive reducers** (`consultants/engine/graph.py:64-75`)
- `research: Annotated[list, operator.add]` (:64) — the failed lane's tombstone `["(researcher lane failed: …)"]` is **concatenated** with surviving lanes' research. The failure string becomes indistinguishable from a real report in the merged list.
- `turns: Annotated[list, operator.add]` (:68), `research_rounds_used` (:69), `total_*_tokens` (:71-72) — all additive; the failed lane's values merge normally (inflating round counts by +1).
- `error: Optional[str]` (:74) and `_role_failed: Optional[str]` (:75) — **non-additive**, default dict-merge (last-write-wins). A lane that succeeds doesn't return `error` in its dict, so it **doesn't overwrite** the failed lane's `error` — that key persists. However, if *multiple* lanes fail, only the last-failing-lane's error survives; earlier ones are silently clobbered.

**2. `runner.py`'s `compiled.stream` loop** (`consultants/server/runner.py:136-161`)
- The researcher exception is caught **inside** `researcher_node` (`council.py:555`), so `run_loop`'s exception never reaches `compiled.stream`. The graph continues through remaining nodes normally; the `except Exception` at `runner.py:155` is only hit by graph-level crashes, not per-node tombstones.

**3. Synthesizer's input messages** (`consultants/engine/council.py:715-723`)
- `synthesizer_node` reads `state.get("research") or []` (:720) — the additive-merged list containing both real research and the tombstone string. `build_synthesizer_messages` (:253) renders every entry as `"RESEARCHER REPORT (round N): …"` with no structural check for failure markers. The synthesizer has **no signal** that one "report" is actually an error; it produces an answer based on partial evidence without acknowledging the gap.

**4. Storage artifact writer** (`consultants/engine/storage.py:117-120, 200-207`)
- After the stream, `runner.py:171-174` evaluates: `terminal_status = "failed" if node_error else "completed"` → `"failed"` because the failed lane's `error` key persisted.
- `write_consultation` is called with `ConsultationResult(status="failed", final_answer=<synthesizer text>)`. `render_summary` (:117-120) writes `final_answer` as the body with `status: "failed"` in YAML front-matter. No guard against non-None `final_answer` exists, and in practice the synthesizer always produces text.

**User-perceived failure mode:** The user sees `status="failed"` in the API/metadata, but `summary.md` still contains a real (incomplete) synthesizer answer. This is confusing — is it failed or not? Additionally, if two lanes fail in a 3-lane fan-out, only the last one's error survives due to the non-additive `error` field, hiding the true extent of degradation.

**Recommended hardening change** — Make `error` and `_role_failed` additive reducers at `consultants/engine/graph.py:74-75`:

```python
error: Annotated[list[str], operator.add]       # was Optional[str]
_role_failed: Annotated[list[str], operator.add]  # was Optional[str]
```

Then update the tombstone returns (`council.py:564`) to wrap values in lists (`"error": ["researcher failed: …"]` instead of a scalar), and update the runner's detection at `runner.py:172` to `if final_state.get("error")` (non-empty list). This ensures all parallel-lane failures accumulate rather than clobber each other, and the synthesizer can later be enhanced to count how many lanes failed vs. succeeded.

---
