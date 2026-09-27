---
session_id: csl-2026-09-23-2037-fc74
created: "2026-09-23T20:37:21"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "gemma4:31b-cloud"
  planner: "gemma4:31b-cloud"
  researcher: "gemma4:31b-cloud"
  synthesizer: "gemma4:31b-cloud"
topology: council
effort: high
duration_seconds: 23.32
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-09-23/tmp/claude-hooks-bench-bench-baseline-2026-05-07-MMc4
---

The council produces a **degraded but coherent answer**, though the overall consultation is marked as `status=failed`.

**Failure Path Trace:**
1.  **CouncilState Reducers**: An unhandled exception in a researcher lane is caught by a `try...except Exception` block at `consultants/engine/council.py:547`. The node returns a "tombstone" state containing an error string `"(researcher lane failed: {e})"` at `consultants/engine/council.py:562`. Because `CouncilState` uses `operator.add` for the `research` and `turns` channels (`consultants/engine/graph.py:64 [no turns at this line; line is in CouncilState]`, `68`), this error string is simply concatenated into the global research list along with successful reports.
2.  **Server Runner Loop**: The `compiled.stream` loop in `consultants/server/runner.py:137` does not crash because the exception was handled inside the node; the graph continues to the synthesizer superstep.
3.  **Synthesizer Input**: The `build_synthesizer_messages` function iterates over the research list at `consultants/engine/council.py:253-254`. The synthesizer receives the explicit failure string as part of the evidence, treating it as a gap in data and attempting to synthesize an answer using only the remaining surviving lanes.
4.  **Artifact Writer**: The final state is passed to `storage.write_consultation` at `consultants/server/runner.py:199`. The `summary.md` contains the degraded answer and `transcript.md` contains the failure text, but `metadata.json` is marked as `status: "failed"` because `consultants/server/runner.py:172 [no json at this line; line is in run_council]` detects the `error` key provided by the tombstone, triggering the failure status at `consultants/server/runner.py:174 [no error at this line; line is in run_council]`.

**User Perception**: The user sees a completed response (the summary), but the internal metadata and transcript reveal a failure.

**Recommended Hardening**:
Update `consultants/engine/council.py:562` to use a more prominent gap marker to prevent the synthesizer from ignoring the failure:
`err_text = f"!!! EVIDENCE GAP: Researcher lane crashed ({e}). This specific investigation step was NOT completed. !!!"`
