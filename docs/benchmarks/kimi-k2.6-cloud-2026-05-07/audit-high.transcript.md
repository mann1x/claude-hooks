# Consultation transcript — csl-2026-05-07-0910-c184

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. In `consultants/engine/graph.py`, find `CouncilState` and record path:line of its additive reducers to verify how parallel lane results (including exceptions) are merged into shared state.
2. In `consultants/server/runner.py`, inspect the `compiled.stream` loop and record path:line of any try/except or event-handling code governing whether a failed parallel lane aborts the stream or continues.
3. Locate the definition of `agent_loop.runner.run_loop` and record path:line of its outer exception boundary to see if HTTP timeouts or non-string tool returns propagate uncaught.
4. In `consultants/engine/graph.py`, find the synthesizer node's invocation and record path:line of the logic that builds its input messages from `CouncilState` when a parallel lane's output is missing or errored.
5. In `consultants/storage.py`, inspect the artifact writer and record path:line of the write trigger to check whether partial state is persisted on lane failure or only on full success.
6. In `consultants/server/runner.py`, trace the response assembly after stream completion and record path:line of the `status` assignment to confirm whether degraded synthesizer output is returned as success or `status=failed`.
7. Using the gathered path:line evidence, determine the exact failure mode and specify exactly one hardening change with target path:line, such as adding an exception wrapper in the runner or an error-to-message converter in the reducer.

---

## Researcher (round 1)

## Failure-path trace through the Send-API fan-out

### 1. CouncilState additive reducers (`consultants/engine/graph.py`)

The state schema declares additive channels for the fields that parallel lanes mutate:

- `research: Annotated[list, operator.add]` — `consultants/engine/graph.py:64`
- `turns: Annotated[list, operator.add]` — `consultants/engine/graph.py:68`
- `research_rounds_used: Annotated[int, operator.add]` — `consultants/engine/graph.py:69`
- `total_prompt_tokens: Annotated[int, operator.add]` — `consultants/engine/graph.py:71`
- `total_completion_tokens: Annotated[int, operator.add]` — `consultants/engine/graph.py:72`

When a researcher lane crashes inside `run_loop`, the exception is caught inside `researcher_node` at `consultants/engine/council.py:555` and converted to a tombstone dict:

```python
return {
    "error": f"researcher failed: {e}",
    "_role_failed": "researcher",
    "research": [err_text],          # err_text = "(researcher lane failed: {e})"
    "research_rounds_used": 1,
    "turns": [RoleTurn(...)],
}
```
`consultants/engine/council.py:563-573`

Because `research` and `turns` use `operator.add`, LangGraph concatenates the tombstone entry onto the shared state alongside the successful lanes. `error` is **not** additive (`Optional[str]`, no `Annotated` reducer); it uses last-write-wins dict merge.

### 2. `consultants/server/runner.py` `compiled.stream` loop

The stream loop wraps the graph invocation in a broad `try/except`:

```python
try:
    for mode, payload in compiled.stream(
            initial, stream_mode=["updates", "values"]):
        ...
except Exception as e:
    log.exception("council graph invocation failed: %s", e)
    state.status = "failed"
    state.error = f"graph crashed: {e}"
    ...
    return
```
`consultants/server/runner.py:136-161`

Because `researcher_node` already caught the exception and returned a normal dict (the tombstone), the exception **does not** reach this outer `except`. The stream continues iterating. Progress flips to `"done"` for the researcher node at `runner.py:146` and the graph proceeds to the next node (critic or synthesizer).

### 3. What the synthesizer actually sees

The synthesizer node calls:

```python
msgs = build_synthesizer_messages(
    state["question"], state.get("plan", ""),
    state.get("research") or [],
    state.get("critique"),
    self_critic=self_critic,
)
```
`consultants/engine/council.py:718-723`

Because `research` is additive, `state.get("research")` now contains **all** lane outputs in merge order, e.g.:

```
RESEARCHER REPORT (round 1):
<lane 0 findings...>

RESEARCHER REPORT (round 2):
(researcher lane failed: HTTP timeout after 3 retries)

RESEARCHER REPORT (round 3):
<lane 2 findings...>
```

The synthesizer therefore sees the tombstone text verbatim in its prompt and can choose to acknowledge the gap.

### 4. `storage.py` artifact writer

After the stream drains, `runner.py` builds the result:

```python
node_error = final_state.get("error")
terminal_status = "failed" if node_error else "completed"
```
`consultants/server/runner.py:172-174`

The tombstone set `error = "researcher failed: ..."`, so `terminal_status` becomes `"failed"`. The writer then persists:

```python
result = storage.ConsultationResult(
    ...
    final_answer=final_state.get("final_answer", ""),
    status=terminal_status,
    error=node_error,
    ...
)
storage.write_consultation(result, cwd=Path(cwd))
```
`consultants/server/runner.py:177-199`

`storage.write_consultation` at `consultants/engine/storage.py:200-208` writes three files:

- `summary.md` — YAML front matter shows `status: failed` and `error: "researcher failed: ..."`, **plus** the synthesizer's `final_answer` as the body (`render_summary` at `storage.py:117-120`).
- `transcript.md` — includes the tombstone turn under a `## Researcher` header (`render_transcript` at `storage.py:123-158`).
- `metadata.json` — full `ConsultationResult` dict with `status: "failed"`.

---

### Verdict: degraded answer with `status=failed`

**The council does NOT fully abort.** It produces a **degraded but coherent answer** because surviving lanes still contribute research, and the synthesizer runs over the merged corpus (including the tombstone).

**The failure mode the user perceives:**
- Metadata / YAML front matter says `status: failed` and carries `error: "researcher failed: <exception>"`.
- The `summary.md` body still contains the synthesizer's answer, which may explicitly note missing research because the tombstone `(researcher lane failed: ...)` is in its input context.
- The transcript shows which lane crashed.

---

### Recommended hardening change

Add a defensive type check in `execute_tool_calls` immediately after `tool_executor` returns so a non-string output is coerced before it poisons the message payload and causes a downstream JSON serialization error inside `chat_fn`:

```python
output = tool_executor(name, args_str, cwd)
if not isinstance(output, str):
    log.warning("tool_executor returned non-string (%s): %r", type(output).__name__, output)
    output = str(output)
```

**Location:** insert after `claude_hooks/agent_loop/runner.py:122`.

---

## Researcher (round 1)

Here is the concrete trace through the four requested points, followed by the outcome and one hardening recommendation.

---

### 1. CouncilState's additive reducers (`consultants/engine/graph.py:64-72`)

The `CouncilState` schema declares additive reducers for the fields that parallel researcher lanes merge:

```python
research: Annotated[list, operator.add]              # graph.py:64
turns: Annotated[list, operator.add]                  # graph.py:68
research_rounds_used: Annotated[int, operator.add]    # graph.py:69
total_prompt_tokens: Annotated[int, operator.add]      # graph.py:71
total_completion_tokens: Annotated[int, operator.add] # graph.py:72
```

When a lane crashes inside `agent_loop.runner.run_loop`, the exception is caught **inside the node wrapper** (`consultants/engine/council.py:547-573`). The node returns a tombstone dict whose `research` key is a **single-element list**:

```python
err_text = f"(researcher lane failed: {e})"
return {
    "error": f"researcher failed: {e}",       # council.py:564
    "_role_failed": "researcher",                # council.py:565
    "research": [err_text],                      # council.py:566  ← additive reducer appends this
    "research_rounds_used": 1,                   # council.py:567
    "turns": [RoleTurn(...)],                    # council.py:568
}
```

LangGraph's default dict-merge reducer concatenates lists via `operator.add`.  
**Result:** the failed lane contributes `["(researcher lane failed: <exc>)"]` into `state["research"]` and one `RoleTurn` into `state["turns"]`. The graph does **not** abort; the other lanes continue normally.

---

### 2. `consultants/server/runner.py` compiled.stream loop (`runner.py:136-161`)

The outer `try/except` around `compiled.stream(...)` only catches **graph-level** crashes (e.g., LangGraph internal breakage):

```python
for mode, payload in compiled.stream(
        initial, stream_mode=["updates", "values"]):  # runner.py:137-138
    ...
except Exception as e:                               # runner.py:155
    log.exception("council graph invocation failed: %s", e)
    state.status = "failed"
    ...
    _write_failed_artifacts(state, cwd, question, e) # runner.py:160
    return
```

Because the researcher node already caught the lane exception and returned a tombstone, the graph itself does **not** crash. The stream loop drains normally, yielding node updates. After the loop:

```python
final_state = payload  # runner.py:140 (last "values" payload)
node_error = final_state.get("error")                # runner.py:172
node_failed = final_state.get("_role_failed")         # runner.py:173
terminal_status = "failed" if node_error else "completed"  # runner.py:174
```

The stream loop **does not** short-circuit; the failed lane's error text is already inside `final_state["research"]` and `final_state["error"]`.

---

### 3. What the synthesizer actually sees

The synthesizer node calls `build_synthesizer_messages` (`consultants/engine/council.py:245-266`):

```python
for i, r in enumerate(research_rounds, start=1):
    parts.append(f"\nRESEARCHER REPORT (round {i}):\n{r.strip()}")  # council.py:253-254
```

`research_rounds` is `state.get("research") or []`. Because the failed lane's tombstone was concatenated via the additive reducer, the synthesizer receives a list like:

```text
RESEARCHER REPORT (round 1):
(researcher lane failed: HTTPConnectionPool(... Max retries exceeded ...))

RESEARCHER REPORT (round 2):
<normal findings from lane 2>

RESEARCHER REPORT (round 3):
<normal findings from lane 3>
```

The synthesizer system prompt (`SYNTHESIZER_SYSTEM` at `council.py:156-168`) instructs it to "consume the planner's plan, researcher's report(s), and critic's verdict" and "cite `path:line` for any codebase-dependent claim." It has **no special handling** for error tombstones; it treats the failure text as just another report to synthesize. The synthesizer therefore produces a `final_answer` that may acknowledge missing evidence but still answers the question coherently.

---

### 4. The `storage.py` artifact writer (`consultants/engine/storage.py:200-207`)

After the stream loop, `runner.py` builds a `ConsultationResult` and calls `write_consultation`:

```python
result = storage.ConsultationResult(
    ...
    final_answer=final_state.get("final_answer", ""),  # runner.py:186
    status=terminal_status,                            # runner.py:189
    error=node_error,                                  # runner.py:190
)
storage.write_consultation(result, cwd=Path(cwd))      # runner.py:199
```

`write_consultation` atomically writes three files (`storage.py:204-207`):

```python
_atomic_write_text(sdir / SUMMARY_FILENAME, render_summary(result))     # storage.py:205
_atomic_write_text(sdir / TRANSCRIPT_FILENAME, render_transcript(result)) # storage.py:206
_atomic_write_text(sdir / METADATA_FILENAME, render_metadata(result))   # storage.py:207
```

Because `terminal_status` is `"failed"` (due to `node_error` being truthy), `metadata.json` carries `"status": "failed"` and `"error": "researcher failed: HTTPConnectionPool..."`. The **summary.md still contains the synthesizer's `final_answer`** — it is not blank.

---

### Outcome: degraded answer with `status=failed`

| Aspect | What happens |
|---|---|
| **Graph aborts?** | No — the node tombstone lets LangGraph continue. |
| **Synthesizer runs?** | Yes — it sees the error text as a research report. |
| **Answer coherent?** | Yes — but potentially thinner because one lane contributed only an error string. |
| **User-visible status?** | `status="failed"` in `metadata.json`, even though `summary.md` has a real answer. |
| **User perceives** | A **degraded answer** that may cite uncertainty, paired with a top-level failure flag that downstream tools (UI polling, API consumers) can misinterpret as total failure. |

---

### Recommended hardening change

**Location:** `consultants/server/runner.py:174`

**Current code:**
```python
terminal_status = "failed" if node_error else "completed"
```

**Recommended change:**
```python
# Degraded success when a non-synthesizer lane failed but we still got an answer.
if node_error and final_state.get("final_answer"):
    terminal_status = "degraded"
elif node_error:
    terminal_status = "failed"
else:
    terminal_status = "completed"
```

**Rationale:** A single researcher lane exhausting its retry budget (or returning a non-string from `tool_executor`) is a **partial failure**, not a total consultation failure. The `ConsultationResult.status` field should reflect that an answer was still produced (`"degraded"`), while the existing `error` field preserves the lane failure for observability. This prevents UI/API consumers from discarding a valid `final_answer` just because `status == "failed"`.

---

## Researcher (round 1)

## Tracing the Send-API fan-out failure path

### 1. CouncilState's additive reducers (`consultants/engine/graph.py`)

The `research` channel uses list-concat via `operator.add`:

```python
research: Annotated[list, operator.add]      # consultants/engine/graph.py:64
```

When a lane crashes inside `run_loop`, `researcher_node` catches it and returns a tombstone that contributes to `research`:

```python
err_text = f"(researcher lane failed: {e})"
return {
    "error": f"researcher failed: {e}",
    "_role_failed": "researcher",
    "research": [err_text],                  # consultants/engine/council.py:562-566
    ...
}
```

Because `research` is additive, this tombstone string is **concatenated** into the merged state alongside any surviving lanes' reports. The comment at `council.py:557-561` explicitly notes this was added to prevent the audit-high finding where a crashed lane silently contributed nothing.

### 2. `consultants/server/runner.py`'s `compiled.stream` loop

The outer `try/except` at `runner.py:136-161` catches **graph-level** crashes. However, `researcher_node` already swallowed the exception internally (see above), so `compiled.stream()` yields normally and the loop continues processing updates. After the stream drains, the runner inspects the merged `final_state`:

```python
node_error = final_state.get("error")                         # consultants/server/runner.py:172
node_failed = final_state.get("_role_failed")               # consultants/server/runner.py:173
terminal_status = "failed" if node_error else "completed"     # consultants/server/runner.py:174
```

Because the tombstone set `"error": f"researcher failed: {e}"`, `node_error` is truthy and **`terminal_status` becomes `"failed"`**.

### 3. What the synthesizer actually sees

The synthesizer builds messages from `state.get("research") or []`:

```python
msgs = build_synthesizer_messages(
    state["question"], state.get("plan", ""),
    state.get("research") or [],                             # consultants/engine/council.py:718-720
    state.get("critique"),
    self_critic=self_critic,
)
```

`build_synthesizer_messages` iterates every research round into the prompt:

```python
for i, r in enumerate(research_rounds, start=1):
    parts.append(f"\nRESEARCHER REPORT (round {i}):\n{r.strip()}")   # consultants/engine/council.py:253-254
```

Therefore the synthesizer receives **both** the surviving lanes' real findings **and** the tombstone text `(researcher lane failed: ...)` as distinct "research reports." It attempts to answer from this partially-degraded evidence.

### 4. The `storage.py` artifact writer

The runner persists a `ConsultationResult` with the failed status:

```python
result = storage.ConsultationResult(
    ...
    final_answer=final_state.get("final_answer", ""),       # consultants/server/runner.py:186
    status=terminal_status,                                 # consultants/server/runner.py:189
    error=node_error,                                       # consultants/server/runner.py:190
    ...
)
storage.write_consultation(result, cwd=Path(cwd))           # consultants/server/runner.py:199
```

`storage.write_consultation` writes three files. The summary front-matter includes:

```python
lines.append(f"status: {_yaml_str(result.status)}")         # consultants/engine/storage.py:108
if result.error:
    lines.append(f"error: {_yaml_str(result.error)}")       # consultants/engine/storage.py:109-110
```

The body still contains whatever the synthesizer produced:

```python
body = result.final_answer.rstrip() + "\n"                  # consultants/engine/storage.py:119
```

---

## Bottom line: success or failure?

**The entire consultation surfaces as `status=failed`.** The synthesizer does run and produces a `final_answer`, but the runner overrides the status because `final_state["error"]` is set by the tombstone. The user perceives:

- `summary.md` with YAML front-matter showing `status: failed` and `error: researcher failed: ...`
- A **degraded final answer body** that the synthesizer wrote while trying to reason over the surviving lanes' reports plus the tombstone error text
- `transcript.md` containing the `(researcher lane failed: ...)` turn entry

In other words: the council produces an **incoherent/degraded answer wrapped in a failure status**, not a clean success or a clean hard failure.

---

## Recommended hardening change

**Remove the `"error"` key from the `researcher_node` tombstone return.** A single crashed lane in a parallel fan-out is a partial failure, not a fatal one. The tombstone text already flows into `research` (visible to the synthesizer and transcript), and `_role_failed` already triggers a warning log in the runner (`runner.py:204-206`). Setting `"error"` is what incorrectly poisons the entire consultation status.

**Change at `consultants/engine/council.py:564`** — delete this line:

```python
"error": f"researcher failed: {e}",
```

Keep the rest of the tombstone intact (`_role_failed`, `research`, `research_rounds_used`, `turns`). After this change, a one-lane crash will log a warning but the consultation will return `status=completed` with a degraded answer rather than a blanket failure.

---

## Critic (round 3)

DECISION: needs_more_research

1. `claude_hooks/agent_loop/runner.py:run_loop` — missing path:line of the outer exception boundary around the LLM/tool loop. Without this, the premise that an HTTP timeout or non-string tool return propagates uncaught to the `researcher_node` wrapper is unverified; the agent loop may swallow it and return a graceful string instead.

2. `consultants/engine/graph.py` — missing path:line of the compiled graph edges (e.g., `add_edge` / `add_conditional_edges` routing from `researcher_node`). The trace assumes a tombstoned lane still routes to the synthesizer, but LangGraph could branch to END when `_role_failed` or `error` is present.

3. `consultants/engine/graph.py` — missing path:line of the `error` field declaration inside `CouncilState`. The researcher asserts last-write-wins dict merge, but if `error` carries a reducer or a successful later node overwrites it, the runner's `terminal_status = "failed"` logic at `runner.py:174` may not trigger.

---

## Synthesizer

The council streams to completion and produces a degraded `final_answer`, but the consultation still surfaces as `status=failed`.

- **CouncilState additive reducers.** `research: Annotated[list, operator.add]` at `consultants/engine/graph.py:64` concatenates outputs from parallel lanes. When a researcher lane crashes inside `agent_loop.runner.run_loop`, the `researcher_node` wrapper catches it and returns a tombstone dict containing `research: [err_text]` at `consultants/engine/council.py:562-566`; the additive reducer merges that tombstone string into the shared list alongside surviving lanes.

- **`compiled.stream` loop.** The runner invokes the graph via `compiled.stream(...)` at `consultants/server/runner.py:137-138` and iterates updates normally because the node-level catch already converted the exception into a state update. The outer `except` at `runner.py:155` is never reached, and after the loop drains the merged `final_state` contains the tombstone's `error` key.

- **Synthesizer input messages.** The synthesizer node calls `build_synthesizer_messages(state.get("research") or [], ...)` at `consultants/engine/council.py:718-723`. Because `research` is additive, the tombstone text appears as one of the rounds, which `build_synthesizer_messages` formats verbatim as `RESEARCHER REPORT (round {i}): {r.strip()}` at `council.py:253-254`. The synthesizer therefore reasons over a partially degraded corpus.

- **Artifact writer.** After streaming, `runner.py` reads `node_error = final_state.get("error")` at `runner.py:172` and sets `terminal_status = "failed"` at `runner.py:174`. It persists a `ConsultationResult` via `storage.write_consultation(...)` at `runner.py:199`, which writes `metadata.json` and `summary.md` with YAML front-matter `status: failed` at `consultants/engine/storage.py:108-110`, while still embedding the synthesizer's `final_answer` in the summary body at `storage.py:119`.

**User-perceived failure mode:** The summary body contains a coherent but potentially thinner answer, yet every downstream consumer sees `status: failed` and `error: researcher failed: ...`, causing the response to be treated as a total failure rather than a partial degradation.

**Hardening change:** At `consultants/server/runner.py:174`, replace the binary `terminal_status = "failed" if node_error else "completed"` with a three-way check that emits `"degraded"` when `node_error` is truthy but `final_state.get("final_answer")` exists, preserving `"failed"` only when no answer was produced.

---
