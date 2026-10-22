---
session_id: csl-2026-10-22-2115-f841
created: "2026-10-22T21:15:43"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "minimax-m3.1-flash:cloud"
  planner: "minimax-m3.1-flash:cloud"
  researcher: "minimax-m3.1-flash:cloud"
  synthesizer: "minimax-m3.1-flash:cloud"
topology: council
effort: high
duration_seconds: 488.32
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-Rb1F
---

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
