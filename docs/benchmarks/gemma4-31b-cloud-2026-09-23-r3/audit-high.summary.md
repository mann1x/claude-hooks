---
session_id: csl-2026-09-23-2050-3d74
created: "2026-09-23T20:50:28"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "gemma4:31b-cloud"
  planner: "gemma4:31b-cloud"
  researcher: "gemma4:31b-cloud"
  synthesizer: "gemma4:31b-cloud"
topology: council
effort: high
duration_seconds: 20.59
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-09-23/tmp/claude-hooks-bench-bench-baseline-2026-05-07-n3hj
---

The council still produces an answer, but the overall session is flagged as `status="failed"`. The user perceives a potentially degraded answer (missing data from the failed lane) while the system logs the failure in the metadata and transcript.

**Failure Path Trace:**

*   **CouncilState's additive reducers:** In `consultants/engine/council.py:547-555`, the `researcher_node` wraps the agent loop in a `try...except` block. When an exception occurs, it emits a "tombstone" update: a dictionary containing an error message and a failure marker `"(researcher lane failed: ...)"` in the `research` list (`consultants/engine/council.py:563-573`). Because the `research` channel uses `operator.add` as a reducer (`consultants/engine/graph.py:64`), this error string is simply appended to the findings of any successful lanes.
*   **`compiled.stream` loop:** Since the exception is caught inside the node and converted into a state update, the `compiled.stream` loop in `consultants/server/runner.py:137-144` does not raise an exception. The stream drains normally, and the `final_state` (`consultants/server/runner.py:140`) accumulates both the successful reports and the tombstone.
*   **Synthesizer's input:** The synthesizer's input is constructed via `build_synthesizer_messages` (`consultants/engine/council.py:245-266`), which consumes the `research` list (`consultants/engine/council.py:220 [no research at this line; line is in build_researcher_messages]`). The synthesizer sees the tombstone string as just another piece of research evidence. It will attempt to synthesize an answer using the remaining available data.
*   **Storage artifact writer:** The `ConsultationResult` is constructed using the `final_state` (`consultants/server/runner.py:177-198`). Because the `researcher_node` set an `error` key in the state, the overall session status is marked as `"failed"` (`consultants/server/runner.py:189`). `storage.write_consultation` (`consultants/engine/storage.py:200`) then persists this "failed" status alongside the (possibly degraded) `final_answer` in `summary.md`.

**Recommended Hardening:**
Modify `build_synthesizer_messages` to explicitly label failed lanes in the prompt so the synthesizer can reason about the missing data rather than treating the error string as standard research.

**Change:** In `consultants/engine/council.py:253-254`, replace the report loop with:
```python
for i, r in enumerate(research_rounds, start=1):
    prefix = "RESEARCHER REPORT" if not r.startswith("(") else "FAILED RESEARCH LANE"
    parts.append(f"\n{prefix} (round {i}):\n{r.strip()}")
```
