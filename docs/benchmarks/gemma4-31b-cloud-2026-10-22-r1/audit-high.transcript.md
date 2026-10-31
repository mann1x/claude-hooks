# Consultation transcript — csl-2026-10-22-1237-134a

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

I have identified the core architectural components required to trace the failure path but have not yet executed the deep-dive analysis.

**Findings & Evidence:**
- **CouncilState/Graph:** Verified existence of `consultants/engine/graph.py`. This is the correct location to analyze additive reducers.
- **Server Runner:** Verified existence of `consultants/server/runner.py`. This is the correct location to analyze the `compiled.stream` loop.
- **Agent Loop:** Verified existence of `claude_hooks/agent_loop/runner.py` containing `run_loop`. This is the origin of the hypothetical exception.
- **Storage:** A `glob` for `storage.py` returned no results in the top-level or immediate subdirectories, meaning the artifact writer is likely located in a path not yet explicitly listed (e.g., `consultants/engine/storage.py` or a different naming convention).

**What was verified and how:**
- Used `survey_project` to map the overall directory structure.
- Used `list_files` on `consultants/` and `consultants/server/` to confirm the presence of the runner and engine files.
- Used `glob` to search for `storage.py` (which failed).

**What is open/unfinished:**
1. **Failure Trace (Reducers):** I have not yet read `consultants/engine/graph.py` to see if an exception in a parallel lane is caught and reduced to an error state or if it propagates and kills the graph execution.
2. **Failure Trace (Stream Loop):** I have not yet read `consultants/server/runner.py` to determine if `compiled.stream` handles partial failures or crashes the request.
3. **Synthesizer Input:** I have not identified the exact code path where the synthesizer gathers messages from the graph output to see if "failed" lanes are filtered out or passed as error strings.
4. **Artifact Writing:** I have not located the `storage.py` equivalent to determine if partial results are persisted.
5. **Final Conclusion:** The determination of "coherent degraded answer" vs "status=failed" and the specific hardening recommendation are pending the above reads.

---

## Researcher (round 1)

The failure path for an unhandled exception in a parallel researcher lane is a **total consultation failure** that surfaces as `status=failed` to the user.

### Failure Trace

1.  **Origin (`claude_hooks/agent_loop/runner.py`):**
    An unhandled exception (e.g., HTTP timeout or type error in `tool_executor`) occurs during the loop in `run_loop` (`claude_hooks/agent_loop/runner.py:230`). Since there is no `try/except` block surrounding the `chat_fn` or `execute_tool_calls` within the `for i in range(config.max_iterations):` loop, the exception propagates immediately up the call stack.

2.  **CouncilState & Reducers (`consultants/engine/graph.py`):**
    Because the exception is raised *during* the execution of the `researcher` node, the node never returns a state update dict. Consequently, the additive reducers for `research` (`consultants/engine/graph.py:64`), `turns` (`consultants/engine/graph.py:68`), and token counts (`consultants/engine/graph.py:71-72`) are **never invoked** for that lane. No partial results from the failing lane are merged into the global state.

3.  **Server Runner Loop (`consultants/server/runner.py`):**
    The exception propagates through the LangGraph `compiled.stream` iterator (`consultants/server/runner.py:137`). It is caught by the broad `except Exception as e:` block at `consultants/server/runner.py:155`.
    *   The runner immediately sets `state.status = "failed"` and `state.error = f"graph crashed: {e}"` (`consultants/server/runner.py:157-158`).
    *   The loop terminates instantly; subsequent nodes (Critic, Synthesizer) are **not executed**.

4.  **Synthesizer's Input:**
    The synthesizer **never runs**. There are no "input messages" for it because the graph execution was aborted at the researcher stage.

5.  **Artifact Writer (`consultants/engine/storage.py`):**
    The runner calls `_write_failed_artifacts` (`consultants/server/runner.py:160`), which constructs a `storage.ConsultationResult` with:
    *   `final_answer = f"(consultation failed: {exc})"` (`consultants/server/runner.py:224`).
    *   `status = "failed"` (`consultants/server/runner.py:227`).
    *   `turns = []` (`consultants/server/runner.py:225`).
    This result is written to disk via `storage.write_consultation` (`consultants/server/runner.py:235`), which triggers `_atomic_write_text` for `summary.md`, `transcript.md`, and `metadata.json` (`consultants/engine/storage.py:205-207`).

### Conclusion
The council does **not** produce a coherent answer. The entire consultation fails. The user perceives a `status=failed` response and a `summary.md` containing only the exception message.

### Recommended Hardening
Wrap the `run_loop` execution in a `try/except` block and return a structured error state instead of raising, allowing the LangGraph reducer to capture the failure as a "failed lane" rather than a "crashed graph."

**Change:** In `claude_hooks/agent_loop/runner.py`, wrap the core loop (lines 215–303) in a `try/except` block that returns a dict containing `{"error": str(e), "_role_failed": "researcher"}`. This ensures the graph continues to the synthesizer, which can then report which specific research lanes failed.

---

## Researcher (round 1)

The failure trace for an unhandled exception in a parallel researcher lane is as follows:

### 1. CouncilState's Additive Reducers (`consultants/engine/graph.py`)
When a researcher lane is spawned via the `Send` API (`consultants/engine/graph.py:359`), its output is merged into the global `CouncilState` using additive reducers. Specifically, the `research` and `turns` channels use `operator.add` (`consultants/engine/graph.py:64, 68`). If a lane crashes, it must return a "tombstone" value to ensure the failure is recorded in these additive lists; otherwise, the lane's contribution is simply missing from the final merge.

### 2. Researcher Node Failure Handling (`consultants/engine/council.py`)
The `researcher_node` explicitly wraps the `loop_runner` (which calls `agent_loop.runner.run_loop`) in a `try...except` block (`consultants/engine/council.py:547`). 
- If an exception occurs (e.g., HTTP timeout or tool executor error), the node catches it (`consultants/engine/council.py:555`).
- It returns a **tombstone state update**:
  - `error`: `"researcher failed: {e}"` (`consultants/engine/council.py:564`)
  - `_role_failed`: `"researcher"` (`consultants/engine/council.py:565`)
  - `research`: `["(researcher lane failed: {e})"]` (`consultants/engine/council.py:566`)
  - `turns`: A `RoleTurn` object containing the error text (`consultants/engine/council.py:568`).
- This prevents the exception from propagating to the graph runner and ensures the failure is visible to the synthesizer.

### 3. Stream Loop and Synthesizer Input (`consultants/server/runner.py` & `consultants/engine/council.py`)
- **Stream Loop**: The `compiled.stream` loop in `consultants/server/runner.py:137` continues executing other lanes and nodes because the exception was caught inside `researcher_node`.
- **Synthesizer Input**: The `synthesizer_node` constructs its prompt using `state.get("research")` (`consultants/engine/council.py:720`). Because of the tombstone return, the synthesizer sees a list of reports where one or more entries are `"(researcher lane failed: ...)"`.
- **Synthesizer Behavior**: The synthesizer is instructed to "Cite `path:line` for any code-dependent claim" (`consultants/engine/council.py:160`). It will see the failure notice and likely produce a **degraded but coherent answer** based on the remaining successful lanes, potentially noting the missing information.

### 4. Artifact Writing (`consultants/server/runner.py` & `consultants/engine/storage.py`)
- **Status Determination**: In `consultants/server/runner.py:172-174`, the runner checks `final_state.get("error")`. Since the tombstone set `"error"`, `terminal_status` becomes `"failed"`.
- **Persistence**: `storage.write_consultation` is called (`consultants/server/runner.py:199`), writing the result to `consultants/engine/storage.py:200`.
- **User Perception**: The `summary.md` will contain the synthesizer's degraded answer, but the `metadata.json` (`consultants/engine/storage.py:166`) and the YAML front-matter in `summary.md` (`consultants/engine/storage.py:108`) will explicitly show `status: "failed"` and the error message.

### Conclusion
The council produces a **degraded answer** rather than a total system crash. The user perceives a coherent response that may be missing specific details, but the consultation is marked as `status=failed` in the metadata.

**Recommended Hardening Change**:
The current tombstone for `researcher_node` sets the global `error` key, which marks the entire consultation as `failed` even if only 1 of 3 lanes crashed. Change the tombstone to use a lane-specific error key or only set `_role_failed` without triggering the global `error` if other lanes succeeded.

**Recommended Fix**: In `consultants/engine/council.py:564`, remove the `"error": f"researcher failed: {e}"` entry from the return dict to avoid marking the entire session as failed when partial research is still available.

---

## Critic (round 2)

DECISION: ready

The researcher's evidence is sufficient. Report 1 claimed a total system crash, but Report 2 correctly identified the `try...except` block in `consultants/engine/council.py:547-555`. I have verified this block exists and returns a "tombstone" state update. This ensures that the exception is caught *inside* the researcher node, allowing the LangGraph stream in `consultants/server/runner.py:137` to continue to the synthesizer. The synthesizer receives the failure notice via the `research` additive reducer (`consultants/engine/graph.py:64`), resulting in a degraded but coherent answer. However, because the tombstone sets the global `error` key (`consultants/engine/council.py:564`), the runner identifies this as a failure (`consultants/server/runner.py:172`), marking the session as `status=failed` in the artifacts.

---

## Synthesizer

The council produces a degraded but coherent answer, but the overall consultation is marked as `status=failed`.

*   **CouncilState's additive reducers:** When a researcher lane fails, the exception is caught in `consultants/engine/council.py:555`, and the node returns a "tombstone" update. The failure message `"(researcher lane failed: {e})"` is merged into the global `research` list via the `operator.add` reducer defined in `consultants/engine/graph.py:64`.
*   **`consultants/server/runner.py` stream loop:** The `compiled.stream` loop at `consultants/server/runner.py:137` does not crash because the exception was handled within the `researcher_node` (`consultants/engine/council.py:547`). Execution proceeds normally to the Critic and Synthesizer nodes.
*   **Synthesizer's input:** The synthesizer gathers all research reports via `state.get("research")` (`consultants/engine/council.py:720`). It sees the successful reports from other lanes alongside the failure string from the crashed lane, allowing it to synthesize a partial answer based on the available data.
*   **The storage.py artifact writer:** Because the tombstone update sets the global `error` key (`consultants/engine/council.py:564`), the runner detects this at `consultants/server/runner.py:172-174` and sets the final status to `"failed"`. This is persisted to `metadata.json` (`consultants/engine/storage.py:166`) and the `summary.md` front-matter (`consultants/engine/storage.py:108 [no md at this line; line is in _yaml_front_matter]`).

**Hardening Recommendation:**
Remove the global `"error": f"researcher failed: {e}"` entry from the return dictionary in `consultants/engine/council.py:564` to prevent a single lane failure from marking the entire consultation as `status=failed` when partial research is still available.

---
