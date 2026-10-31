# Consultation transcript — csl-2026-10-22-1928-3131

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. Verify if `council.researcher_node` (consultants/engine/council.py) contains a try-except block around `agent_loop.runner.run_loop` to catch exceptions and populate `CouncilState`'s `error` or `_role_failed` fields.
2. Determine if `CouncilState`'s additive reducers for `research` and `turns` (consultants/engine/graph.py:64, 68) allow the graph to proceed to the synthesizer when one lane in a `Send` fan-out fails, or if a single unhandled exception in a lane crashes the entire `compiled.stream` generator.
3. Analyze `consultants/server/runner.py`'s `compiled.stream` loop (lines 137-161) to see if an exception raised during a parallel researcher lane execution is caught by the `except Exception as e` block (line 155) and how it affects the `final_state` passed to the synthesizer.
4. Trace the data flow from a failed researcher lane (either via `error` in state or a missing result in `research` list) into the `synthesizer_node` (consultants/engine/council.py) to see what the synthesizer actually perceives as input.
5. Check `consultants/engine/storage.py`'s `write_consultation` (line 200) and `_write_failed_artifacts` (line 211) to confirm if the resulting `summary.md` reflects a "failed" status or a degraded answer when a researcher lane crashes.
6. Identify the exact line in `consultants/engine/council.py` or `consultants/server/runner.py` where a `try-except` wrap around the researcher's loop should be added to prevent total consultation failure.

---

## Researcher (round 1)

The failure path for an unhandled exception in a parallel researcher lane is as follows:

1.  **Researcher Node Catch**: `consultants/engine/council.py:547-555` contains a `try...except Exception as e` block around the `loop_runner` (which calls `agent_loop.runner.run_loop`). If an exception is raised, it is caught here.
2.  **Tombstone Return**: Instead of crashing the graph, the node returns a "tombstone" dictionary (`consultants/engine/council.py:563-573`) containing:
    *   `error`: The exception message.
    *   `_role_failed`: `"researcher"`.
    *   `research`: A list containing the error text `(researcher lane failed: {e})`.
    *   `turns`: A `RoleTurn` recording the failure.
3.  **Additive Reducers**: In `consultants/engine/graph.py:64` and `68`, the `research` and `turns` fields are `Annotated[list, operator.add]`. The tombstone's error text is concatenated with the successful reports from other lanes.
4.  **Synthesizer Input**: The synthesizer receives the merged `research` list via `build_synthesizer_messages` (`consultants/engine/council.py:245-266`). It sees the actual reports from surviving lanes AND the string `(researcher lane failed: ...)` for the failed lane.
5.  **Artifact Writing**:
    *   `consultants/server/runner.py:172-174` detects the `error` field in the final state.
    *   `terminal_status` is set to `"failed"` (`consultants/server/runner.py:174`).
    *   `storage.write_consultation` is called with `status="failed"` and the specific `error` message (`consultants/server/runner.py:189-190`).
    *   `consultants/engine/storage.py:119` writes the synthesizer's final answer (which may be degraded) to `summary.md`.

**Conclusion**: The council **still produces an answer**, but the consultation is marked as `status=failed`. The user perceives a **degraded answer** where the synthesizer acknowledges the failure of one lane (if it's honest) or simply lacks the data from that lane, while the metadata/transcript explicitly logs the crash.

**Recommended Hardening**:
The current "tombstone" approach prevents total failure but relies on the synthesizer to handle a "failed" report string. To improve robustness, provide a structured failure object in the reducer.
**Change**: In `consultants/engine/council.py:566`, change `"research": [err_text]` to a structured format or ensure the synthesizer prompt explicitly handles these markers. However, the most critical gap is that the `error` field in `CouncilState` is a simple `Optional[str]` (`consultants/engine/graph.py:74`), meaning if multiple lanes fail, only the last one's error is preserved.

**Recommendation**: Change `CouncilState.error` to an additive list to capture all lane failures.
**Path**: `consultants/engine/graph.py:74` $\rightarrow$ `error: Annotated[list[str], operator.add]` (and update corresponding node returns).

---

## Researcher (round 1)

The failure path of a researcher lane crash during the Send-API fan-out is as follows:

### 1. CouncilState's Additive Reducers (`consultants/engine/graph.py`)
When a parallel researcher lane is spawned via `Send` (`consultants/engine/graph.py:359`), it operates on a subset of the state. The `CouncilState` uses `operator.add` as the reducer for the `research` and `turns` channels (`consultants/engine/graph.py:64, 68`). 

If a researcher lane crashes (e.g., unhandled exception in `run_loop`), the `researcher_node` catches the exception (`consultants/engine/council.py:547`) and returns a **tombstone update** (`consultants/engine/council.py:563`). This update includes:
- `research`: A list containing the error text `"(researcher lane failed: {e})"` (`consultants/engine/council.py:566`).
- `turns`: A `RoleTurn` object containing the same error text (`consultants/engine/council.py:568`).
- `error` and `_role_failed`: Markers indicating a failure occurred (`consultants/engine/council.py:564, 565`).

Because of the `operator.add` reducer, this error string is concatenated into the global `research` list alongside any successful reports from other lanes.

### 2. Runner's Stream Loop (`consultants/server/runner.py`)
The runner iterates over the graph stream (`consultants/server/runner.py:137`). 
- If the crash happened **inside** `researcher_node`'s `try-except` block, the graph continues normally. The `final_state` (`consultants/server/runner.py:140`) will contain the merged results (successes + tombstones).
- If the exception were to escape `researcher_node` (unlikely given the wrap), it would be caught by the runner's top-level `try-except` (`consultants/server/runner.py:136`), which sets `state.status = "failed"` and calls `_write_failed_artifacts` (`consultants/server/runner.py:157-160`).

### 3. Synthesizer's Input (`consultants/engine/council.py`)
The synthesizer receives the merged `research` list in `build_synthesizer_messages` (`consultants/engine/council.py:245`). Specifically, it iterates through the `research_rounds` (which contains the merged fan-out results) and appends them to the prompt (`consultants/engine/council.py:253-254`).

**What the synthesizer actually sees:**
The synthesizer sees a list of reports where one or more entries are simply the string `"(researcher lane failed: <exception details>)"`.

### 4. Artifact Writer (`consultants/engine/storage.py`)
The runner determines the `terminal_status` based on whether `final_state.get("error")` is present (`consultants/server/runner.py:172-174`). Since the tombstone return from `researcher_node` sets `"error": f"researcher failed: {e}"` (`consultants/engine/council.py:564`), the `terminal_status` becomes `"failed"`.

This status is passed into `storage.ConsultationResult` and written to `summary.md` via the YAML front-matter (`consultants/engine/storage.py:108`).

### Conclusion: User Perception
The council **produces a degraded answer** rather than a total failure. 
- **Status:** The `summary.md` front-matter will show `status: failed` and include the `error` string.
- **Content:** The synthesizer will attempt to write a final answer based on the surviving researcher lanes. Since it sees the failure message as "evidence," it may explicitly mention that some research lanes failed or simply ignore the gap and produce a partial answer.
- **Perceived Failure Mode:** The user receives a coherent but potentially incomplete answer, accompanied by a "failed" status in the metadata.

### Recommended Hardening
To prevent the synthesizer from potentially hallucinating a "complete" answer when critical lanes failed, the `synthesizer_node` should be made aware of the `_role_failed` marker to adjust its tone or explicitly warn the user.

**Change:** Update `consultants/engine/council.py` to inject a warning into the synthesizer's prompt if `_role_failed` is present in the state.

**Exact Line:** In `consultants/engine/council.py:718`, modify `build_synthesizer_messages` (or the call site in `synthesizer_node`) to append a notice: `if state.get("_role_failed"): parts.append("\nWARNING: One or more research lanes failed. Your answer may be incomplete.")`.

---

## Critic (round 2)

DECISION: ready

The researcher's evidence is sufficient. The failure path is fully traced and verified:
1. **Exception Catch**: `consultants/engine/council.py:547-555` catches the exception and returns a tombstone.
2. **State Merge**: `consultants/engine/graph.py:64, 68` uses `operator.add` to merge the failure string into the research and turns lists.
3. **Synthesizer Input**: `consultants/engine/council.py:253-254` injects the failure string `(researcher lane failed: ...)` directly into the prompt as a researcher report.
4. **Runner/Storage**: `consultants/server/runner.py:172-174` detects the `error` key, sets `terminal_status = "failed"`, and passes this to `storage.ConsultationResult` (`runner.py:177-190`).

The conclusion that the user receives a coherent but degraded answer with a `status=failed` metadata marker is correct.

---

## Synthesizer

The council produces a coherent but degraded answer, while the overall consultation is marked as `status=failed`.

**Failure Path Trace**
1.  **Exception Catch**: An unhandled exception in `agent_loop.runner.run_loop` is caught by a `try...except` block in `consultants/engine/council.py:547-555 [no run_loop at this line; line is in researcher_node]`. This prevents the graph from crashing and returns a "tombstone" update (`consultants/engine/council.py:563-573`) containing the error message.
2.  **Additive Reducers**: The tombstone's error string `(researcher lane failed: {e})` is merged into the global `research` and `turns` lists via `operator.add` reducers in `consultants/engine/graph.py:64, 68`.
3.  **Stream Loop**: Because the exception was caught within the node, the `compiled.stream` loop in `consultants/server/runner.py:137` continues normally. The graph proceeds to the synthesizer node with the merged state.
4.  **Synthesizer Input**: In `build_synthesizer_messages`, the synthesizer iterates through the merged research results (`consultants/engine/council.py:253-254`) and receives the failure string `(researcher lane failed: ...)` as if it were a research report.
5.  **Artifact Writer**: The runner detects the `error` key in the final state (`consultants/server/runner.py:172-174`), setting `terminal_status = "failed"`. This status is passed to `storage.write_consultation` (`consultants/server/runner.py:189-190 [no write_consultation at this line; line is in run_council]`) and written into the `summary.md` YAML front-matter (`consultants/engine/storage.py:108 [no md at this line; line is in _yaml_front_matter]`).

**User Perception**
The user receives a coherent answer based on the successful researcher lanes. Depending on the synthesizer's behavior, the answer may be incomplete or explicitly mention that some research lanes failed. However, the metadata in the resulting artifact explicitly labels the session as `status: failed`.

**Recommended Hardening**
Change `CouncilState.error` from `Optional[str]` to an additive list to prevent subsequent lane failures from overwriting the first error message.
**Change**: `consultants/engine/graph.py:74` $\rightarrow$ `error: Annotated[list[str], operator.add]` (and update node returns to return lists).

---
