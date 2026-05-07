---
session_id: csl-2026-05-07-1025-f8da
created: "2026-05-07T10:25:26"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "qwen3.5:397b-cloud"
  planner: "qwen3.5:397b-cloud"
  researcher: "qwen3.5:397b-cloud"
  synthesizer: "qwen3.5:397b-cloud"
topology: council
effort: high
duration_seconds: 228.60
status: completed
cwd: /tmp/claude-hooks-bench-bench-baseline-2026-05-07-NsBk
---

**Bottom line:** The council produces a degraded answer with partial evidence; whether the API returns `status="failed"` or `status="completed"` depends on LangGraph's non-annotated field merge semantics, which the codebase does not explicitly control.

---

### Failure Path Trace

| Stage | Location | Behavior |
|-------|----------|----------|
| **Exception capture** | `consultants/engine/council.py:555-571` | `researcher_node` catches `Exception`, returns tombstone with `error`, `_role_failed`, and `research: ["(researcher lane failed: ...)"]` |
| **State merge** | `consultants/engine/graph.py:64-72` | `research` and `turns` use `operator.add` (concatenate); `error` and `_role_failed` at lines 74-75 are **not annotated** (plain dict merge, last-write-wins) |
| **Stream loop** | `consultants/server/runner.py:137-161` | Try/except at line 155 only fires if graph itself crashes; tombstone returns do NOT trigger this block |
| **Synthesizer input** | `consultants/engine/council.py:718-723` | Receives `state.get("research")` containing both successful findings and tombstone error strings |
| **Status determination** | `consultants/server/runner.py:173-176` | `terminal_status = "failed" if node_error else "completed"` — depends on whether `error` persists after merge |
| **Artifact write** | `consultants/engine/storage.py:199-207` | Writes `summary.md`, `transcript.md`, `metadata.json` regardless of status; `metadata.json` includes `status` and `error` fields |

---

### User-Perceived Failure Mode

1. **API response:** `status` is either `"failed"` or `"completed"` depending on whether the last-merged lane overwrote the `error` field (undefined without explicit `error: None` in successful returns)
2. **summary.md:** Contains a `final_answer` synthesized from partial evidence — structurally complete but potentially missing critical findings from the failed lane
3. **transcript.md:** Shows the failed lane's `RoleTurn` with content like `"(researcher lane failed: HTTP timeout...)"` at `consultants/engine/storage.py:129-158`
4. **metadata.json:** Contains `error` string and `status` value for programmatic inspection

**Risk:** Silent evidence loss. The synthesizer produces a confident answer without knowing which plan items were never investigated.

---

### Recommended Hardening Change

**Add explicit `error: None` to successful `researcher_node` returns** to guarantee deterministic merge semantics:

```python
# consultants/engine/council.py:545-550 (successful return path)
return {
    "research": [text],
    "research_rounds_used": 1,
    "turns": [RoleTurn(...)],
    "error": None,  # NEW: explicitly clear error to prevent tombstone persistence
    "_role_failed": None,  # NEW: explicitly clear failure flag
}
```

**Rationale:** This ensures `error` and `_role_failed` use overwrite semantics predictably. A single failed lane sets `error`; subsequent successful lanes clear it. The `runner.py:173-176` status logic then correctly reflects whether *any* lane failed, and downstream consumers can distinguish "degraded but usable" from "completely failed" by checking if `final_answer` is non-empty despite `error` being set.
