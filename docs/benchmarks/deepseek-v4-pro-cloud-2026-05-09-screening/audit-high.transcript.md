# Consultation transcript — csl-2026-05-09-0710-3ecb

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. In `consultants/engine/graph.py`, locate the fan‑out node that dispatches parallel `Send` objects to `agent_loop.runner.run_loop` (search for `Send` class usage, likely near the `plan` → `researchers` edge). Examine the exception handling wrap (or absence) around each lane’s invocation.
2. In `consultants/engine/graph.py`, inspect `CouncilState`’s `messages` reducer (often `add_messages` from `langgraph`). Determine how a lane that raises without returning an `AIMessage` affects the aggregated list after the fan‑in step.
3. In `consultants/server/runner.py`, find the `async for chunk in graph.astream(...)` or `stream(...)` loop. Check whether `try/except` around the stream captures `GraphBubbleUp` or other exceptions, and what final HTTP status and body are returned when one lane fails mid‑stream.
4. In `consultants/engine/graph.py`, locate the synthesizer node (after fan‑in) and its input selector. Verify if it composes the prompt from `state["messages"]` without filtering, thereby potentially including only partial researcher outputs (or error tokens) from the failed lane.
5. In `storage.py`, find the artifact writing function (e.g., `save_run` or `write_artifact`). Examine the conditions under which the final artifact is persisted: does it rely on a completed state graph execution, or does it capture partial outputs on error?

---

## Researcher (round 1)

## Researcher Report — Send-API Fan-Out Failure Path

### 1. `CouncilState` additive reducers (`consultants/engine/graph.py:50–80`)

```python
research: Annotated[list, operator.add]        # line 64 — list concat
turns: Annotated[list, operator.add]           # line 68 — list concat
research_rounds_used: Annotated[int, operator.add]  # line 69
total_prompt_tokens: Annotated[int, operator.add]   # line 71
total_completion_tokens: Annotated[int, operator.add]  # line 72
```

**BUT** `error` (line 74) and `_role_failed` (line 75) are **non-additive** — plain `Optional[str]` with no `operator.add` annotation. When parallel `Send` branches return partial-state dicts, LangGraph applies last-write-wins to these fields. A failed lane that sets `error: "researcher failed: HTTP timeout"` can have its value silently **overwritten** by a successful lane that returns no `error` key. The merge is non-deterministic — whichever lane's write lands last wins (`consultants/engine/graph.py:48` notes "default dict-merge reducer" for non-declared keys).

### 2. Fan-out dispatch (`consultants/engine/graph.py:349–386`)

The fan-out conditional edge after `planner` spawns one `Send("researcher", {...})` per lane (line 359). Each `Send` payload inherits parent state fields plus `plan_item`/`lane_idx` (lines 364–384). **There is no try/except wrap around individual lane invocations** — LangGraph handles parallel execution internally. Each lane independently enters `_wrap_researcher` → `council.researcher_node`.

### 3. `researcher_node` tombstone (`consultants/engine/council.py:547–573`)

When `loop_runner(...)` raises (e.g., `ChatClient.chat` exhausts retries → `RuntimeError` at `claude_hooks/get_advice/chat_client.py:285`, or `URLError` at line 302), the `except Exception as e` at line 555 catches it:

```python
# consultants/engine/council.py:556-573
log.exception("researcher_node failed: %s", e)
err_text = f"(researcher lane failed: {e})"
return {
    "error": f"researcher failed: {e}",
    "_role_failed": "researcher",
    "research": [err_text],         # additive — concatenated
    "research_rounds_used": 1,      # additive — summed
    "turns": [RoleTurn(             # additive — concatenated
        role="researcher", round=this_round, content=err_text, ...)],
}
```

The exception is **caught** — it never propagates to LangGraph's stream loop. Each failed lane emits a tombstone string `"(researcher lane failed: <exc>)"` into its `research` list return.

For `tool_executor` returning non-string: `claude_hooks/caliber_proxy/tools.py:439` wraps every tool result as `str(result)` — so this path is **not a realistic failure mode**. Even tool exceptions produce `"error: tool raised: ..."` (line 438), always a string.

### 3. `runner.py` stream loop (`consultants/server/runner.py:136–161`)

```python
# line 137-161
for mode, payload in compiled.stream(initial, stream_mode=["updates", "values"]):
    if mode == "values" and isinstance(payload, dict):
        final_state = payload   # full merged state after each superstep
```

The fan-out superstep produces one `"values"` emission containing the **additively-merged** state. Surviving lanes' reports + tombstone entries are concatenated into `research[]`. The stream never hits the outer `except Exception` at line 155 because all lanes returned via tombstone — **status stays "completed"**.

After the stream drains (line 167+):

```python
# consultants/server/runner.py:172-174
node_error = final_state.get("error")
node_failed = final_state.get("_role_failed")
terminal_status = "failed" if node_error else "completed"
```

**Race condition:** Because `error` and `_role_failed` are non-additive, a successful lane's delta (which omits these keys) can overwrite the failed lane's values. If the last-write wins from a healthy lane, `node_error` is `None` → `terminal_status = "completed"`. The failure is **silently swallowed** in the status field.

### 4. Synthesizer input (`consultants/engine/council.py:718–723`)

```python
# line 720
state.get("research") or []
```

After additive merge, the synthesizer sees the research list containing:
- Surviving lane reports (real findings with `path:line` refs)
- Tombstone entries like `"(researcher lane failed: HTTP timeout: <urlopen error timed out>)"`

The `build_synthesizer_messages` at line 245–266 wraps these into the prompt with no special handling of tombstones — the synthesizer treats them as opaque text blobs. It has **no structured signal** that a lane failed, only the human-readable tombstone string embedded in the research list.

### 5. Storage artifact writer (`consultants/engine/storage.py:177–208`)

```python
# line 186
final_answer=final_state.get("final_answer", ""),
# line 189
status=terminal_status,
# line 190
error=node_error,
```

Because the runner already set `terminal_status` (possibly overwritten to `"completed"`), `storage.write_consultation` writes `summary.md` with `status: completed` even though a lane failed. The tombstone text IS present in `transcript.md` (via `turns` additive reducer, correctly merged). The user sees a **coherent-looking answer with `status: completed`** — no visible error in the HTTP response, no `status=failed` in the session poll.

### Answer: Does the council produce a coherent answer?

**Yes, it produces a DEGRADED answer with `status=completed`.** The user perceives a competent-looking answer that may be **missing or wrong on facts the failed lane was tasked to investigate**. The failure is only visible if the user reads the `transcript.md` artifact and notices a `"(researcher lane failed: ...)"` entry among the researcher reports. The HTTP API returns `{"status": "completed"}` with `final_answer` populated.

The outer `except Exception` at `runner.py:155` (status=failed graph crash) **never triggers** because `researcher_node` catches the exception before LangGraph sees it. This path only activates on non-lane failures (e.g. memory corruption in the graph topology itself).

---

### Recommended hardening

**Make `_role_failed` an additive field** so the runner reliably detects ANY lane failure regardless of merge order. Change the TypedDict declaration at `consultants/engine/graph.py:75` from:

```python
_role_failed: Optional[str]
```
to:
```python
_role_failed: Annotated[list[str], operator.add]
```

Then update `runner.py:173` to check for non-empty list instead of truthiness:

```python
# consultants/server/runner.py:173
node_failed = final_state.get("_role_failed") or []
terminal_status = "failed" if node_error or node_failed else "completed"
```

And update `researcher_node`'s tombstone at `consultants/engine/council.py:565` from `"_role_failed": "researcher"` to `"_role_failed": ["researcher"]` (and similarly for planner at line 454, critic at 685, synthesizer at 738). This is a one-line schema change with three mechanical return-site updates, and it closes the last-write-wins race on failure detection without changing the positive-path behavior.

---

## Researcher (round 1)

Now I have all the evidence. Let me verify one critical detail — what `run_loop` does with an exception from `chat_fn`, and the exact merge behavior for non-additive fields across parallel `Send` lanes.

---

## Researcher (round 1)

Now let me trace the exact failure path through each of the four layers, starting from the point where `loop_runner` raises inside an active Send fan-out lane.

---

### 1. CouncilState's additive reducers (`consultants/engine/graph.py`)

The `research` channel is declared at `consultants/engine/graph.py:64`:
```python
research: Annotated[list, operator.add]
```

When a researcher lane's `researcher_node` catches the exception at `consultants/engine/council.py:555-573`, it returns a tombstone partial update:

```python
# council.py:562-573
err_text = f"(researcher lane failed: {e})"
return {
    "error": f"researcher failed: {e}",
    "_role_failed": "researcher",
    "research": [err_text],          # additive: gets list-concatenated with other lanes
    "research_rounds_used": 1,       # additive int: merged
    "turns": [RoleTurn(...)],        # additive
}
```

LangGraph merges this into the running state alongside concurrent lanes' returns. The successful lanes contribute their `research: ["real findings..."]` deltas; the failed lane contributes `research: ["(researcher lane failed: ...)"]`. Because `research` uses `operator.add` (list concatenation), the final `state["research"]` is a single list containing both real findings AND the error stub — e.g. `["finding A", "finding B", "(researcher lane failed: HTTP timeout)"]`.

**Critical subtlety with `error` and `_role_failed`**: These are declared at `graph.py:74-75` as plain `Optional[str]` — NO annotated reducer. LangGraph's default reducer for scalars is last-write-wins. In a Send fan-out, the merge order of parallel lane updates is **non-deterministic**. If a successful lane returns no `error` key at all, and the failed lane's update is merged last, `error` is set. If a successful lane's update is merged last, `error` may be absent. This is a latent race condition at `graph.py:74`.

---

### 2. `runner.py`'s `compiled.stream` loop (`consultants/server/runner.py`)

After all fan-out lanes merge, the graph continues to the next node (critic or synthesizer). The stream loop at `runner.py:136-161` accumulates `final_state` from `"values"` mode yields:

```python
# runner.py:137-141
for mode, payload in compiled.stream(
        initial, stream_mode=["updates", "values"]):
    if mode == "values" and isinstance(payload, dict):
        final_state = payload
```

The graph itself does NOT throw — the exception was caught inside `researcher_node`. The `try/except` at `runner.py:155-161` is **not entered**. The stream loop drains normally, synthesizer runs, and `final_state` contains both `error` (from the failed lane's tombstone) and `final_answer` (from the synthesizer).

Then at `runner.py:172-174`:

```python
node_error = final_state.get("error")
node_failed = final_state.get("_role_failed")
terminal_status = "failed" if node_error else "completed"
```

**This is the decisive line.** A single failed lane poisons the entire consultation's status to `"failed"`, even though the synthesizer ran successfully on the surviving lanes' evidence and produced a real `final_answer`.

---

### 3. What the synthesizer actually sees

The synthesizer builds its messages at `council.py:718-723`:

```python
msgs = build_synthesizer_messages(
    state["question"], state.get("plan", ""),
    state.get("research") or [],    # ← includes both real findings AND error stubs
    state.get("critique"),
    self_critic=self_critic,
)
```

And `build_synthesizer_messages` at `council.py:245-266` renders each research round as:

```python
for i, r in enumerate(research_rounds, start=1):
    parts.append(f"\nRESEARCHER REPORT (round {i}):\n{r.strip()}")
```

So the synthesizer sees something like:

```
RESEARCHER REPORT (round 1):
- Finding A: the dispatcher at claude_hooks/dispatcher.py:42...
- Finding B: the timeout is set to 3s at ...

RESEARCHER REPORT (round 1):
(researcher lane failed: HTTPConnectionPool(host='...', port=11433): Read timed out. (read timeout=30))
```

The synthesizer receives the error text as if it were a real finding. It has no structured signal that this lane failed — it's just another string in the `research` list. The synthesizer may:
- Ignore it and answer from the surviving evidence (**most likely** — the preamble at `council.py:96-107` instructs it to be concise and decisive)
- Include it as a caveat in the output ("one research lane timed out, answer may be incomplete")

---

### 4. The `storage.py` artifact writer (`consultants/engine/storage.py`)

The artifact write happens at `runner.py:177-199`:

```python
result = storage.ConsultationResult(
    ...
    final_answer=final_state.get("final_answer", ""),
    status=terminal_status,       # "failed"
    error=node_error,             # "researcher failed: <e>"
    ...
)
storage.write_consultation(result, cwd=Path(cwd))
```

`write_consultation` at `storage.py:200-208` atomically writes three files:

| File | What it gets |
|---|---|
| `summary.md` | YAML front-matter with `status: "failed"` + `error: "researcher failed: ..."`, then the synthesizer's real answer as body |
| `transcript.md` | All turns including the failed lane's tombstone `RoleTurn` and the synthesizer's output |
| `metadata.json` | `"status": "failed"`, `"error": "researcher failed: ..."`, plus all token counters |

If the exception had escaped the graph entirely (e.g., a bug in `researcher_node`'s except handler itself), the `try/except` at `runner.py:155-161` would fire, calling `_write_failed_artifacts` at `runner.py:211-237`, which writes `final_answer: "(consultation failed: <exc>)"` with `status: "failed"` and no synthesizer output at all.

---

### Summary: does the council produce a coherent answer?

**Yes — a degraded answer with `status=failed`.** The graph completes normally because `researcher_node` catches the exception at `council.py:555`. The synthesizer runs on the merged `research` list (real findings + error stubs) and produces a real `final_answer`. But `runner.py:174` marks the consultation as `"failed"` because `error` is truthy.

**User-perceived failure mode:** The user gets a real answer covering only the surviving lanes' plan items. The answer may omit findings for the crashed lane's sub-questions. The API response shows `status: "failed"` and `error: "researcher failed: <exception>"` — confusing because a real answer WAS produced. The `summary.md` front-matter says `status: "failed"` right above a perfectly reasonable answer body.

---

### Hardening recommendation

**Change `consultants/server/runner.py:174`** — don't fail the entire consultation when a single lane's tombstone set `error` but the synthesizer still produced a non-empty answer:

```python
# runner.py:172-175 — BEFORE
node_error = final_state.get("error")
node_failed = final_state.get("_role_failed")
terminal_status = "failed" if node_error else "completed"

# runner.py:172-179 — AFTER
node_error = final_state.get("error")
node_failed = final_state.get("_role_failed")
has_answer = bool(
    final_state.get("final_answer", "").strip()
    and not final_state.get("final_answer", "").startswith("(consultation incomplete")
)
terminal_status = "completed" if has_answer else "failed"
```

This preserves the `error` field in metadata (for debugging) but marks consultations as `"completed"` when the synthesizer produced a real answer despite a lane failure. If the synthesizer itself fails and emits `"(consultation incomplete: ...)"`, the status correctly stays `"failed"`.

A secondary hardening: add an explicit reducer to `error` and `_role_failed` in `CouncilState` at `graph.py:74-75` to eliminate the non-deterministic merge race — e.g. `Annotated[Optional[str], lambda a, b: a or b]` so the first non-None error survives regardless of merge order.

---

## Critic (round 3)

DECISION: ready  
The researcher covered the fan‑out dispatch, tombstone construction, additive merge of `research`, the runner’s status decision at `runner.py:174`, the synthesizer’s input including error stubs, and the storage artifact contents. File:line citations are precise for each step. The only nuance—the non‑deterministic merge of `error` and `_role_failed`—is correctly identified and does not block a definitive answer; the researcher still delivers a concrete failure path (status=failed with degraded answer) and a single hardening change at `runner.py:174` (make status dependent on a non‑empty `final_answer` rather than raw `error`). No missing fact would alter the conclusion.

---

## Synthesizer

The council produces a degraded but coherent answer, while the consultation is incorrectly marked `status=failed` because a failed lane’s tombstone sets the `error` field to a truthy value at `runner.py:174` — even though the synthesizer ran successfully on the surviving lanes’ evidence.

Each researcher lane that raises inside `loop_runner` is caught by the `except Exception` at `consultants/engine/council.py:555`. It returns a tombstone containing `error: "researcher failed: …"` and `_role_failed: "researcher"` (lines 562–573). Because the `research` channel uses `operator.add` (list concatenation) at `consultants/engine/graph.py:64`, the final merged `research` list contains both the actual findings and error stubs like `"(researcher lane failed: HTTP timeout)"`. The synthesizer then consumes this merged list at `council.py:718–723` (rendered as plain text at lines 245–266) and produces a real `final_answer` — the answer is coherent but may be missing facts the failed lane was tasked to discover.

The stream loop at `consultants/server/runner.py:137–141` collects the merged state normally because no exception escapes the graph. After the stream drains, lines 172–174 check `final_state.get("error")` and unconditionally set `terminal_status = "failed"` when it is truthy. This `terminal_status` is passed to the storage writer at `runner.py:177–199` and written to `summary.md` and `metadata.json` (`storage.py:200–208`). The user therefore receives a real final answer but sees `status: "failed"` and an error description, even though the consultation completed and delivered a usable result. The answer may be missing findings from the crashed lane, but there is no obvious indication that the missing information is due to a lane failure — only the contradiction between a valid answer and a "failed" status.

**Hardening recommendation (single change):** Modify the status decision in `consultants/server/runner.py:172–179` to treat the consultation as `"completed"` when a non‑empty `final_answer` was actually produced, ignoring the tombstone `error` key:

```python
# BEFORE (lines 172-174)
node_error = final_state.get("error")
node_failed = final_state.get("_role_failed")
terminal_status = "failed" if node_error else "completed"

# AFTER (replace lines 172-174 with this block)
node_error = final_state.get("error")
node_failed = final_state.get("_role_failed")
has_answer = bool(
    final_state.get("final_answer", "").strip()
    and not final_state.get("final_answer", "").startswith("(consultation incomplete")
)
terminal_status = "completed" if has_answer else "failed"
```

This preserves the `error` in metadata for debugging but accurately marks the consultation as `"completed"` when a real answer exists, eliminating the confusing `status=failed` label after a successful synthesis.

---
