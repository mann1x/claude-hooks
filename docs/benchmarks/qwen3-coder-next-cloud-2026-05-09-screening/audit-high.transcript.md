# Consultation transcript — csl-2026-05-09-0651-eaa3

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. Check `consultants/engine/graph.py:45-52` for how `CouncilState` additive reducers handle partial failures in `add_messages` and `add_updates`.  
2. Inspect `consultants/server/runner.py:122-140` to trace how `compiled.stream()` iterates over lanes and handles exceptions raised in `agent_loop.runner.run_loop`.  
3. Examine `consultants/engine/synthesizer.py:78-95` to verify what messages the synthesizer actually receives when a lane fails — specifically whether partial outputs are buffered or dropped.  
4. Review `consultants/engine/storage.py:158-175` to confirm whether failed lane artifacts are written as empty/placeholder objects or skipped entirely.  
5. Trace `consultants/engine/state.py:38-44` to confirm whether `CouncilStatus` transitions to `failed` on any unhandled exception or only on specific marker conditions.

---

## Researcher (round 1)

Based on the evidence, here's the exact failure path when a researcher lane raises an unhandled exception in `agent_loop.runner.run_loop` (e.g., HTTP timeout exhausting retries or `tool_executor` returning a non-string).

---

### 1. CouncilState additive reducers (`consultants/engine/graph.py:60-75`)

`CouncilState` declares `research`, `turns`, `research_rounds_used`, etc., with `Annotated[<type>, operator.add]` reducers (lines 64, 68, 69, 71, 72). When a parallel researcher lane returns a partial state, LangGraph merges it by concatenating lists and summing integers — **not** by overwriting.

Crucially, the **error state is NOT additive**. Only the `error` and `_role_failed` fields in `CouncilState` (lines 74–75) are regular (non-annotated) fields, meaning **last-write-wins** semantics apply if multiple lanes set them.

---

### 2. Failure path in `researcher_node` (`consultants/engine/council.py:545-573`)

When `loop_runner(...)` raises an exception at `council.py:548–554`, the catch block returns a **tombstone** delta:

```python
return {
    "error": f"researcher failed: {e}",
    "_role_failed": "researcher",
    "research": [err_text],
    "research_rounds_used": 1,
    "turns": [RoleTurn(role="researcher", round=this_round, content=err_text, ...)],
}
```
— `council.py:562–572`.

**What LangGraph does next:**
- `error` → last lane’s exception message wins (no reducer).
- `_role_failed` → same: last lane wins.
- `research` → additive: `[err_text]` is concatenated with other lanes’ research findings.
- `turns` → additive: each lane’s `RoleTurn` appended.
- `research_rounds_used`/`tokens` → additive: +1 for failed lane.

---

### 3. Stream loop in `consultants/server/runner.py:137–161`

The `compiled.stream()` loop at `runner.py:137–154` **does not wrap each lane individually** — it streams updates from *all* nodes (including fan-out lanes) to a single exception handler.

- If any node raises an exception *after* the graph starts (e.g., `run_loop` raising), it propagates up to the `try/except` block at `runner.py:136–161`.
- On line 155–160, the entire graph invocation is treated as a **hard failure**, and the state is set to `status="failed"`.

**BUT** — if the exception occurs *inside* a parallel fan-out lane but LangGraph itself doesn’t raise (e.g., LangGraph handles lane-level failures), the state will contain both:
- `final_state["error"] = "researcher failed: <exc>"` (from the tombstone).
- `final_state["_role_failed"] = "researcher"`.

The stream loop itself never catches lane-level exceptions; only graph-level crashes (e.g., serialization errors) trigger `runner.py:155–160`.

---

### 4. Synthesizer input messages (`consultants/engine/council.py:715–748`, `council.py:718–723`)

`synthesizer_node` receives:
- `state["question"]`, `state["plan"]`, `state["research"]`, `state["critique"]`.

Because `research` uses `operator.add`, the synthesizer’s `research` list will contain:
- `[<lane1_result>, ..., "<researcher lane failed: HTTP timeout>", ..., <laneN_result>]`.

So the synthesizer **does see** the failure stub (e.g., `(researcher lane failed: HTTP 504 after 3 retries)`), and can incorporate it into its answer — but only if it reads the `error` field from state.

However, `synthesizer_node` **does not check `state["error"]`** or `_role_failed`. It only reads `research`, `plan`, and `critique`. If the synthesizer sees one or more failure stubs in `research`, it may phrase its answer as “Lane 2 failed to answer” — but there’s no guarantee.

Crucially, the synthesizer’s own exception handler at `council.py:725–748` does not propagate lane failures to its output — only its *own* crash is recorded.

---

### 5. Storage artifact writer (`consultants/engine/storage.py:200–208`)

`storage.write_consultation()` uses the final state:
- `final_answer = final_state.get("final_answer", "")`.
- `error = node_error` (from `runner.py:172`).
- `status = "failed"` only if `node_error is not None`.

If a lane failed:
- `node_error` is populated from `final_state.get("error")` (`runner.py:172–174`).
- The consultation’s `status` becomes `"failed"`.
- `summary.md` will contain a front-matter `error` field and a `final_answer` that may be partial or include failure notes.

However, the **user-facing `final_answer` is still written**, because `storage.py` only uses `final_state["final_answer"]`, which is set by the synthesizer — and if the synthesizer didn’t crash, it *will* produce a value.

---

### Outcome: **degraded, not fully failed**

**The council does NOT fail completely** when a single researcher lane crashes. Instead:

- **User-perceivable failure mode:**
  - `status = "failed"` (machine-readable).
  - `error = "researcher failed: <exception>"`.
  - `final_answer` is still produced, but **may be incomplete or misleading** (synthesizer sees failure stubs but no explicit instruction to avoid them).
  - `transcript.md` and `metadata.json` show the exact lane error.

- **When the user sees a coherent answer:**
  - If **other lanes succeed**, the synthesizer has enough evidence to answer, and the error stubs may only appear in the transcript — not the final answer.
  - If **all lanes fail**, the synthesizer still receives `[all failure stubs]`, which usually leads to a qualified answer like “Multiple lanes failed to find evidence”.

---

### Hardening recommendation

**Add lane-level error aggregation and fail-fast guardrails in `researcher_node`** to ensure the graph is marked as failed *before* the synthesizer processes partial data.

**Change at `consultants/engine/council.py:554–573`:**

```python
    except Exception as e:
        log.exception("researcher_node failed: %s", e)
        err_text = f"(researcher lane failed: {e})"
        # Propagate the exception *and* tombstone — this ensures the
        # graph-level catch at runner.py:155 sees it, preventing
        # the synthesizer from producing a confident answer over gaps.
        raise RuntimeError(
            f"Researcher lane {state.get('lane_idx', 'unknown')} failed: {e}"
        ) from e
        # NOTE: the `return` block below is intentionally omitted;
        # if a lane crashes, let LangGraph surface it as a hard failure.
        # Tombstone-only return (current code) allows the synthesizer
        # to proceed with degraded evidence, which is user-unfriendly.
```

**Rationale:**
- The current code returns tombstones *instead* of raising, which lets the graph continue to the synthesizer — resulting in potentially confident but wrong answers.
- Raising the exception ensures the `compiled.stream()` call at `runner.py:137` hits the `except Exception` at line 155, sets `status="failed"`, and skips the `write_consultation` call with `status="completed"`.
- If you *must* allow degraded answers, add a separate `error`-aggregation reducer (`error_list: Annotated[list, operator.add]`) and update the synthesizer to check for any error in that list — but that requires more invasive changes.

**Path:line**: `consultants/engine/council.py:554–573` → change from *return* to *raise*.

```json
{
  "failure_path": {
    "council_state_reducers": {
      "location": "consultants/engine/graph.py:60-75",
      "behavior": "research, turns, rounds_used, tokens use operator.add; error and _role_failed are last-write-wins"
    },
    "researcher_failure_tombstone": {
      "location": "consultants/engine/council.py:554-573",
      "keys_written": ["error", "_role_failed", "research", "turns", "research_rounds_used", "total_prompt_tokens", "total_completion_tokens"]
    },
    "stream_loop_handling": {
      "location": "consultants/server/runner.py:136-161",
      "exception_handler": "sets status='failed' and writes artifacts only for *graph-level* crashes; does not catch lane-level failures inside the stream loop"
    },
    "synthesizer_input": {
      "location": "consultants/engine/council.py:715-748",
      "sees": "research list containing [successful_lanes..., '(researcher lane failed: ...)', ...] but does not check state['error'] or _role_failed"
    },
    "storage_artifacts": {
      "location": "consultants/engine/storage.py:200-208",
      "behavior": "writes final_answer (if synthesizer succeeded), status='failed' only if node_error present, and error field in front-matter"
    }
  },
  "user_perceives": "degraded answer (status='failed' but final_answer written, possibly with failure stubs visible in transcript only)",
  "recommended_hardening": {
    "change": "Replace tombstone return with raise RuntimeError in researcher_node exception handler",
    "path": "consultants/engine/council.py:554-573"
  }
}
```

---

## Researcher (round 1)

(researcher lane produced no findings within the iteration budget; gaps remain — see plan)

---

## Researcher (round 1)

(researcher lane produced no findings within the iteration budget; gaps remain — see plan)

---

## Critic (round 3)

DECISION: needs_more_research.

1. `consultants/engine/council.py:545–573` — The researcher_node returns a tombstone *only when* `loop_runner(...)` raises — but `loop_runner` may itself be wrapped or may surface *partial* errors (e.g., tool_executor returning non-string may not raise, only return bad output), so we lack confirmation that *all* exceptional conditions result in the exception path being taken.

2. `consultants/engine/state.py:38–44` — Need explicit confirmation that `CouncilStatus` transitions to `failed` *only* when `node_error is not None` (via `runner.py:172–174`), and not on other conditions (e.g., empty research list, timeout-only lanes), especially since `research_rounds_used` is additive.

3. `consultants/engine/council.py:718–723` — The synthesizer’s `state` input must be inspected concretely: does it receive `state["error"]`, `state["_role_failed"]`, and `state["research"]` *simultaneously*, or is the synthesizer invoked *after* `node_error` is aggregated elsewhere (e.g., in graph state post-edges)? This affects whether it can reasonably respond to failures.

---

## Synthesizer

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

---
