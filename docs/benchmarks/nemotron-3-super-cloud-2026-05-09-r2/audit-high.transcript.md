# Consultation transcript — csl-2026-05-09-0745-a436

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. Consult consultants/engine/graph.py:locate additive reducer functions (e.g., `add_lane_result`) and examine how they handle exceptions from `agent_loop.runner.run_loop` (look for try/except or error propagation around line ~112).  
2. Trace consultants/server/runner.py:find the `compiled.stream` loop that iterates over lane futures; identify where exceptions are caught and whether they break the stream or are logged (around line ~78).  
3. Inspect the synthesizer module (likely `consultants/engine/synthesizer.py`):see how it builds its input messages from the council state; verify what it receives when a lane raises an unhandled exception (check message assembly around line ~45).  
4. Review storage.py:locate the artifact writer (`write_artifact` or similar) and see if it writes partial results or only on successful completion (note any conditional checks around line ~63).  
5. Determine the final status flow:search for where `status=failed` is set in the graph or runner (e.g., in `graph.py` after reducer aggregation or in `runner.py` stream exit) to understand whether the consultation aborts or returns a degraded answer.

---

## Researcher (round 1)

- Researcher node catches `run_loop` exceptions and returns a tombstone error with `_role_failed="researcher"` and error text `(researcher lane failed: {e})` (consultants/engine/council.py:562)  
- CouncilState's additive reducers (`research`, `turns`, `research_rounds_used`) merge the error text and turn from the failed lane into the overall state (consultants/engine/graph.py:60-73)  
- Runner's `compiled.stream` loop updates `final_state`; after streaming, `node_error = final_state.get("error")` triggers `terminal_status = "failed"` (consultants/server/runner.py:172-174)  
- Synthesizer may still produce a `final_answer`; the `ConsultationResult` stores both `final_answer` and `error`, so the user sees a failed consultation but may receive a degraded answer missing insights from the failed lane (consultants/server/runner.py:186,199)  
- Hardening: include lane index in researcher lane error message to improve debugging (change consultants/engine/council.py:562 to `err_text = f"(researcher lane {state.get('lane_idx', '?')} failed: {e})"`)

---

## Researcher (round 1)

- Exception in `agent_loop.runner.run_loop` is caught by `researcher_node` (consultants/engine/council.py:556-573), which returns a tombstone update containing the error string in `research` and `turns`, and sets `error`/`_role_failed` fields (lines 563-564).  
- `CouncilState` declares `error` and `_role_failed` as optional keys without reducers (consultants/engine/graph.py:74-75), so parallel lane updates overwrite each other via last-write-wins merging; the final state may retain an error from any failed lane.  
- The runner’s stream loop drains `compiled.stream` into `final_state` (consultants/server/runner.py:135-161); if `final_state.get("error")` is truthy, `terminal_status` is set to `"failed"` (lines 172-174).  
- The synthesizer still runs (unless it also fails) and receives the `research` list that includes the error strings, potentially yielding a degraded answer that reflects the missing lane.  
- Artifacts are written unconditionally via `storage.write_consultation` (storage.py:200-207), so `summary.md` contains the synthesizer’s `final_answer` (possibly error-laden) and `transcript.md` logs the failed researcher turn.  
- Thus, the consultation surfaces as `status=failed` but may still yield a coherent (degraded) answer; the user perceives a failed consultation with incomplete or caveated output.  

**Hardening change**: In `consultants/engine/council.py`, remove the `error` and `_role_failed` fields from the `researcher_node` exception handler (lines 563-564) so a single lane failure does not mark the whole consultation as failed, allowing status to remain `"completed"` when other nodes succeed.

---

## Researcher (round 1)

- Researcher lane exceptions are caught in `researcher_node` and returned as tombstone state with `"error": f"researcher failed: {e}"` (consultants/engine/council.py:563-565)  
- `CouncilState.error` is defined as `Optional[str]` with no reducer annotation, causing parallel researcher lane errors to overwrite each other via LangGraph's default dict-merge reducer (consultants/engine/graph.py:74)  
- After graph streaming, `runner.py` sets `terminal_status = "failed"` if `final_state.get("error")` exists (consultants/server/runner.py:172-174)  
- `ConsultationResult.error` stores a single string, so only the last lane's error survives in artifacts (consultants/engine/storage.py:66)  
- Hardening: Change `error` to `Annotated[list[str], operator.add]` to accumulate all lane errors; update nodes to return `[err_text]` and runner to join list for final error (consultants/engine/graph.py:74)

---

## Critic (round 3)

DECISION: ready
The researcher’s three rounds trace the exception from `researcher_node` (council.py:563‑565) through the additive reducers (graph.py:60‑73) where the error text is merged into `research` and `turns`, note that `error` and `_role_failed` lack reducers and thus undergo last‑write‑wins (graph.py:74‑75). The runner’s `compiled.stream` loop collects the final state and sets `terminal_status = "failed"` when `final_state.get("error")` is truthy (runner.py:172‑174). The synthesizer still runs and receives the `research` list containing the error strings, so it can craft a degraded answer missing the failed lane’s insights (runner.py:186,199). Artifacts are written unconditionally via `storage.write_consultation` (storage.py:200‑207), preserving whatever `final_answer` the synthesizer produced. Hence the consultation surfaces as `status=failed` but may still return a coherent (degraded) answer to the user. No further concrete gaps are needed.

---

## Synthesizer

The consultation ends with `status=failed` but the synthesizer can still return a coherent (degraded) answer that lacks the insights from the failed researcher lane.  

The exception in `agent_loop.runner.run_loop` is caught by `researcher_node`, which returns a tombstone update containing the error string in `research` and `turns` and sets `error`/`_role_failed` (consultants/engine/council.py:563‑565). CouncilState’s additive reducers merge this error text and the turn into the overall state (consultants/engine/graph.py:60‑73), while the `error` and `_role_failed` fields lack reducers, so parallel lane errors overwrite each other via last‑write‑wins (consultants/engine/graph.py:74‑75). After the graph streams, the runner’s loop sets `terminal_status = "failed"` when `final_state.get("error")` is truthy (consultants/server/runner.py:172‑174). The synthesizer still runs and receives the `research` list that includes the error strings, allowing it to craft a final answer missing the failed lane’s insights (consultants/server/runner.py:186,199). Artifacts are written unconditionally via `storage.write_consultation`, preserving whatever `final_answer` the synthesizer produced (storage.py:200‑207). Thus the user sees a failed consultation but may obtain a degraded, incomplete answer.  

Hardening: add the lane index to the researcher lane error message to improve debugging (consultants/engine/council.py:562 → `err_text = f"(researcher lane {state.get('lane_idx', '?')} failed: {e})"`).

---
