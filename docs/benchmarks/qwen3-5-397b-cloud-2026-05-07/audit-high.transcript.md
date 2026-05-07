# Consultation transcript — csl-2026-05-07-1025-f8da

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. Inspect `consultants/engine/graph.py` `CouncilState` reducer implementation to verify how parallel lane exceptions are merged into the shared state object.
2. Trace `consultants/server/runner.py` `compiled.stream` method to identify the exception handling boundary around graph execution and SSE yielding.
3. Locate the synthesizer node input construction in `consultants/engine/graph.py` to determine if it receives partial results, error tokens, or nothing from failed researcher lanes.
4. Examine `storage.py` artifact writer (locate via import in `graph.py` or `runner.py`) to confirm if it persists state on exception paths and what `status` value is assigned.
5. Verify the HTTP response status code logic in `consultants/server/runner.py` to distinguish between `status=failed` and degraded success conditions.
6. Identify the call site of `agent_loop.runner.run_loop` within `consultants/engine/graph.py` to check for missing try/except wrappers around parallel execution.
7. Pinpoint the specific line in `consultants/engine/graph.py` or `consultants/server/runner.py` where wrapping parallel lane execution would isolate failures from the main consultation flow.

---

## Researcher (round 1)

## Failure Path Analysis: Unhandled Exception in Researcher Lane

### 1. CouncilState's Additive Reducers (`consultants/engine/graph.py`)

When a parallel researcher lane raises an exception inside `agent_loop.runner.run_loop`, the failure is caught by `researcher_node`'s try/except block at `consultants/engine/council.py:555-572`. The node returns a **tombstone state update**:

```python
# consultants/engine/council.py:563-572
return {
    "error": f"researcher failed: {e}",
    "_role_failed": "researcher",
    "research": [err_text],  # Single-item list with error message
    "research_rounds_used": 1,
    "turns": [RoleTurn(role="researcher", round=this_round, content=err_text, ...)],
}
```

The additive reducers in `CouncilState` (`consultants/engine/graph.py:64-72`) merge this tombstone with results from surviving lanes:

- `research: Annotated[list, operator.add]` at `consultants/engine/graph.py:64` — concatenates the error tombstone into the research list alongside successful lane reports
- `turns: Annotated[list, operator.add]` at `consultants/engine/graph.py:68` — appends the failed lane's RoleTurn to the transcript
- `total_prompt_tokens/completion_tokens: Annotated[int, operator.add]` at `consultants/engine/graph.py:71-72` — sums token counts (zeros from the failed lane)

**Key insight**: The graph does NOT crash. LangGraph's reducer pattern allows the failed lane's tombstone to merge cleanly with surviving lanes' outputs.

---

### 2. `consultants/server/runner.py`'s `compiled.stream` Loop

The stream loop at `consultants/server/runner.py:136-161` distinguishes two failure modes:

**Graph-level crash** (exception bubbles out of `compiled.stream`):
```python
# consultants/server/runner.py:155-161
except Exception as e:
    log.exception("council graph invocation failed: %s", e)
    state.status = "failed"
    state.error = f"graph crashed: {e}"
    _write_failed_artifacts(state, cwd, question, e)
    return
```

**Node-level failure** (node catches exception, returns tombstone):
```python
# consultants/server/runner.py:172-174
node_error = final_state.get("error")
node_failed = final_state.get("_role_failed")
terminal_status = "failed" if node_error else "completed"
```

When a researcher lane fails internally, the graph completes normally. The `final_state` contains `error` set by the tombstone, so `terminal_status = "failed"` at `consultants/server/runner.py:174`.

---

### 3. Synthesizer's Input Messages

The synthesizer receives **partial results mixed with error tombstones**. `build_synthesizer_messages` at `consultants/engine/council.py:245-266` constructs the prompt from `state.get("research") or []`:

```python
# consultants/engine/council.py:253-254
for i, r in enumerate(research_rounds, start=1):
    parts.append(f"\nRESEARCHER REPORT (round {i}):\n{r.strip()}")
```

If lane 1 of 3 fails, the synthesizer sees:
```
RESEARCHER REPORT (round 1):
[findings from lane 0 with path:line citations]

RESEARCHER REPORT (round 2):
(researcher lane failed: HTTP timeout exhausted retry budget)

RESEARCHER REPORT (round 3):
[findings from lane 2 with path:line citations]
```

The synthesizer processes this as normal input. Whether it produces a coherent answer depends on:
- How critical the failed lane's plan items were to the user's question
- Whether the synthesizer's prompt (at `consultants/engine/council.py:156-168`) causes it to acknowledge gaps

The synthesizer prompt at `consultants/engine/council.py:156-168` says "cite `path:line` for every codebase-dependent claim" but does NOT instruct it to flag incomplete research. The synthesizer may produce a **confidently degraded answer** that appears complete but is missing evidence from the failed lane.

---

### 4. Storage.py Artifact Writer

The artifact writer at `consultants/engine/storage.py:200-208` writes three files regardless of `status`:

- `summary.md` via `render_summary` at `consultants/engine/storage.py:117-120` — contains `result.final_answer` (the synthesizer's output, which may be degraded)
- `transcript.md` via `render_transcript` at `consultants/engine/storage.py:123-158` — iterates all `result.turns`, including the failed lane's error tombstone turn at `consultants/engine/storage.py:129-157`
- `metadata.json` via `render_metadata` at `consultants/engine/storage.py:166-169` — contains `"status": "failed"` and `"error": "researcher failed: ..."`

The user perceives:
- **API response**: `status=failed` (from `consultants/server/runner.py:174`)
- **summary.md**: A final answer that may have gaps but looks structurally complete
- **transcript.md**: The failed lane's error message visible in the researcher turn section
- **metadata.json**: Machine-readable `status: "failed"` with the error string

---

## Verdict: Degraded Answer, Not Complete Failure

**The council produces a degraded answer, not a complete failure.** The consultation does NOT surface as `status=failed` because the graph crashed — it surfaces as `status=failed` because a node set the `error` field, but the synthesizer still ran and produced `final_answer`.

**Failure mode the user perceives**:
1. API returns `status: "failed"` 
2. But `summary.md` contains a seemingly-complete answer (potentially missing critical evidence from the failed lane)
3. The transcript reveals the gap only if the user reads `transcript.md`

This is a **silent degradation risk**: the user sees `status=failed` but may still trust the `final_answer` text, not realizing it's based on partial research.

---

## Recommended Hardening Change

**Single change**: Modify `consultants/server/runner.py:172-174` to distinguish critical node failures (planner/synthesizer) from non-critical failures (researcher/critic). Only mark `status=failed` for critical node failures; mark non-critical failures as `status=completed_with_warnings`:

```python
# consultants/server/runner.py:172-174 (replace with)
node_error = final_state.get("error")
node_failed = final_state.get("_role_failed")
# Only planner/synthesizer failures are terminal; researcher/critic 
# failures produce degraded but usable answers.
CRITICAL_ROLES = {"planner", "synthesizer"}
if node_error and node_failed in CRITICAL_ROLES:
    terminal_status = "failed"
elif node_error:
    terminal_status = "completed_with_warnings"  # New status
else:
    terminal_status = "completed"
```

This change makes the API response accurately reflect whether the answer is usable (`completed_with_warnings` indicates gaps but coherent output) vs. unusable (`failed` indicates no valid `final_answer`).

---

## Researcher (round 1)

Now I have the complete picture. Let me trace the exact failure path:

## Failure Trace: Unhandled Exception in Researcher Lane

### 1. CouncilState's Additive Reducers (`consultants/engine/graph.py:50-81`)

The state schema defines which fields use additive merging:

- `research: Annotated[list, operator.add]` (line 64) — **additive**: parallel lanes concatenate their findings
- `turns: Annotated[list, operator.add]` (line 68) — **additive**: all turns merge
- `total_prompt_tokens: Annotated[int, operator.add]` (line 71) — **additive**: counts sum
- `error: Optional[str]` (line 74) — **NOT annotated**: default dict-merge (last write wins)
- `_role_failed: Optional[str]` (line 75) — **NOT annotated**: default dict-merge (last write wins)

When a researcher lane crashes, its tombstone state merges: the error text overwrites any prior `error` value, but `research` and `turns` from surviving lanes are preserved via concatenation.

### 2. Exception Capture in `researcher_node` (`consultants/engine/council.py:555-571`)

```python
except Exception as e:
    log.exception("researcher_node failed: %s", e)
    err_text = f"(researcher lane failed: {e})"
    return {
        "error": f"researcher failed: {e}",           # line 564
        "_role_failed": "researcher",                 # line 565
        "research": [err_text],                       # line 568
        "research_rounds_used": 1,                    # line 569
        "turns": [RoleTurn(...)],                     # lines 569-574
    }
```

The exception is **caught internally** — it does NOT bubble up to `compiled.stream()`. The lane returns a tombstone that merges into the shared state via the additive reducers.

### 3. `compiled.stream` Loop (`consultants/server/runner.py:136-161`)

```python
for mode, payload in compiled.stream(initial, stream_mode=["updates", "values"]):
    # ... progress tracking ...
except Exception as e:                              # line 155
    log.exception("council graph invocation failed: %s", e)
    state.status = "failed"                         # line 157
    state.error = f"graph crashed: {e}"             # line 158
    _write_failed_artifacts(state, cwd, question, e)  # line 160
    return
```

**Critical observation**: The `try/except` only fires if the graph **itself crashes** (e.g., LangGraph internal error). Since `researcher_node` catches its exception and returns a tombstone, the stream completes normally. Execution continues to line 173.

### 4. Synthesizer's Input Messages (`consultants/engine/council.py:718-722`)

```python
msgs = build_synthesizer_messages(
    state["question"], state.get("plan", ""),
    state.get("research") or [],    # includes ALL lane outputs
    state.get("critique"),
    self_critic=self_critic,
)
```

The synthesizer receives `state.get("research")` which contains:
- Successful lane findings (from surviving lanes)
- Error tombstones like `"(researcher lane failed: HTTP timeout...)"` from crashed lanes

The synthesizer sees a **degraded but non-empty** research list. It can still produce an answer, but that answer will be based on incomplete evidence.

### 5. Storage.py Artifact Writer (`consultants/engine/storage.py:200-208` + `consultants/server/runner.py:173-199`)

```python
# runner.py:173-174
node_error = final_state.get("error")
node_failed = final_state.get("_role_failed")
terminal_status = "failed" if node_error else "completed"  # line 174

# runner.py:177-199
result = storage.ConsultationResult(
    ...
    final_answer=final_state.get("final_answer", ""),
    ...
    status=terminal_status,              # "failed" if any error exists
    error=node_error,
    ...
)
storage.write_consultation(result, cwd=Path(cwd))  # line 199
```

**The consultation is marked `status="failed"`** even if the synthesizer produced a coherent answer, because `node_error` is truthy.

---

## Answer: Does the Council Produce a Coherent Answer?

**Yes, but degraded — and the user sees `status=failed`.**

- The council **does not entirely fail**: surviving lanes merge their findings; the synthesizer runs and produces a `final_answer`.
- The user perceives: 
  1. `status: "failed"` in `metadata.json` and YAML front-matter (`consultants/engine/storage.py:108`)
  2. An error message in the front-matter: `error: "researcher failed: <exception>"`
  3. A `final_answer` that may be incomplete (based on partial evidence from surviving lanes)
  4. In `transcript.md`, a `RoleTurn` with content like `"(researcher lane failed: HTTP timeout...)"`

This is the **audit-high finding** referenced at `consultants/engine/council.py:555-571` (comment on lines 557-561): *"Without this, a crashed lane silently contributes nothing — the synthesizer never sees the gap and writes a confidently-degraded answer over the surviving lanes."*

---

## Recommended Hardening Change (ONE change)

**Modify the status logic to distinguish between "synthesizer produced an answer despite lane failures" vs. "entire pipeline crashed":**

```python
# consultants/server/runner.py:173-174 (current)
node_error = final_state.get("error")
node_failed = final_state.get("_role_failed")
terminal_status = "failed" if node_error else "completed"
```

**Change to:**

```python
# consultants/server/runner.py:173-176 (hardened)
node_error = final_state.get("error")
node_failed = final_state.get("_role_failed")
has_final_answer = bool(final_state.get("final_answer", "").strip())
# Only mark failed if synthesizer did not produce output; otherwise
# mark completed-with-degraded-evidence so users get the partial answer.
terminal_status = "completed" if has_final_answer else "failed"
```

**Rationale**: The synthesizer is the terminal node. If it produced a `final_answer`, the user has *something* actionable — even if based on incomplete research. Marking this as `status=failed` causes downstream consumers (UI, API) to hide the answer entirely. A `status=completed` with `error` populated in metadata lets the UI show the answer with a warning banner about degraded evidence.

**Citation**: `consultants/server/runner.py:173-174`

---

## Researcher (round 1)

Now I have the complete picture. Let me trace the exact failure path:

## Failure Path Analysis

### 1. CouncilState's Additive Reducers (`consultants/engine/graph.py:64-72`)

The `CouncilState` TypedDict declares these additive reducers:
- `research: Annotated[list, operator.add]` — list concatenation
- `turns: Annotated[list, operator.add]` — list concatenation  
- `research_rounds_used: Annotated[int, operator.add]` — integer sum
- `total_prompt_tokens: Annotated[int, operator.add]` — integer sum
- `total_completion_tokens: Annotated[int, operator.add]` — integer sum

Critically, `error` and `_role_failed` are **NOT** annotated with reducers (`consultants/engine/graph.py:74-75`), meaning they use **plain overwrite semantics**. When parallel lanes merge, whichever lane's state update is processed last determines the final value.

### 2. Exception Capture in `researcher_node` (`consultants/engine/council.py:555-571`)

When `agent_loop.runner.run_loop` raises an unhandled exception (HTTP timeout exhausting retry budget, or `tool_executor` returning non-string):

```python
# consultants/engine/council.py:555-571
except Exception as e:
    log.exception("researcher_node failed: %s", e)
    err_text = f"(researcher lane failed: {e})"
    return {
        "error": f"researcher failed: {e}",
        "_role_failed": "researcher",
        "research": [err_text],
        "research_rounds_used": 1,
        "turns": [RoleTurn(...)],
    }
```

The exception is **caught and converted to a tombstone**. The lane contributes `(researcher lane failed: ...)` to the additive `research` list, but the `error`/`_role_failed` fields may be overwritten by subsequent successful lanes.

### 3. `compiled.stream` Loop (`consultants/server/runner.py:137-161`)

The try/except at `consultants/server/runner.py:155-161` only fires if an exception **escapes** the graph execution. Since `researcher_node` catches exceptions internally, parallel lane failures do **NOT** trigger this block. The stream completes normally.

### 4. Synthesizer's Input Messages (`consultants/engine/council.py:718-723`)

The synthesizer receives:
```python
state.get("research") or []  # Includes tombstone entries from failed lanes
```

If lane 0 failed but lane 1 succeeded, `research` = `["(researcher lane failed: HTTP timeout...)", "Findings from lane 1..."]`. The synthesizer sees both but has **no structured indication** that lane 0's plan items were never investigated. It synthesizes from partial evidence.

### 5. Storage Artifact Writer (`consultants/engine/storage.py:199-207`)

The `ConsultationResult` is built with:
- `status=terminal_status` — determined by `final_state.get("error")` at `consultants/server/runner.py:175-176`
- `final_answer=final_state.get("final_answer", "")` — whatever the synthesizer produced

Since `error` uses overwrite semantics, if the **last** researcher lane succeeded, `final_state["error"]` is `None`, yielding `status="completed"`.

---

## Answer: Degraded Answer, Not Full Failure

**The council produces a confidently-degraded answer with `status="completed"`.** The user perceives:

1. A coherent final answer in `summary.md` (`consultants/engine/storage.py:119`)
2. `status: "completed"` in `metadata.json` (`consultants/engine/storage.py:168-169`)
3. **No visible indication** that some plan items were never researched
4. The transcript shows tombstone entries like `"(researcher lane failed: HTTP timeout...)"` but these are buried in `transcript.md` (`consultants/engine/storage.py:129-158`)

The failure mode is **silent evidence loss**: the synthesizer writes a decisive answer based on 2/3 lanes of research, unaware that lane 1's plan items were never grounded.

---

## Recommended Hardening Change

**Wrap the fallback summary call in `researcher_node` with its own exception handler** to prevent any exception from escaping the node:

```python
# consultants/engine/council.py:613-639 — current code has the fallback
# chat call OUTSIDE the outer try/except, creating an escape path:
if not text.strip():
    # ... summary_msgs built ...
    try:
        follow_up = chat_client.chat({...})  # Can raise
        # ...
    except Exception as e:  # This catches it, but be explicit
```

**Hardening**: Add an outer try/except around the **entire** fallback block (lines 586-639), not just the inner `chat_client.chat()` call, to guarantee no exception escapes `researcher_node`:

```python
# consultants/engine/council.py:586 — wrap the entire fallback:
if not text.strip():
    try:  # NEW: outer fallback protection
        log.warning(...)
        loop_msgs = final.get("_loop_messages") or payload["messages"]
        # ... rest of fallback ...
    except Exception as fallback_e:
        log.exception("researcher fallback entirely failed: %s", fallback_e)
        text = f"(researcher lane failed and fallback failed: {fallback_e})"
```

This ensures the tombstone is **always** returned, never an uncaught exception that could bubble to `compiled.stream` and trigger `status="failed"`.

---

## Critic (round 3)

DECISION: needs_more_research

**Critical inconsistencies between research rounds:**

1. **Status determination contradiction**: Rounds 1-2 claim `status="failed"` when any researcher lane fails (citing `runner.py:174`). Round 3 claims `status="completed"` if the last lane succeeds due to overwrite semantics. These cannot both be true. Need to verify what SUCCESSFUL `researcher_node` returns include—do they explicitly set `error: None` or omit the field? If omitted, does LangGraph's state merge preserve the prior `error` value or clear it?

2. **Missing: successful lane return structure**. The researcher traced the exception path (`council.py:555-571`) but never showed what a successful `researcher_node` return dict looks like. Without this, the overwrite-semantics claim at `graph.py:74-75` is speculative.

3. **Hardening recommendation shifted**: Rounds 1-2 recommend changing `runner.py:173-174` status logic. Round 3 recommends wrapping the fallback block at `council.py:586`. These address different failure modes. Need to confirm which failure path actually escapes—the inner `run_loop` exception (already caught at `555-571`) or the fallback `chat_client.chat()` call.

**Specific gaps to fill:**
- `consultants/engine/council.py:500-550` — show the successful `researcher_node` return dict structure
- `consultants/server/runner.py:173-176` — confirm exact status logic with surrounding context (does it check `final_answer` presence?)
- `consultants/engine/graph.py:74-75` — verify LangGraph's default merge behavior for non-annotated fields when one update omits the key

---

## Synthesizer

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

---
