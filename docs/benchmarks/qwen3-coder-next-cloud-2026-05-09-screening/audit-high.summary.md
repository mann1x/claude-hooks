---
session_id: csl-2026-05-09-0651-eaa3
created: "2026-05-09T06:51:06"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "qwen3-coder-next:cloud"
  planner: "qwen3-coder-next:cloud"
  researcher: "qwen3-coder-next:cloud"
  synthesizer: "qwen3-coder-next:cloud"
topology: council
effort: high
duration_seconds: 83.69
status: completed
cwd: /tmp/claude-hooks-bench-bench-baseline-2026-05-07-KnHx
---

The council **does not produce a coherent answer when a researcher lane fails** — it produces a *degraded* answer with `status="failed"` but still emits a `final_answer`, exposing the failure to the user in the transcript only (not in the synthesized answer) *unless* all lanes fail.

**Failure path:**

1. **`consultants/engine/graph.py:60–75`**: `research`, `turns`, `research_rounds_used`, `tokens`, `error_list` use `operator.add`. `error` and `_role_failed` use last-write-wins (no reducer).

2. **`consultants/engine/council.py:554–573`**: When `loop_runner()` raises, `researcher_node` returns a tombstone (not re-raises), populating `error`, `_role_failed`, and inserting failure stubs into `research`. This *does not* raise a graph-level exception — so `runner.py` never hits `status="failed"` via `node_error`.

3. **`consultants/server/runner.py:136–161`**: `compiled.stream()` catches *only* graph-level exceptions (e.g., serialization), not lane-level failures. Therefore, **lane-level failures do NOT trigger `status="failed"`** via `runner.py:155–160`.

4. **`consultants/engine/council.py:715–748`**: Synthesizer sees `state["research"] = [ ..., "(researcher lane failed: HTTP 504 after 3 retries)", ...]` *and* `state["error"]`, `state["_role_failed"]`. However, it does **not** check `state["error"]` or `_role_failed` — only reads `research`, `plan`, `critique`.

5. **`consultants/engine/storage.py:200–208`**: `status="failed"` *only* if `node_error is not None` (`runner.py:172–174`). Since lane failures return tombstones instead of raising, `node_error` remains `None`, so `status="completed"` even when research failed.

**Therefore:**
- `status="completed"` in `metadata.json`, `transcript.md`, and `summary.md`.
- `final_answer` is still produced, *possibly* containing failure stubs if the synthesizer’s prompt naturally includes evidence references (e.g., “Lane 2 reported an error but found…”).
- User sees a *false-positive success* — `status=completed`, but evidence may be invalid or missing.

**Hardening change (only one required):**

**At `consultants/engine/council.py:554–573`, change the `researcher_node` exception handler to re-raise instead of returning tombstones:**

```python
except Exception as e:
    log.exception("researcher lane %s failed: %s", state.get("lane_idx"), e)
    raise RuntimeError(f"Researcher lane {state.get('lane_idx', '?')} failed: {e}") from e
```

This ensures the `compiled.stream()` call in `runner.py:137` hits `runner.py:155–160`, populating `node_error`, setting `status="failed"`, and preventing `storage.write_consultation(..., status="completed")`.

**Path: `consultants/engine/council.py:554–573` → change `return {...}` to `raise`**.
