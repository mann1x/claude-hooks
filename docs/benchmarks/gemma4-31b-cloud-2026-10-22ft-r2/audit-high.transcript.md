# Consultation transcript — csl-2026-10-22-2004-94c5

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. Examine `claude_hooks/agent_loop/runner.py:run_loop` to determine if exceptions (HTTP timeouts, type errors) are caught internally or propagate to the caller.
2. Locate the fan-out loop in `consultants/server/runner.py` (specifically searching for `compiled.stream`) to see how it handles exceptions from parallel lanes (e.g., `asyncio.gather` configuration).
3. Analyze `consultants/engine/graph.py` to find the additive reducers for `CouncilState` and verify if they handle missing values or exception objects when aggregating researcher results.
4. Trace the hand-off from the fan-out path to the synthesizer in `consultants/engine/council.py` or `consultants/server/runner.py` to see what messages are sent to the synthesizer when a lane fails.
5. Inspect `consultants/engine/storage.py` to determine if a lane failure prevents artifact writing or if the writer logs a partial failure.
6. Evaluate the final return path in `consultants/server/runner.py` to see if any lane failure triggers a `status=failed` response or allows a degraded success response.

---

## Researcher (round 1)

The failure path of an unhandled exception in a parallel researcher lane is as follows:

### 1. Exception Propagation in `agent_loop`
In `claude_hooks/agent_loop/runner.py`, the `run_loop` function has **no top-level try-except block** around the `chat_fn(payload)` call (line 230) or the `execute_tool_calls(...)` call (line 300). If `chat_fn` raises an HTTP timeout or `tool_executor` returns a non-string (which would cause a `TypeError` when `len(output)` is called at `claude_hooks/agent_loop/runner.py:126`), the exception propagates immediately to the caller.

### 2. Failure in `consultants/server/runner.py`
The `run_council` function invokes the compiled LangGraph via `compiled.stream` (line 137). 
- Because the researcher lanes are spawned via `Send` (a parallel fan-out mechanism in LangGraph), an unhandled exception in any one of these asynchronous lanes causes the entire `compiled.stream` generator to raise that exception.
- This exception is caught by the broad `except Exception as e` block at `consultants/server/runner.py:155`.
- The recovery path is:
    - `state.status` is set to `"failed"` (line 157).
    - `state.error` is set to `"graph crashed: <exception>"` (line 158).
    - `_write_failed_artifacts` is called (line 160).

### 3. Synthesizer Input & Final Answer
The synthesizer **never sees any messages**. Because the exception occurs during the researcher phase, the graph execution is aborted before the synthesizer node is ever entered. 
- The `final_state` (captured from `mode == "values"`) will only contain the state up until the crash.
- The synthesizer's contribution is skipped entirely.

### 4. Artifact Writing via `storage.py`
The `_write_failed_artifacts` helper (line 211) creates a `storage.ConsultationResult` where:
- `final_answer` is hardcoded to `f"(consultation failed: {exc})"` (line 224).
- `turns` is an empty list `[]` (line 225).
- `status` is `"failed"` (line 227).
These are then written to disk via `storage.write_consultation` (`consultants/engine/storage.py:200`), resulting in a `summary.md` that contains only the failure notice.

### Conclusion
The council **fails entirely** and surfaces as `status=failed`. It does not produce a degraded answer; it produces a failure artifact. The user perceives a total crash of the consultation.

### Recommended Hardening
Wrap the `run_loop` logic in a try-except block to capture role-level failures into the state instead of crashing the graph.

**Change**: In `claude_hooks/agent_loop/runner.py`, wrap the main `for` loop (lines 215-303) in a try-except block that returns a response dict containing an `error` key, allowing the `consultants/engine/graph.py` additive reducers to handle the failure gracefully without aborting the entire stream.

**Path:Line**: `claude_hooks/agent_loop/runner.py:215`

---

## Researcher (round 1)

The failure path for an unhandled exception in a parallel researcher lane (e.g., an HTTP timeout in `agent_loop.runner.run_loop`) is as follows:

### 1. CouncilState's Additive Reducers (`consultants/engine/graph.py`)
When a researcher lane is fanned out via LangGraph's `Send` API, the state is partially updated by each lane.
- The `researcher_node` in `consultants/engine/council.py:547-573` wraps the `loop_runner` (which is `run_loop`) in a `try...except Exception` block.
- If an exception occurs, it returns a "tombstone" update:
  - `error`: `f"researcher failed: {e}"` (`consultants/engine/council.py:564`)
  - `_role_failed`: `"researcher"` (`consultants/engine/council.py:565`)
  - `research`: `[f"(researcher lane failed: {e})"]` (`consultants/engine/council.py:566`)
  - `turns`: A `RoleTurn` object containing the error text (`consultants/engine/council.py:568-572`).
- Because `CouncilState` defines `research` and `turns` as `Annotated[list, operator.add]` (`consultants/engine/graph.py:64, 68`), these tombstone values are **appended** to the global state. The exception is swallowed by the node, allowing other parallel lanes to continue.

### 2. `consultants/server/runner.py`'s compiled.stream loop
- The runner iterates through `compiled.stream` (`consultants/server/runner.py:137`).
- The `values` stream mode provides the fully merged state after each superstep (`consultants/server/runner.py:139-141`).
- The runner does not check for `_role_failed` or `error` inside the stream loop; it only catches exceptions that crash the *entire* graph (`consultants/server/runner.py:155-161`). Since the researcher node caught the exception, the stream loop completes normally.

### 3. Synthesizer's Input Messages
- The `synthesizer_node` calls `build_synthesizer_messages` (`consultants/engine/council.py:718`).
- This function iterates over `state.get("research") or []` (`consultants/engine/council.py:253-254`).
- The synthesizer sees the tombstone text: `"(researcher lane failed: ...)"` as one of the research reports. It is then the synthesizer's job to decide if the remaining surviving lanes provide enough evidence to answer.

### 4. `storage.py` Artifact Writer
- After the stream finishes, the runner determines the `terminal_status` based on `final_state.get("error")` (`consultants/server/runner.py:172-174`).
- Since the researcher node set the `error` key in the state (`consultants/engine/council.py:564`), `terminal_status` becomes `"failed"` (`consultants/server/runner.py:174`).
- `storage.write_consultation` is called with `status="failed"` and the error message (`consultants/server/runner.py:189-190, 199`).

### Conclusion
The council **produces a degraded answer** rather than a total failure.
- **User Perception**: The user receives a `status=failed` response in the API/UI, but the `final_answer` field will contain the synthesizer's best attempt based on the surviving lanes.
- **Failure Mode**: The answer is "confidently degraded"—the synthesizer might hallucinate a conclusion or omit critical facts that the failed lane was supposed to find, but it will be aware (via the input messages) that some research failed.

### Recommended Hardening
The current implementation allows the synthesizer to proceed even if critical research lanes fail. The synthesizer should be explicitly prompted to warn the user about the failure.

**Change**: Modify `build_synthesizer_messages` in `consultants/engine/council.py` to check for failures in the research reports and inject a warning.

**Path**: `consultants/engine/council.py`
**Change**: Around line 254, add a check:
```python
    # Suggested hardening:
    failures = [r for r in research_rounds if r.startswith("(researcher lane failed:")]
    if failures:
        parts.append(f"\nWARNING: {len(failures)} research lane(s) failed. Do not assume the missing information is absent from the codebase; it may simply have failed to be retrieved.")
```

---

## Critic (round 2)

DECISION: ready

The researchers provided contradictory accounts; I have verified the code and confirmed Researcher 2 is correct. The `researcher_node` explicitly catches exceptions from the researcher loop, preventing a graph crash and allowing the synthesizer to run.

1.  **Reducers**: `CouncilState` uses `operator.add` for `research` and `turns` (`consultants/engine/graph.py:64`). When a lane fails, `researcher_node` returns a "tombstone" list containing the error text (`consultants/engine/council.py:566`), which is appended to the global state.
2.  **Runner Loop**: The `compiled.stream` loop in `consultants/server/runner.py:137` completes normally because the exception is swallowed by the node. The `except Exception` block at `runner.py:155` is NOT triggered.
3.  **Synthesizer Input**: The synthesizer receives the tombstone text (e.g., `"(researcher lane failed: ...)"`) as one of the research reports via `build_synthesizer_messages` (`consultants/engine/council.py:253-254`).
4.  **Artifact Writer**: `consultants/server/runner.py:172-174` detects the `"error"` key in the final state and sets `terminal_status = "failed"`. The `storage.ConsultationResult` is written with `status="failed"` but contains the synthesizer's `final_answer` (`consultants/server/runner.py:186, 189`).

**Conclusion**: The council produces a **confidently degraded answer**. The user perceives a `status=failed` result, but the `final_answer` field will contain the synthesizer's best attempt based on surviving lanes. The failure mode is a "partial success" masked as a "failure" in the status field.

**Recommended Hardening**:
Modify `build_synthesizer_messages` in `consultants/engine/council.py` to detect tombstone strings in `research_rounds` and inject an explicit warning to the synthesizer to prevent hallucinations regarding missing data.
**Path:Line**: `consultants/engine/council.py:254` (insert warning block after the research loop).

---

## Synthesizer

The council produces a confidently degraded answer; the user perceives a `status=failed` result, but the `final_answer` field contains the synthesizer's best attempt based on the surviving researcher lanes.

**Failure Path Trace**

1.  **CouncilState Reducers**: The `researcher_node` catches the exception in a `try...except` block (`consultants/engine/council.py:547-573`). It returns a "tombstone" update containing the error text `"(researcher lane failed: {e})"` in the `research` list (`consultants/engine/council.py:566`). Because `CouncilState` uses `operator.add` for the `research` field (`consultants/engine/graph.py:64`), this error string is appended to the global state without crashing the graph.
2.  **Runner Loop**: The `compiled.stream` loop in `consultants/server/runner.py:137` completes normally because the exception was swallowed by the node. The broad `except Exception` block at `consultants/server/runner.py:155` is not triggered.
3.  **Synthesizer Input**: The `build_synthesizer_messages` function iterates over the `research` state (`consultants/engine/council.py:253-254 [no research at this line; line is in build_synthesizer_messages]`). The synthesizer receives the tombstone error string as one of its research reports, allowing it to attempt a synthesis using only the successful lanes.
4.  **Artifact Writer**: The runner checks `final_state.get("error")` (`consultants/server/runner.py:172-174`). Since the `researcher_node` set the `error` key upon failure (`consultants/engine/council.py:564`), the `terminal_status` is set to `"failed"` (`consultants/server/runner.py:174`). The final result is written to storage with `status="failed"` but includes the synthesizer's `final_answer` (`consultants/server/runner.py:186, 189`).

**Hardening Recommendation**

Modify `build_synthesizer_messages` to explicitly warn the synthesizer when tombstone strings are detected in the research reports, preventing hallucinations about missing information.

**Path:Line**: `consultants/engine/council.py:254` (Insert a check for strings starting with `"(researcher lane failed:"` and append a warning to the synthesizer's prompt).

---
