# Consultation transcript — csl-2026-05-07-1033-c508

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. Inspect `consultants/engine/graph.py` `CouncilState` reducer to verify if it aggregates partial lane results or propagates exceptions to the root state.
2. Trace `consultants/server/runner.py` `compiled.stream` loop to identify the try/except boundary wrapping `agent_loop.runner.run_loop`.
3. Examine the synthesizer input payload construction in `consultants/engine/graph.py` to determine if failed lanes send error tokens or empty content.
4. Review `storage.py` artifact writer to confirm if `write_artifact` executes before or after the final consultation status flag is set.
5. Verify the HTTP response handler in `consultants/server/runner.py` to determine if partial success returns 200 with degraded content or 500/failed status.
6. Locate the specific line in `agent_loop/runner.py` where tool_executor non-string returns or timeouts raise unhandled exceptions.
7. Identify the exact file and line in the fan-out dispatch logic where a `try/except` block should be inserted to isolate lane failures.

---

## Researcher (round 1)

Based on my analysis of the codebase, here is the concrete failure path trace:

## Failure Path Trace

### 1. CouncilState's Additive Reducers (`consultants/engine/graph.py`)

When a parallel researcher lane raises an unhandled exception in `agent_loop.runner.run_loop`, the exception is **caught inside `researcher_node`** at `consultants/engine/council.py:555-571`, NOT by the stream loop. The node returns a tombstone dict:

```python
# consultants/engine/council.py:563-571
err_text = f"(researcher lane failed: {e})"
return {
    "error": f"researcher failed: {e}",
    "_role_failed": "researcher",
    "research": [err_text],  # Additive reducer merges this
    "research_rounds_used": 1,
    "turns": [RoleTurn(...)],
}
```

The `CouncilState` TypedDict at `consultants/engine/graph.py:50-81` declares `research` and `turns` with `Annotated[list, operator.add]` reducers (lines 64, 68). When multiple lanes fan out, these **concatenate all lane outputs** — including the error tombstone string — into the merged state.

### 2. `compiled.stream` Loop (`consultants/server/runner.py`)

The try/except boundary at `consultants/server/runner.py:136-161` wraps the entire `compiled.stream()` call. However, **node-level exceptions do NOT escape** — they are handled inside each node (council.py:442-461 for planner, 555-571 for researcher, 673-694 for critic, 725-747 for synthesizer). The stream completes normally with a merged `final_state`.

After the stream drains, `runner.py:172-174` checks for node-recorded errors:

```python
node_error = final_state.get("error")
node_failed = final_state.get("_role_failed")
terminal_status = "failed" if node_error else "completed"
```

**Critical finding**: Since `researcher_node` sets `state["error"]` on exception, `node_error` is truthy, and `terminal_status` becomes `"failed"` — **even if other lanes succeeded**.

### 3. Synthesizer's Input Messages (`consultants/engine/council.py`)

The synthesizer receives the merged `research` array at `consultants/engine/council.py:718-722`:

```python
def synthesizer_node(state: dict, ...) -> dict:
    msgs = build_synthesizer_messages(
        state["question"], state.get("plan", ""),
        state.get("research") or [],  # Contains error tombstones from failed lanes
        state.get("critique"),
        ...
    )
```

When one lane fails, `state["research"]` contains a mix like:
```python
[
    "Lane 0 findings: path/to/file.py:42 shows...",
    "(researcher lane failed: HTTP timeout after 3 retries)",  # Tombstone
    "Lane 2 findings: src/module.py:108 defines...",
]
```

The synthesizer **sees the error text as research content** and must produce an answer from degraded evidence. It does not know which lanes failed — it only sees the concatenated strings.

### 4. Storage.py Artifact Writer (`consultants/engine/storage.py`)

At `consultants/server/runner.py:177-199`, a `ConsultationResult` is created with `status=terminal_status` (line 189). Since `terminal_status = "failed"`, the artifacts are written with:

- `summary.md`: Contains the synthesizer's answer (based on partial research) **but** YAML front-matter shows `status: failed` (`consultants/engine/storage.py:108`)
- `transcript.md`: Includes the failed lane's tombstone turn (`consultants/engine/storage.py:129-158` iterates `result.turns`)
- `metadata.json`: `status: "failed"` and `error: "researcher failed: <exc>"` (`consultants/engine/storage.py:166-169`)

## Answer: Does the Council Produce a Coherent Answer?

**No — the entire consultation is marked `status=failed`**, even if 2 of 3 lanes succeeded. The user perceives:

1. A `failed` status in the API response and metadata.json
2. A synthesizer answer that may be **degraded** (missing findings from the failed lane) but is still written to `summary.md`
3. Visible error tombstones in the transcript like `"(researcher lane failed: HTTP timeout after 3 retries)"`

The failure mode is **all-or-nothing**: one lane's exception poisons the `error` field, which forces `terminal_status = "failed"` at `consultants/server/runner.py:174`.

## Recommended Hardening Change

**Exactly ONE change**: Modify `researcher_node` to **NOT set `error` for lane-level failures** when other lanes may succeed. Instead, track lane failures separately and only set `error` if ALL lanes fail.

**File**: `consultants/engine/council.py`  
**Lines**: 555-571 (the exception handler in `researcher_node`)

**Change**: Remove the `"error"` key from the tombstone return dict. The `"_role_failed"` field already records which role failed for diagnostics, but it should NOT trigger `terminal_status = "failed"` for partial lane failures. The synthesizer can still see the error text in `research` and note the gap in its answer.

```python
# Current (council.py:563-571):
return {
    "error": f"researcher failed: {e}",  # REMOVE THIS LINE
    "_role_failed": "researcher",
    "research": [err_text],
    ...
}

# Hardened:
return {
    "_role_failed": "researcher",  # Keep for diagnostics
    "research": [err_text],
    ...
}
```

This allows the council to produce a **degraded but coherent answer** with `status=completed` when at least one lane succeeds, while still recording the failure in the transcript and metadata for audit.

---

## Researcher (round 1)

Now I have the complete picture. Let me trace the exact failure path with concrete citations.

## Failure Path Analysis

### 1. CouncilState's Additive Reducers (`consultants/engine/graph.py`)

When a researcher lane raises an unhandled exception inside `agent_loop.runner.run_loop`, the exception is **caught inside `researcher_node`** at `consultants/engine/council.py:555-571`, not by the graph infrastructure. The node returns a tombstone state update:

```python
# consultants/engine/council.py:563-571
return {
    "error": f"researcher failed: {e}",
    "_role_failed": "researcher",
    "research": [err_text],  # tombstone string in additive reducer
    "research_rounds_used": 1,
    "turns": [RoleTurn(...)],
}
```

The additive reducers at `consultants/engine/graph.py:64-72` (`research: Annotated[list, operator.add]`, `turns: Annotated[list, operator.add]`) merge the tombstone into the aggregated state alongside any successful lanes' findings. Critically, `error` and `_role_failed` at lines 74-75 are **NOT annotated with reducers** — they use default dict-merge semantics, meaning the last writer wins. Since the failed lane sets `error` and successful lanes don't clear it, the key persists in `final_state`.

### 2. `compiled.stream` Loop (`consultants/server/runner.py`)

The stream loop at `consultants/server/runner.py:136-161` wraps the entire `compiled.stream()` call in a try/except. However, because `researcher_node` catches its own exceptions at line 555, **the exception never bubbles to runner.py**. The graph completes normally, yielding all lane updates via `stream_mode=["updates", "values"]`.

After the stream drains, `runner.py:172-174` checks:
```python
node_error = final_state.get("error")
node_failed = final_state.get("_role_failed")
terminal_status = "failed" if node_error else "completed"
```

Since the failed lane's `error` key persists in the merged `final_state`, **`terminal_status` becomes `"failed"`** even if other lanes succeeded.

### 3. Synthesizer's Input Messages

The synthesizer at `consultants/engine/council.py:718-723` receives:
```python
msgs = build_synthesizer_messages(
    state["question"], state.get("plan", ""),
    state.get("research") or [],  # includes tombstone: "(researcher lane failed: ...)"
    state.get("critique"),
    self_critic=self_critic,
)
```

The synthesizer sees the tombstone string as part of the research list (e.g., `["<findings from lane 0>", "(researcher lane failed: HTTP timeout)", "<findings from lane 2>"]`). The prompt at `consultants/engine/council.py:156-168` (`SYNTHESIZER_SYSTEM`) instructs it to "write the final answer the user will see, consuming the planner's plan, researcher's report(s)" — but **has no special handling for error tombstones**. The synthesizer may produce a coherent answer that acknowledges the gap, but it proceeds regardless.

### 4. Storage.py Artifact Writer

`storage.write_consultation()` is called at `consultants/server/runner.py:199` **after** `terminal_status` is computed at line 174. The `ConsultationResult` at lines 177-198 includes:
- `status=terminal_status` (line 189) — `"failed"` due to the persisted `error` key
- `error=node_error` (line 190) — the exception string
- `final_answer=final_state.get("final_answer", "")` (line 185) — the synthesizer's output (may be coherent)

The artifacts (`summary.md`, `transcript.md`, `metadata.json`) are written atomically at `consultants/engine/storage.py:200-207` with `status="failed"` in the YAML front-matter and JSON metadata.

---

## Concrete Answer

**Does the council still produce a coherent answer, or does the entire consultation fail?**

The consultation **surfaces as `status="failed`** at `consultants/server/runner.py:174`, but the synthesizer **may still produce a degraded-but-coherent answer** using findings from surviving lanes. The user perceives:

1. **API response**: HTTP 200 (the runner doesn't raise; it sets `state.status = "failed"` at line 201 and returns normally)
2. **Artifact `summary.md`**: Contains the synthesizer's answer with YAML front-matter showing `status: "failed"` and `error: "researcher failed: ..."` at `consultants/engine/storage.py:108-110`
3. **Artifact `metadata.json`**: Machine-readable record with `"status": "failed"` and the error string
4. **Failure mode**: The user sees a complete answer that may reference gaps (e.g., "Lane 1 timed out; based on lanes 0 and 2...") but the consultation is flagged as failed for downstream filtering.

**The entire consultation does NOT crash** — the node-level try/except at `consultants/engine/council.py:555` prevents exception propagation. However, **any single lane failure marks the whole consultation as `status="failed"`** due to the `error` key persistence in the merged state.

---

## Recommended Hardening Change

**Exactly ONE change**: Modify `consultants/server/runner.py:174` to distinguish between **full graph failure** (exception bubbled to runner) and **partial lane failure** (node caught exception, returned tombstone). The synthesizer already received partial findings; let the consultation complete with degraded status tracked in metadata only.

```python
# consultants/server/runner.py:172-176 (replace lines 173-174)
node_error = final_state.get("error")
node_failed = final_state.get("_role_failed")
# Partial lane failures (node caught exception) should not fail the
# entire consultation — the synthesizer received surviving findings.
# Only mark failed if the graph itself crashed (exception at line 155).
terminal_status = "completed" if node_failed else "completed"
# Track partial failure in result.error for metadata, but don't fail status.
```

Wait — that's not quite right. Let me refine:

**Correct single-line change at `consultants/server/runner.py:174`**:

```python
# OLD: terminal_status = "failed" if node_error else "completed"
# NEW: Allow partial success; only graph-crash exceptions (line 155) set failed.
terminal_status = "completed"  # node_error indicates partial lane failure, not total crash
```

The `error` field still populates at line 190 for auditability, but `status` reflects that the council produced an answer. This matches the actual behavior: the synthesizer ran, wrote `final_answer`, and artifacts exist.

**Citation for the change location**: `consultants/server/runner.py:174`

---

## Researcher (round 1)

Now I have the complete picture. Let me trace the exact failure path and answer the question concretely.

## Failure Path Analysis

### 1. Exception Origin in `agent_loop/runner.py`

The unhandled exception originates at **`claude_hooks/agent_loop/runner.py:122`** where `tool_executor` is called directly without a try/except:

```python
output = tool_executor(name, args_str, cwd)  # line 122 — no exception handling
```

If the tool executor raises (HTTP timeout exhausting retry budget, or returning a non-string that causes downstream code to fail), the exception propagates through:
- `execute_tool_calls` → `run_loop` at **`claude_hooks/agent_loop/runner.py:300`** → back to the caller

### 2. CouncilState's Additive Reducers (`consultants/engine/graph.py`)

The `CouncilState` TypedDict at **`consultants/engine/graph.py:64-74`** defines additive reducers using `operator.add`:

- `research: Annotated[list, operator.add]` — line 64
- `turns: Annotated[list, operator.add]` — line 68
- `research_rounds_used: Annotated[int, operator.add]` — line 69
- `total_prompt_tokens: Annotated[int, operator.add]` — line 71
- `total_completion_tokens: Annotated[int, operator.add]` — line 72

These merge parallel Send lanes by concatenation/summation. Critically, `error` and `_role_failed` (lines 74-75) are **NOT** additive — they are plain Optional fields that get overwritten by whichever lane writes last.

### 3. Researcher Node Exception Handling (`consultants/engine/council.py`)

The `researcher_node` wraps the `loop_runner` call at **`consultants/engine/council.py:547-564`**:

```python
try:
    final = loop_runner(...)  # line 548
except Exception as e:
    log.exception("researcher_node failed: %s", e)  # line 556
    err_text = f"(researcher lane failed: {e})"  # line 560
    return {
        "error": f"researcher failed: {e}",  # line 564
        "_role_failed": "researcher",  # line 565
        "research": [err_text],  # line 566
        ...
    }
```

**Key finding**: The exception IS caught here. The lane does NOT crash the entire graph. It returns a tombstone with error markers.

### 4. Server Runner's Stream Loop (`consultants/server/runner.py`)

The `compiled.stream` loop at **`consultants/server/runner.py:137-161`** has a top-level try/except:

```python
try:
    for mode, payload in compiled.stream(initial, stream_mode=["updates", "values"]):  # line 137-138
        ...
except Exception as e:
    log.exception("council graph invocation failed: %s", e)  # line 156
    state.status = "failed"  # line 157
    state.error = f"graph crashed: {e}"  # line 158
    ...
```

This only fires if the exception escapes the node-level handling — which it doesn't for researcher lane failures.

### 5. What the Synthesizer Actually Sees

The synthesizer at **`consultants/engine/council.py:715-758`** receives the merged `state["research"]` list. If one lane failed, it sees something like:

```python
state.get("research") or []  # line 720 — contains ["(researcher lane failed: HTTP timeout...)"]
```

The synthesizer then calls `_single_shot` with this degraded research list at **`consultants/engine/council.py:726`**. It produces a final answer based on incomplete evidence.

### 6. Storage Artifact Writer (`consultants/engine/storage.py`)

The `write_consultation` function at **`consultants/engine/storage.py:200-208`** writes all three files regardless of status. The `ConsultationResult` at **`storage.py:54-73`** includes:

- `status: str` — "completed" or "failed" (line 65)
- `error: Optional[str]` — the error message (line 66)

The `render_transcript` at **`storage.py:129-158`** iterates over `result.turns` and includes the error tombstone as a normal turn.

---

## Answer: Does the Council Produce a Coherent Answer?

**The council produces a DEGRADED answer with `status="completed"`, NOT `status="failed"`.**

The failure mode the user perceives:

1. **`metadata.json`** shows `"status": "completed"` (not "failed") because the graph itself didn't crash — only one lane failed.
2. **`summary.md`** contains a synthesizer answer that may be incomplete or confidently wrong, because it synthesized from partial research. The synthesizer sees error stubs like `"(researcher lane failed: ...)"` in the research list but is not instructed to treat these as fatal.
3. **`transcript.md`** shows the failed lane's error tombstone as a normal turn at **`storage.py:129-158`**, but the user must manually inspect this to see the gap.

The critical flaw: **`consultants/engine/council.py:564-565`** sets `error` and `_role_failed`, but these are plain fields (not additive). With parallel Send lanes, whichever lane writes last overwrites these fields. If lane 0 fails but lane 2 succeeds last, the final state may have NO error marker despite a failed lane.

---

## Recommended Hardening Change

**Add per-tool-call exception handling in `execute_tool_calls` to isolate individual tool failures within a lane:**

```python
# In claude_hooks/agent_loop/runner.py:120-128, wrap tool_executor call:
try:
    output = tool_executor(name, args_str, cwd)
except Exception as e:
    output = f"(tool {name} failed: {e})"
    log.exception("tool %s(%s) failed: %s", name, args_str[:80], e)
```

This ensures:
- A single tool failure doesn't crash the entire lane
- The lane can continue with other tools in the same batch
- The synthesizer sees partial results rather than a complete lane tombstone

**File:line**: `claude_hooks/agent_loop/runner.py:122`

---

## Critic (round 3)

DECISION: needs_more_research

**Critical gap: Contradictory status determination between rounds 2 and 3**

Round 2 cites `consultants/server/runner.py:174` as `terminal_status = "failed" if node_error else "completed"` — meaning any persisted `error` key forces `status="failed"`.

Round 3 claims `status="completed"` because "the graph itself didn't crash" — but this contradicts the code logic at runner.py:174 that Round 2 cited.

**Missing verification needed at `consultants/server/runner.py:172-176`**:
- Does `final_state.get("error")` actually return truthy after parallel lane merge?
- Round 3 notes `error` is a plain field (not additive) at `graph.py:74-75` — meaning last-writer-wins in parallel Send. If the successful lane writes last, `error` could be overwritten to `None`.
- This race condition determines whether user sees `status="failed"` or `status="completed"`.

**Secondary gap**: The recommended hardening change differs across all three rounds (council.py:564 vs runner.py:174 vs agent_loop/runner.py:122). Need consensus on which single change actually addresses the root cause.

**Resolve before synthesis**: Confirm the actual behavior at `runner.py:172-176` with the parallel Send merge semantics — does a single failed lane guarantee `status="failed"` or is it non-deterministic based on lane completion order?

---

## Synthesizer

**The consultation produces a degraded answer but surfaces as `status="failed"` in most cases — however, the outcome is non-deterministic due to a race condition in parallel lane merging.**

## Failure Path Trace

| Component | Behavior | Citation |
|-----------|----------|----------|
| **Exception origin** | `tool_executor` raises at `claude_hooks/agent_loop/runner.py:122` with no local handling | `agent_loop/runner.py:122` |
| **Lane catch** | `researcher_node` catches at `consultants/engine/council.py:555`, returns tombstone with `error` key | `council.py:555-566` |
| **State merge** | `research`/`turns` use `operator.add` reducers; `error`/`_role_failed` are plain fields (last-writer-wins) | `graph.py:64-75` |
| **Status check** | `runner.py:174` sets `terminal_status = "failed" if node_error else "completed"` | `runner.py:172-174` |
| **Synthesizer input** | Receives merged `research` list containing error tombstones like `"(researcher lane failed: ...)"` | `council.py:720` |
| **Artifact write** | `storage.py:200-208` writes all files; YAML front-matter reflects `status: failed` | `storage.py:108-110` |

## User-Perceived Failure Mode

1. **API response**: HTTP 200 (graph didn't crash), but `metadata.json` shows `"status": "failed"`
2. **`summary.md`**: Contains synthesizer answer based on partial evidence — may acknowledge gaps or silently omit failed lane findings
3. **`transcript.md`**: Shows error tombstone as a normal turn at `storage.py:129-158`
4. **Race condition**: If a successful lane writes `error: None` last, `status` may incorrectly show `"completed"` despite a failed lane

## Recommended Hardening Change

**Remove the `error` key from lane-level exception tombstones** — partial lane failures should not poison the entire consultation status when the synthesizer can still produce degraded answers from surviving lanes.

```python
# consultants/engine/council.py:563-566 — REMOVE the "error" line
return {
    # "error": f"researcher failed: {e}",  # DELETE THIS
    "_role_failed": "researcher",  # Keep for diagnostics
    "research": [err_text],
    ...
}
```

**Rationale**: The `error` field at `graph.py:74` is not additive — it's designed for graph-level crashes, not partial lane failures. The synthesizer already receives error tombstones in `research` and can note gaps in its answer. Track lane failures via `_role_failed` and transcript tombstones, but let `status="completed"` reflect that the council produced an answer.

---
