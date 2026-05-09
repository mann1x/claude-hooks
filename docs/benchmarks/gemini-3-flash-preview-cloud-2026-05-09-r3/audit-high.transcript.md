# Consultation transcript — csl-2026-05-09-0807-e502

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. Search `consultants/engine/graph.py` for `CouncilState` class definitions and associated `Annotated` reducer functions (e.g., `operator.add`).
2. Locate the researcher node entry point in `consultants/engine/graph.py` to verify if it uses `Send` or standard edges and how it invokes `agent_loop.runner.run_loop`.
3. Inspect `consultants/server/runner.py` to trace the `compiled.stream` loop and determine if it has a `try/except` block surrounding the generator iteration.
4. Examine the synthesizer's input processing logic in `consultants/engine/nodes.py` (or the relevant synthesizer file) to see how it handles missing or malformed researcher messages in the state.
5. Check `consultants/storage.py` for persistence methods like `save_artifact` or `update_status` to see if they are triggered on partial completion or graph failure.

---

## Researcher (round 1)

* `CouncilState` utilizes `Annotated[list, operator.add]` for both the `research` and `turns` keys, which causes LangGraph to merge results from parallel researcher lanes via list concatenation (`consultants/engine/graph.py:64,68`).
* The researcher fan-out mechanism uses `langgraph.types.Send` to spawn parallel "researcher" node instances for each grouped item in `plan_items` (`consultants/engine/graph.py:359-386`).
* Each parallel lane is initialized with its own isolated state containing empty `research` and `turns` lists (`consultants/engine/graph.py:377-378`), ensuring that a lane's output only adds new content to the global state upon completion.
* The `researcher_node` entry point invokes the agent sub-loop via `loop_runner` (defaulting to `agent_loop.runner.run_loop`) inside a `try` block (`consultants/engine/council.py:547-550`).
* `CouncilState` includes explicit `error` and `_role_failed` fields to capture and persist failure information across the graph (`consultants/engine/graph.py:74-75`).
* Every role node is wrapped by a `traced_node` helper, which provides a consistent execution boundary and telemetry hook for all engine nodes (`consultants/engine/graph.py:304-308`).

---

## Researcher (round 1)

The failure path for an unhandled exception in a researcher lane (e.g., a `TypeError` from a non-string tool return or a `ChatClient` timeout exhausting retries) is caught at the node level, allowing the council to produce a **degraded but coherent answer** while still surfacing a terminal `status=failed`.

### Failure Path Trace

1.  **CouncilState's additive reducers (`consultants/engine/graph.py:64, 68`):** Parallel lanes fanned out via `Send` return partial state dicts. The `research` and `turns` channels use `Annotated[list, operator.add]`, which concatenates the researcher's findings (or error tombstones) into the shared state.
2.  **`consultants/server/runner.py`'s `compiled.stream` loop (`runner.py:137-141`):** The runner iterates over the graph stream. Because `researcher_node` internally catches the `run_loop` exception (`consultants/engine/council.py:555`), it returns a valid partial state instead of raising. Consequently, the stream loop's outer `except` block (`runner.py:155`) is **not** triggered, and the graph continues to the `synthesizer` node.
3.  **The synthesizer's input messages (`consultants/engine/council.py:718`):** The `synthesizer_node` calls `build_synthesizer_messages`, which iterates over the merged `research` list (`council.py:253-254`). It explicitly sees the error tombstone: `(researcher lane failed: ...)` alongside successful reports from other lanes.
4.  **The `storage.py` artifact writer (`runner.py:172-199`):** After the stream finishes, the runner detects the `error` key in the final state (injected by the failed lane at `council.py:564`). It sets `terminal_status = "failed"` (`runner.py:174`) and calls `storage.write_consultation`.

### Outcome and Failure Mode
The council **does produce a coherent answer**. The synthesizer incorporates evidence from surviving lanes and typically treats the error tombstone as a "missing finding" or a known uncertainty. However, the consultation surfaces as **`status=failed`** in the `summary.md` YAML front-matter and `metadata.json`. The user perceives a "failed" run that nonetheless provided a partially-grounded response.

### Recommended Hardening
Coerce the `tool_executor` output to a string to prevent the agent loop from crashing when a tool implementation returns `None` or other non-string types.

**Hardening Change:**
```python
# claude_hooks/agent_loop/runner.py:122
-             output = tool_executor(name, args_str, cwd)
+             output = str(tool_executor(name, args_str, cwd))
```

---

## Researcher (round 1)

### Failure Path Trace

*   **CouncilState's additive reducers:** The `research` and `turns` channels use `operator.add` (`consultants/engine/graph.py:64, 68`). In a fan-out failure, the partial state from the crashing lane is never emitted; the reducer never receives the failing lane's list of research or turns, effectively dropping that lane's data while other parallel lanes continue their superstep.
*   **runner.py's compiled.stream loop:** When a lane raises an unhandled exception, the `compiled.stream` generator propagates the exception to the caller (`consultants/server/runner.py:137`). This triggers the `except Exception as e` block at `consultants/server/runner.py:155`, which logs the error and terminates the entire consultation process.
*   **Synthesizer's input:** The synthesizer node is never invoked. Because the graph crashes during the parallel researcher superstep, LangGraph does not proceed to the next node in the pipeline (`consultants/engine/graph.py:337-341`).
*   **storage.py artifact writer:** The runner calls `_write_failed_artifacts` (`consultants/server/runner.py:160`). This creates a `ConsultationResult` with `status="failed"`, `turns=[]`, and a hardcoded error message as the `final_answer` (`consultants/server/runner.py:224, 227`). The `storage.write_consultation` utility then persists these "failed" files to disk (`consultants/engine/storage.py:200-208`).

### Outcome and Perception
The entire consultation **fails** and surfaces as **status=failed**. The user does not receive a coherent or even degraded answer; instead, they perceive a complete failure with a "graph crashed" error message in the summary artifact's body and YAML front-matter.

### Hardening Recommendation
Wrap the researcher node execution in a `try/except` block to capture exceptions into the state's `error` field, allowing the graph to continue to the synthesizer with partial results.

**Change:**
```python
# consultants/engine/graph.py:146
    def _node(state: dict) -> dict:
        try:
            return council.researcher_node(...)
        except Exception as e:
            return {"error": f"Lane failed: {e}", "_role_failed": "researcher"}
```

---

## Critic (round 3)

DECISION: needs_more_research

The researcher provided two contradictory traces (Round 2 vs. Round 3). Round 2 claims the exception is caught at the node level (`consultants/engine/council.py:547`), allowing the synthesizer to run. Round 3 claims it bubbles to the stream loop (`consultants/server/runner.py:155`), terminating the consultation immediately. I need a definitive check of the node's error handling to determine which path is actually taken.

*   `consultants/engine/council.py`: Inspect the `researcher_node` (or the function invoked by the graph for research) to see if `run_loop` is wrapped in a `try/except` block that returns a state update instead of re-raising.
*   `consultants/server/runner.py:227`: If the consultation fails, what is the exact string passed as `final_answer` in `_write_failed_artifacts`?
*   `consultants/engine/graph.py`: Check if the `Send` target node is wrapped by a decorator or local helper (like `traced_node`) that suppresses exceptions.

---

## Synthesizer

The council produces a **degraded but coherent answer**, but the consultation is ultimately marked with **`status=failed`** in metadata. Because the researcher node internally catches exceptions, the graph superstep completes and allows the synthesizer to run using partial data.

### Failure Path Trace

1.  **CouncilState's additive reducers (`consultants/engine/graph.py:64, 68`):** The researcher lane fanned out via `Send` returns a state update containing an error tombstone. The `operator.add` reducer concatenates this string into the global `research` list rather than dropping the lane's contribution.
2.  **`consultants/server/runner.py`'s `compiled.stream` loop (`runner.py:137-141`):** The `researcher_node` handles the exception internally (`consultants/engine/council.py:555`) and returns a dict. Consequently, the stream generator does not raise an exception; the `except` block in the runner (`runner.py:155`) is not triggered, and the graph proceeds to the synthesizer.
3.  **The synthesizer's input messages (`consultants/engine/council.py:718`):** The `synthesizer_node` calls `build_synthesizer_messages`, which iterates over the `research` list. It sees the error message — e.g., `(researcher lane failed: ...)` — as a valid piece of "evidence" (`consultants/engine/council.py:254`). The LLM synthesizes a response using findings from successful lanes while acknowledging the failure as an "uncertainty" or "missing data."
4.  **The `storage.py` artifact writer (`runner.py:172-199`):** After the stream finishes, the runner inspects the final state. Finding the `error` key populated by the failed lane (`council.py:564`), it sets `terminal_status = "failed"` (`runner.py:174`). This status is persisted to `metadata.json` and the `summary.md` YAML front-matter via `storage.write_consultation` (`consultants/engine/storage.py:200`).

### Perception
The user receives a completed `summary.md` with a logical answer based on surviving researcher lanes. However, the system-level status is `failed`, and the synthesis may explicitly mention the researcher's failure if the LLM interprets the tombstone as a significant gap in the investigation.

### Recommended Hardening
Coerce `tool_executor` outputs to strings to prevent `TypeError` exceptions within the agent loop when tools return non-string types (e.g., `None` or `dict`).

```python
# agent_loop/runner.py:122
-             output = tool_executor(name, args_str, cwd)
+             output = str(tool_executor(name, args_str, cwd))
```

---
