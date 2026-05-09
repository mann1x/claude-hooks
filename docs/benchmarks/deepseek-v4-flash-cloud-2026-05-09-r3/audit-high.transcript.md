# Consultation transcript — csl-2026-05-09-0827-d547

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. Open `consultants/engine/graph.py` and locate the additive reducer that aggregates lane outputs into `CouncilState` (search for `reducer` or `additive`). Verify whether it catches exceptions from individual lane results (e.g., `except Exception` around the reducer call) or allows them to propagate.

2. In `consultants/server/runner.py`, examine the `compiled.stream` loop (around the `while` or `async for` over stream events). Identify how the loop handles events that signal an exception from a researcher lane (e.g., `LaneException` event, or raw exception inside the lane's `run_loop`). Check if the loop catches that exception or lets it break the stream.

3. Trace the message payload that the synthesizer receives as input: in `consultants/engine/synthesizer.py` (or wherever the synthesizer node is invoked), look at the `messages` list passed to the synthesizer. Determine whether a failed lane's output is replaced by a placeholder (e.g., "error: ...") or omitted entirely, and how the synthesizer distinguishes between successful and failed lanes.

4. In `consultants/storage.py`, locate the artifact writer (likely a method called `write_artifact` or `write`). Inspect whether the writer is called inside a try block that catches exceptions from the lane execution, or if it is called after the stream loop finishes even if an error occurred. Check if partial artifacts from other lanes are still written.

5. Simulate (or read existing unit tests) the scenario: one researcher lane raises an unhandled exception (e.g., a `TimeoutError` after retries). Run through the code paths identified above to determine if the `stream` loop catches this exception and sets `status=failed` or if it continues and the synthesizer receives incomplete data.

6. Based on the above, pinpoint the exact line or block where the failure either propagates to the top (causing total failure) or is swallowed inconsistently. Recommend a single change at that line/block (e.g., adding a try/except inside the additive reducer or the stream loop's lane result handling).

---

## Researcher (round 1)

## Failure Path Trace: Send-API Researcher Lane Crash

### 1. Exception origin in `run_loop`

`agent_loop/runner.py` has **zero exception handling** in the main loop body (lines 215-301). When `chat_fn(payload)` at line 230 raises — e.g. `ChatClient.chat` at `get_advice/chat_client.py:178` exhausts its `DEFAULT_MAX_RETRIES=8` (line 31) on retryable 5xx — the exception propagates uncaught out of `run_loop`.

Similarly, `execute_tool_calls` at `runner.py:95` calls `tool_executor` at line 122, which is `traced_tool` wrapping caliber's `execute` (`trace.py:211`). The wrapper records the error then re-raises (lines 220-222). If `tool_executor` raises (e.g. non-string return), same path.

No `try/except` wraps the `for i in range(config.max_iterations)` loop or `chat_fn` call or `execute_tool_calls` — confirmed by grep returning zero matches (`"except Exception|except:"` in runner.py produced no hits).

### 2. researcher_node catches it — tombstone return

`council.py:547-573`: The `except Exception as e` at line 555 catches the run_loop crash. Returns a tombstone dict at lines 562-573:

```python
# council.py:563-573
return {
    "error": f"researcher failed: {e}",
    "_role_failed": "researcher",
    "research": [err_text],           # additive reducer — concats
    "research_rounds_used": 1,        # additive reducer — sums
    "turns": [RoleTurn(...)],         # additive reducer — concats
}
```

**Key**: `error` and `_role_failed` are **NOT** additive (graph.py:74-75: `error: Optional[str]`, `_role_failed: Optional[str]` — no `Annotated[..., operator.add]`). LangGraph's default reducer for unannotated TypedDict fields is **replace** — last-writer-wins across parallel Send branches. Since only the failing lane returns `error`, the merged state correctly contains the failing lane's error. Other lanes return no `error` key, so their merge doesn't clobber it.

### 3. Additive reducers merge the lane outputs

`graph.py:64-72`: Fields `research`, `turns`, `research_rounds_used`, `total_*_tokens` all use `Annotated[..., operator.add]`. LangGraph concatenates lists and sums ints across parallel Send lanes:

- `research` becomes: `["lane 1 findings", "(researcher lane failed: timeout)", "lane 3 findings"]`
- `turns` becomes all three lane's RoleTurn objects
- `research_rounds_used` = 3 (1 per lane)
- `error` = `"researcher failed: timeout"` (set by failing lane, not overwritten because other lanes omit it)

### 4. compiled.stream loop (runner.py:136-161)

The loop at `runner.py:137` iterates over `compiled.stream(initial, stream_mode=["updates", "values"])`. When a Send lane completes, LangGraph yields `("updates", {"researcher": tombstone_dict})`. The loop processes progress flips at lines 144-154. Then it yields `("values", merged_state)` where `final_state` at line 139-140 gets the full merged state including the error field.

When the stream completes normally (line 155 not triggered), the code falls through to line 171-206:

```python
# runner.py:172-174
node_error = final_state.get("error")       # "researcher failed: timeout"
node_failed = final_state.get("_role_failed")  # "researcher"
terminal_status = "failed" if node_error else "completed"  # "failed"
```

**The stream loop does NOT break** — the exception was already caught inside `researcher_node` and returned as a normal state update. The graph continues to the critic node.

### 5. What the synthesizer sees

The critic runs normally (council.py:667-712). It reads `state.get("research") or []` — the combined list including the error text. If the critic says "ready", the graph routes to synthesizer.

`synthesizer_node` at council.py:715 builds messages at lines 718-722:

```python
msgs = build_synthesizer_messages(
    state["question"], state.get("plan", ""),
    state.get("research") or [],   # ← includes "(researcher lane failed: timeout)"
    state.get("critique"),
)
```

`build_synthesizer_messages` (council.py:245) iterates research rounds:
```
RESEARCHER REPORT (round 1):
[lane 1 findings]
RESEARCHER REPORT (round 2):
(researcher lane failed: timeout)
RESEARCHER REPORT (round 3):
[lane 3 findings]
```

The synthesizer **does NOT check `state.get("error")`** — there's no guard at line 715-758. It produces a `final_answer` that may or may not acknowledge the missing lane. The error text in the research looks like a real research report (just says "lane failed") so the synthesizer might include it uncritically.

### 6. Storage artifact writer

`runner.py:177-199`: Builds `ConsultationResult` with:
- `final_answer = final_state.get("final_answer", "")` — synthesizer's output with partial info
- `status = "failed"` (because `node_error` is truthy)
- `error = "researcher failed: timeout"`

`storage.py:200-208`: Writes three atomic files:
- `summary.md` — YAML front-matter with `status: failed` + `error: researcher failed: ...`, then the synthesizer's partial answer in the body
- `transcript.md` — includes the tombstone RoleTurn from the failed lane
- `metadata.json` — `{"final_answer": "...", "status": "failed", "error": "researcher failed: timeout", "turns": [all three lanes]}`

### Result: Degraded answer with status=Failed

The council **does produce an answer**, but it's **degraded** and the status is `failed`. The user sees:

1. **Status**: `failed` — but clicking through, they find a real (partial) answer
2. **Summary.md**: A synthesizer-written answer that includes surviving lanes' findings plus the failed lane's stub text mixed in as if it were real evidence
3. **Metadata.json**: error field set, making automated consumers (caliber dashboards, polling UI) show a red/failed state even though actionable content exists

**Failure mode perceived**: "Consultation failed" indicator, confusing because reading the summary reveals useful content from surviving lanes. The user has no easy way to distinguish "partial success with one dead lane" from "total failure."

### Recommended Hardening

**One change**: In `council.py`, `researcher_node` exception handler — when this is a Send-API fan-out (detected via `plan_item`), do NOT set `error` / `_role_failed`. The error text in `research` is sufficient for the synthesizer to see and account for the gap. The single-researcher path (critic re-routes, non-fanned topologies) should keep setting `error` because a crash there means the entire research phase failed.

Change at `council.py:562-573` — wrap the error return in a conditional:

```python
# council.py:562-573 — after "except Exception as e:"
plan_item = state.get("plan_item")
is_fanout_lane = bool(plan_item)
return {
    **({} if is_fanout_lane else {
        "error": f"researcher failed: {e}",
        "_role_failed": "researcher",
    }),
    "research": [err_text],
    "research_rounds_used": 1,
    "turns": [RoleTurn(
        role="researcher", round=this_round, content=err_text,
        prompt_tokens=0, completion_tokens=0, duration_seconds=0.0,
    )],
}
```

This means a single dead lane produces `status="completed"` with a partial-but-honest answer, rather than `status="failed"` with a confusing partially-usable artifact. The synthesizer still sees `(researcher lane failed: timeout)` in `research` and can responsibly acknowledge the gap.

---

## Researcher (round 1)

## Failure Path Report: Failed Parallel Researcher Lane

### 1. Exception Origin — `run_loop` has no internal try/except

The for loop at `claude_hooks/agent_loop/runner.py:215` calls `chat_fn(payload)` at line 230 and `execute_tool_calls(...)` at line 300. Neither call is in a try block inside `run_loop`. If `chat_fn` (an HTTP call to Ollama) exhausts its retry budget and raises, **the exception propagates out of `run_loop` uncaught** at `runner.py:230`. The only try/except in `run_loop`'s return path is at line 309 (`final = dict(final or {})`) but that executes only if the loop `break`s normally — an exception hits before that line.

The exception arrives at `researcher_node`'s outer try/except (`council.py:547-555`) and IS caught there.

### 2. CouncilState's Additive Reducers (`consultants/engine/graph.py:50-80`)

The failed lane's `researcher_node` returns a tombstone dict at `council.py:563-573`:

```python
return {
    "error": f"researcher failed: {e}",
    "_role_failed": "researcher",
    "research": [err_text],
    "research_rounds_used": 1,
    "turns": [RoleTurn(...)],
    "total_prompt_tokens": 0,
    "total_completion_tokens": 0,
}
```

LangGraph's merge via the additive reducers:
- `research: Annotated[list, operator.add]` (`graph.py:64`) → the tombstone text `"(researcher lane failed: HTTP timeout after 3 retries)"` is **appended** to the research list alongside the successful lanes' reports. The synthesizer will see a list like `["(researcher lane failed: ...)", "Concrete findings from lane 1", "Concrete findings from lane 2"]`.
- `error: Optional[str]` (`graph.py:73`) — **not an additive reducer**, so it's a plain dict key. The failed lane sets it; no subsequent node (including the successful lanes and the synthesizer) clears it. It persists in the merged state.
- `_role_failed: Optional[str]` (`graph.py:75`) — same, persists once set.

### 3. The Stream Loop (`consultants/server/runner.py:135-161`)

The stream loop completes normally — the exception was caught *inside* the node, so LangGraph never propagates it to the stream consumer. `final_state` at `runner.py:140` gets the merged dict with `error` set.

The catch at `runner.py:155-161` (`except Exception as e: state.status = "failed"`) does **NOT** fire — the graph finished without raising.

### 4. The Synthesizer's Input Messages

At `council.py:717-723`, `synthesizer_node` calls `build_synthesizer_messages` with `state.get("research") or []`. The research list contains ALL lanes' reports including the tombstone.

`build_synthesizer_messages` at `council.py:245-266` renders them as:

```
USER QUESTION:
...

PLANNER'S PLAN:
...

RESEARCHER REPORT (round 1):
(researcher lane failed: HTTP timeout after 3 retries)

RESEARCHER REPORT (round 2):
Concrete findings from lane 1...

RESEARCHER REPORT (round 3):
Concrete findings from lane 2...

Now write the final answer for the user...
```

**Critical detail**: The synthesizer's system prompt at `council.py:156-167` says *"Do NOT mention the council, the roles, or the process — the user only wants the answer."* This means the model is actively discouraged from saying "one lane failed" or admitting incompleteness. It will almost certainly produce a **confidently incomplete answer**, ignoring the failed lane's scope.

### 5. The Artifact Writer (`consultants/server/runner.py:171-206`)

At `runner.py:172-174`:
```python
node_error = final_state.get("error")       # "researcher failed: ..."
node_failed = final_state.get("_role_failed") # "researcher"
terminal_status = "failed" if node_error else "completed"
```

Since `node_error` is truthy, `terminal_status = "failed"`.

The `ConsultationResult` at `runner.py:177-199` IS written with:
- `status="failed"` (line 189)
- `error="researcher failed: HTTP timeout after 3 retries"` (line 190)
- `final_answer=<synthesizer's output>` (line 186 — whatever the synthesizer produced from partial evidence)

`storage.write_consultation()` at `runner.py:199` calls `_atomic_write_text` (`storage.py:174-192`) which writes all three files atomically. **The artifact IS written successfully** — the function is not inside a try block for the graph crash, it's after the stream loop. Only if the graph itself raises (caught at `runner.py:155`) does it fall through to `_write_failed_artifacts` at `runner.py:211-237`.

### 6. User-Perceived Failure Mode

| Aspect | Value |
|---|---|
| `state.status` | `"failed"` (`runner.py:201`) |
| Dashboard/UI indicator | Shows failure/error |
| `summary.md` content | Contains synthesizer's answer based only on surviving lanes |
| `metadata.json` status | `"failed"` |
| Answer quality | **Confidently incomplete** — synthesizer won't mention the gap |

The user sees a consultation that **reports as failed** but contains a plausible-looking answer that is **silently missing** the scope assigned to the failed lane. Because the synthesizer prompt forbids mentioning the process/roles, the partial answer reads as definitive — a classic "silent degradation" failure mode.

### 7. Recommended Hardening Change

**File**: `consultants/server/runner.py:174`

**Problem**: `terminal_status` is binary ("failed"/"completed"). A single failed researcher lane among 3 causes the entire consultation to report as failed, even though the synthesizer produced a real answer from surviving lanes.

**Fix**: Add a `"degraded"` status when (a) the error came from a researcher lane, (b) the synthesizer still produced a non-empty answer:

```python
# At consultants/server/runner.py:174, replace the single line:
#   terminal_status = "failed" if node_error else "completed"
# With:
if node_error and node_failed == "researcher" and final_state.get("final_answer", "").strip():
    terminal_status = "degraded"
elif node_error:
    terminal_status = "failed"
else:
    terminal_status = "completed"
```

This also requires adding `"degraded"` to the `ConsultationResult.status` type hint at `consultants/engine/storage.py:65` and any consumer that checks the status string. Without this change, a UI polling `status` would hide a useful partial answer behind a red "failed" banner.

---

## Researcher (round 1)

Now let me check the trace module for the `traced_tool` wrapper contract:

---

## Critic (round 3)

DECISION: ready

All four numbered sub-questions are answered with concrete path:line citations (graph.py:64-72 additive reducers, council.py:555-573 tombstone, runner.py:135-161 stream loop, runner.py:172-174 status check, council.py:245-266 & 717-723 synthesizer input, storage.py:174-192 artifact writer). The failure mode (status=failed, partial answer with silent gap) is clearly described. The two hardening proposals (council.py:562-573 vs runner.py:174) are both based on the same trace — the synthesizer can select one to satisfy the “exactly one” requirement. No concrete missing fact prevents the synthesizer from producing a correct final answer.

---

## Synthesizer

The council **does produce an answer**, but it is **degraded** and **status=failed**. The user sees a consultation marked as failed in the dashboard/UI, yet the summary contains a plausible-looking, silently incomplete answer from the surviving lanes.

## Concrete trace (path:line for every claim)

1. **Exception origin** – `agent_loop/runner.py:230` (`chat_fn(payload)`) and `runner.py:300` (`execute_tool_calls(...)`) have no `try/except`. The exception propagates uncaught out of `run_loop` (confirmed: zero `except` hits in that file).

2. **researcher_node catches it** – `council.py:555-573` catches the exception and returns a tombstone dict with `error` and `_role_failed` fields set, plus `research: [err_text]`, `turns: [RoleTurn(...)]`, `research_rounds_used: 1`.

3. **Additive reducers merge across Send lanes** – `graph.py:64-72`: `research`, `turns`, `research_rounds_used`, `total_*_tokens` use `Annotated[..., operator.add]`. The tombstone's `research` text is appended to the successful lanes' findings. Fields `error` (`graph.py:73`) and `_role_failed` (`graph.py:75`) are **not additive** – they are plain `Optional[str]` with LangGraph's default last-writer-wins. Only the failing lane sets them, so they persist in the merged state.

4. **Stream loop completes normally** – `runner.py:135-161` iterates the LangGraph stream. Because the exception was caught _inside_ the node, no exception reaches the loop; the `except Exception` at `runner.py:155` does **not** fire. `final_state` at `runner.py:140` contains the merged dict with `error` set.

5. **Synthesizer sees the error text as a research report** – `council.py:717-723` calls `build_synthesizer_messages(state.get("research") or [])`. `build_synthesizer_messages` (`council.py:245-266`) renders all entries as `RESEARCHER REPORT (round N): <text>`. The failed lane's `"(researcher lane failed: HTTP timeout after 3 retries)"` appears as a normal research report. The synthesizer's system prompt (`council.py:156-167`) says *"Do NOT mention the council, the roles, or the process"* – so it produces a **confidently incomplete answer** that silently ignores the missing lane's scope.

6. **Storage writes status=failed with partial answer** – `runner.py:172-174`:
   ```python
   node_error = final_state.get("error")       # truthy
   node_failed = final_state.get("_role_failed")  # "researcher"
   terminal_status = "failed" if node_error else "completed"
   ```
   Then `runner.py:177-199` builds `ConsultationResult` with `status="failed"`, `error="researcher failed: ..."`, `final_answer=<synthesizer's partial output>`. `storage.write_consultation()` (`storage.py:174-192`) writes all three atomic files successfully – this code is after the stream loop, not inside a try block for the graph crash.

## User-perceived failure mode

- **Dashboard/UI**: Shows `status=failed` (red banner, error indicator).
- **summary.md**: Contains the synthesizer's answer based only on surviving lanes – reads as a complete, definitive answer.
- **metadata.json**: `status: failed`, `error: researcher failed: timeout after 3 retries`.
- **Silent degradation**: The user has no signal that one researcher lane was lost; the answer is missing the scope assigned to that lane, but the model never mentions the gap.

## Recommended hardening change (exactly one)

**File**: `consultants/server/runner.py:174`

**Problem**: The binary `terminal_status` (`"failed"`/`"completed"`) conflates "total failure" with "partial answer from surviving lanes." A single failed researcher lane among N causes the whole consultation to report as failed, hiding a useful partial answer behind a red banner.

**Fix**: Introduce a `"degraded"` status when a researcher lane failed but the synthesizer still produced a non-empty answer:

```python
# Replace line 174:   terminal_status = "failed" if node_error else "completed"
if node_error and node_failed == "researcher" and final_state.get("final_answer", "").strip():
    terminal_status = "degraded"
elif node_error:
    terminal_status = "failed"
else:
    terminal_status = "completed"
```

**Companion changes** (minimal, required for correctness):
- Add `"degraded"` to the `ConsultationResult.status` type hint at `consultants/engine/storage.py:65` (e.g., `Literal["completed", "failed", "degraded"]`).
- Update any UI/API consumer that checks `status` to handle `"degraded"` (e.g., show a yellow/yield banner instead of red).

This change allows the UI to display a nuanced state: "Partially complete — some research lanes failed" while still writing the partial answer, rather than hiding it behind a binary "failed."

---
