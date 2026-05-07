---
session_id: csl-2026-05-07-1008-4409
created: "2026-05-07T10:08:45"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "minimax-m2.7:cloud"
  planner: "minimax-m2.7:cloud"
  researcher: "minimax-m2.7:cloud"
  synthesizer: "minimax-m2.7:cloud"
topology: council
effort: high
duration_seconds: 752.45
status: completed
cwd: /tmp/claude-hooks-bench-bench-baseline-2026-05-07-NhRc
---

## Synthesizer's Answer

The specific files (`consultants/engine/graph.py`, `consultants/server/runner.py`, `consultants/engine/council.py`, `consultants/engine/storage.py`) **do not exist at those paths** in this repository. The codebase uses `engine/graph.py`, `server/loop.py`, `server/storage.py` under a flat `engine/` and `server/` namespace. Without those files confirmed present, every `path:line` citation in the three researcher reports is unverified. The contradiction between the reports cannot be resolved without the actual source.

---

**If we assume the researcher reports are accurate to the actual code intent**, the following is the most defensible trace:

The consultant's Send-API fan-out is a LangGraph `Send` fan-out to parallel `researcher` nodes. When one lane raises an unhandled exception inside `run_loop` (e.g., HTTP timeout exhausting retry budget, `tool_executor` returning non-string):

1. **The exception is caught at `researcher_node`** (researcher report Round 1/3: `consultants/engine/council.py:547`). A tombstone dict is returned — `{"error": "...", "research": ["(researcher lane failed: ...)"]}` — instead of propagating to LangGraph.

2. **Additive reducers fire** (`consultants/engine/graph.py:64-72`). `research: Annotated[list, operator.add]` concatenates tombstone text with surviving lanes' `research` strings. `self._state.replace()` at `runner.py:140` sees the merged state.

3. **Stream loop drains** (`consultants/server/runner.py:136-174`). The `except` block at line 155 does **not** fire (exception was caught in the node). After drain, `node_error = final_state.get("error")` at line 172 is truthy, so `terminal_status = "failed"` at line 174.

4. **Synthesizer fires** (`consultants/engine/council.py:245-266`). It receives `research_rounds` containing all surviving lane outputs **plus** the tombstone string. It produces a degraded-but-coherent answer.

5. **Artifact writer fires** (`consultants/engine/storage.py:200-207`). Writes `summary.md` (degraded final answer + `status: failed` in front-matter), `transcript.md` (all turns including stub), `metadata.json` (`status: "failed"`, `error` field set).

**Bottom line:** The council produces a **degraded but coherent answer**, not a hard crash. The user sees `status=failed` in the poll response and a final answer that is narrower than expected (surviving lanes only + error-string tombstone in the research reports). The `status=failed` comes from `runner.py:174` via `node_error` check — not from the except block at line 155.

---

**ONE hardening change** — add tombstone detection to the synthesizer prompt so it does not silently hallucinate over the failed lane's gap:

**`consultants/engine/council.py`** (after the research loop around line 253, inside `build_synthesizer_messages`):

```python
# After building RESEARCHER REPORT parts:
if any("(researcher lane failed" in r for r in research_rounds):
    parts.append(
        "\nWARNING: One researcher lane failed. The answer below may be "
        "incomplete. Cite only confirmed findings; do not speculate."
    )
```

This ensures the synthesizer flags its own degraded quality rather than producing a confidently-wrong answer that erases the failure from the user's view. The tombstone mechanism already catches the exception internally (making this a second-layer guard); this change surfaces the degradation to the answer quality rather than hiding it.
