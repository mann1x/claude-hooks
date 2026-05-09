# Consultation transcript — csl-2026-05-09-0756-6bce

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. In `consultants/engine/graph.py`, find the `parallel_researcher` node (likely using `async_map`) and inspect its reducer function (e.g., `reduce` inside `CouncilState`). Locate the line where updates from parallel lanes are merged — check if a lane exception is caught and appended to `state.errors` (search for `state.errors.append` or similar). Determine if partial messages from other lanes are still added to `state.messages`.

2. In `consultants/server/runner.py`, examine the `compiled.stream` loop (around lines 65–90). Find the `while True` loop that feeds the graph execution. Identify how exceptions from inside a node (e.g., an unhandled `HTTPError` from a researcher lane) propagate: is there a `try/except` around the `async for` that yields events? If not, confirm the exception escapes to the outer `main()`.

3. In the same file or `consultants/engine/graph.py`, locate the synthesizer node’s input definition. Check whether the synthesizer receives the full `state.messages` list (including partial outputs from successful lanes) even when `state.errors` is non-empty, or whether the aggregator clears `state.messages` on error.

4. In `consultants/storage.py`, find the `ArtifactWriter` class and its `write` or `run` method (around lines 130–160). Verify if it is called unconditionally after the graph completes, or only when `state.status != 'failed'`. Check if it serializes the conversation and state even if errors are present.

5. Compile the exact failure path: from the unhandled exception in one parallel researcher, through the reducer, the runner’s loop, the synthesizer’s input, and the storage writer — noting each point where the error is either contained or allowed to break the whole consultation.

6. Based on the path, determine whether the user receives a coherent (degraded) answer or a full failure (status=failed). Then propose exactly ONE hardening change, specifying file and line number (e.g., add a try/except in the parallel lane reducer to convert exceptions into error messages appended to `state.messages`).

---

## Researcher (round 1)

## Failure Path Trace: Parallel Researcher Lane Crash in Send Fan-Out

### 1. CouncilState Additive Reducers (`consultants/engine/graph.py`)

`CouncilState` at `graph.py:50-80` declares fields with two reducer strategies:

| Field | Reducer | Type |
|---|---|---|
| `research` | `Annotated[list, operator.add]` (`graph.py:64`) | list concat |
| `turns` | `Annotated[list, operator.add]` (`graph.py:68`) | list concat |
| `total_prompt_tokens` | `Annotated[int, operator.add]` (`graph.py:71`) | integer sum |
| `error` | **no reducer** — plain `Optional[str]` (`graph.py:73`) | last-write-wins |

**Critical gap**: `error` is not additive. In the Send fan-out (`graph.py:349-385`), LangGraph spawns N parallel researcher invocations. Each returns a delta dict. For non-reduced fields, LangGraph's merge is a sequential `dict.update()` on the parent state. A return dict that omits `error` does NOT overwrite an existing `error` — so one lane's error *can* survive. But if TWO lanes fail, lane B's error overwrites lane A's — only the last failure is recorded.

### 2. The Exception Path Through `compiled.stream` (`consultants/server/runner.py`)

There are two distinct layers:

**Layer 1 — node-internal catch** (`council.py:555-573`):
```python
try:
    final = loop_runner(payload, cwd, config=cfg, ...)
except Exception as e:
    log.exception("researcher_node failed: %s", e)
    err_text = f"(researcher lane failed: {e})"
    return {
        "error": f"researcher failed: {e}",
        "_role_failed": "researcher",
        "research": [err_text],          # added to additive reducers
        "turns": [RoleTurn(...)],         # added to additive reducers
    }
```

`loop_runner` calls `chat_fn(payload)` at `agent_loop/runner.py:230`, which goes through `TracedChat.chat` (`trace.py:170-200`) into `ChatClient.chat`. If the HTTP call exhausts retries and raises, the exception propagates: `TracedChat.chat` catches-and-re-raises (`trace.py:179-181`), `run_loop` has no try/except around `chat_fn` at line 230, so it propagates to `researcher_node`'s `loop_runner(...)` call at line 548 — which IS caught by the `except Exception` at line 555.

**Layer 2 — outer catch in runner** (`runner.py:136-161`):
```python
try:
    for mode, payload in compiled.stream(initial, stream_mode=["updates", "values"]):
        ...
except Exception as e:
    state.status = "failed"
    state.error = f"graph crashed: {e}"
    return
```

This catches exceptions that escape node functions. **For the researcher lane crash scenario, the exception never reaches this layer** — `researcher_node`'s internal `except` at `council.py:555` catches it and returns a tombstone update. The graph continues normally.

### 3. What the Synthesizer Actually Sees

`build_synthesizer_messages` at `council.py:245-266` constructs the input:

```python
parts = [
    f"USER QUESTION:\n{question.strip()}",
    f"\nPLANNER'S PLAN:\n{plan.strip()}",
]
for i, r in enumerate(research_rounds, start=1):
    parts.append(f"\nRESEARCHER REPORT (round {i}):\n{r.strip()}")
```

The `research_rounds` list is `state.get("research") or []`. Because `research` uses **operator.add**, all parallel lanes' results are concatenated. So the synthesizer sees something like:

```
RESEARCHER REPORT (round 1):
- Findings from surviving lane (path:line refs, real evidence)
...

RESEARCHER REPORT (round 2):
(researcher lane failed: HTTP timeout after 10 retries)
```

**There is NO warning prefix, no structural distinction.** The tombstone `(researcher lane failed: ...)` is formatted identically to a real report. `SYNTHESIZER_SYSTEM` (`council.py:156-168`) only says "consuming the planner's plan, researcher's report(s), and critic's verdict" — nothing about handling partial failures.

The synthesizer *may* notice the "(researcher lane failed" text and downgrade its confidence, but the prompt doesn't instruct it to do so. **Most likely: the synthesizer produces a confident answer based on surviving lanes only**, treating the failure marker as an opaque text it can't use, and writes a complete-looking `final_answer`.

### 4. The `storage.py` Artifact Writer (`runner.py:177-199`)

After `compiled.stream` drains, the runner builds `ConsultationResult` at `runner.py:177-199`:

```python
node_error = final_state.get("error")          # line 172 — "researcher failed: timeout"
node_failed = final_state.get("_role_failed")   # line 173 — "researcher"
terminal_status = "failed" if node_error else "completed"  # line 174 — "failed"

result = ConsultationResult(
    final_answer=final_state.get("final_answer", ""),   # line 186 — synthesizer's output
    turns=list(final_state.get("turns") or []),           # line 187 — includes failed-lane turn
    status=terminal_status,                                # line 189 — "failed"
    error=node_error,                                      # line 190 — set
)
storage.write_consultation(result, cwd=Path(cwd))         # line 199
```

This writes three files under `<cwd>/.claude-hooks/consultants/<sid>/` (`storage.py:200-207`):

- **`summary.md`** — YAML front-matter + `final_answer`. Front-matter shows `status: failed`. The answer body is the synthesizer's complete-looking text from surviving lanes.
- **`transcript.md`** — Full per-role conversation including the tombstone turn with `(researcher lane failed: ...)`.
- **`metadata.json`** — Structured record with `"status": "failed"` and `"error": "researcher failed: ..."`.

### Verdict: What the User Perceives

| Question | Answer |
|---|---|
| Does the council still produce a coherent answer? | **Yes** — the synthesizer runs normally and writes a `final_answer` string. The answer looks coherent, is well-formatted, and cites `path:line` from the surviving lane. |
| Does it surface as `status=failed`? | **Yes** — `final_state["error"]` is non-empty, so `terminal_status = "failed"` at `runner.py:174`. The metadata.json and summary.md both show `status: failed`. |
| **Failure mode the user perceives** | **Confidently incomplete answer with a "failed" label.** The user sees `status=failed` and a plausible-looking answer. To discover WHY it failed, they must read `transcript.md` and notice the tombstone. The answer itself gives no warning that a research lane crashed — it reads as authoritative but is missing whatever the dead lane was supposed to investigate. |

### One Hardening Change

**Problem location**: `build_synthesizer_messages` at `council.py:245-248` — the function has no awareness of partial failures. It wraps all research entries (including failure tombstones) uniformly as `RESEARCHER REPORT (round N)`.

**Change**: After `council.py:254` (the `for` loop that builds `parts`), scan `research_rounds` for lane-failure patterns. If found, inject an explicit warning into the synthesizer's messages **before** any research reports:

```python
# council.py, after line 224 (end of the researcher report loop)
# Check for partial researcher failures and alert the synthesizer.
# Without this, a crashed lane blends into the report list and the
# synthesizer produces a confident answer missing that lane's contribution.
failed_lanes = [
    r for r in research_rounds
    if r.startswith("(researcher lane failed") or r.startswith("(researcher failed")
]
if failed_lanes:
    parts.insert(2, (
        f"\n**WARNING**: {len(failed_lanes)} of {len(research_rounds)} "
        "research lane(s) encountered an error and returned no findings. "
        "The evidence below is INCOMPLETE. In your answer, explicitly "
        "identify which sub-questions remain unanswered."
    ))
```

**Why this is the single highest-impact change**: It directly fixes the "confidently degraded answer" failure mode — the synthesizer receives an unambiguous instruction to downgrade its completeness claims. It requires no state schema changes (relies on the existing tombstone pattern in `research_rounds`), no reducer changes, and no new state fields. It adds ~8 lines at `council.py:224` with zero risk to existing happy-path behavior (the scan only triggers when a failure marker is present).

---

## Researcher (round 1)

## Failure Path Trace: One Researcher Lane Raises in `run_loop`

### 1. Exception origin within `run_loop`

In `claude_hooks/agent_loop/runner.py:230`, the inner loop calls `chat_fn(payload)`. If `ChatClient.chat()` exhausts its 8 retries (defined at `chat_client.py:31` via `DEFAULT_MAX_RETRIES = 8`), it raises `RuntimeError` at line 285-286. There is **no `try`/`except`** wrapping line 230 inside the `for i in range(config.max_iterations)` loop — the exception propagates directly out of `run_loop`.

If the `tool_executor` (line 122) itself raises (not returns non-string — non-string return is silently coerced into `results` as a dict at line 133), that also propagates out of the loop since there is no `try` on the `execute_tool_calls` call at line 300.

### 2. `researcher_node` catches and tombstone-returns

Back in `consultants/engine/council.py:555`, the `except Exception as e:` block catches the propagation:

```python
except Exception as e:
    log.exception("researcher_node failed: %s", e)
    err_text = f"(researcher lane failed: {e})"
    return {
        "error": f"researcher failed: {e}",           # line 564
        "_role_failed": "researcher",                   # line 565
        "research": [err_text],                         # line 566
        "research_rounds_used": 1,                      # line 567
        "turns": [RoleTurn(... content=err_text)],      # lines 568-573
    }
```

This is a *delta* return — LangGraph merges it into the running state via the additive reducers defined in `CouncilState` (`graph.py:64`, `graph.py:68`): `research: Annotated[list, operator.add]` and `turns: Annotated[list, operator.add]`.

**Key consequence:** The `research` list is **appended to** (not replaced by) whatever the successful parallel lanes returned. So after fan-out merge, `state.research` = `["findings from lane 0", ..., "(researcher lane failed: HTTP timeout after 8 retries)"]`.

### 3. `error` and `_role_failed` in additive vs non-additive reducers

`CouncilState` at `graph.py:74-75`:
```python
error: Optional[str]           # line 74 — NOT additive, no Annotated
_role_failed: Optional[str]    # line 75 — NOT additive
```

These are **plain keys with no reducer**. LangGraph's default dict-merge behavior for non-additive keys is **last writer wins** across the parallel Send fan-out. Since the failing lane returns `error` and `_role_failed` while the successful lanes do NOT return these keys, the values **persist** — a successful lane's return dict lacks the key so it doesn't overwrite.

### 4. CouncilState after fan-out + merge

State contains:
- `research`: `["lane0 findings", "lane1 findings", "(researcher lane failed: ...)"]`
- `turns`: `[RoleTurn(lane0), RoleTurn(lane1), RoleTurn(failed_lane, content=error_text)]`
- `error`: `"researcher failed: ..."`
- `_role_failed`: `"researcher"`
- `total_prompt_tokens`, `total_completion_tokens`: sum of all lanes

### 5. Synthesizer input — what it actually sees

`build_synthesizer_messages` at `council.py:245-266` reads `state.get("research") or []` and iterates over it:

```python
for i, r in enumerate(research_rounds, start=1):
    parts.append(f"\nRESEARCHER REPORT (round {i}):\n{r.strip()}")
```

So the synthesizer receives:
```
USER QUESTION: ...
PLANNER'S PLAN: ...
RESEARCHER REPORT (round 1):
<lane 0 findings>
RESEARCHER REPORT (round 2):
<lane 1 findings>
RESEARCHER REPORT (round 3):
(researcher lane failed: HTTP timeout after 8 retries)
...
Now write the final answer...
```

The synthesizer LLM **sees the failure as text** — it's another "report" that reads like a failure. The LLM incorporates it naturally: it writes an answer based on the successful lanes' evidence, and may acknowledge the gap.

### 6. `runner.py` terminal status logic

Back in `consultants/server/runner.py:171-174`:
```python
node_error = final_state.get("error")            # = "researcher failed: ..."
node_failed = final_state.get("_role_failed")    # = "researcher"
terminal_status = "failed" if node_error else "completed"
```

**`terminal_status` is set to `"failed"`** because `node_error` is truthy. The `node_failed` is checked only as a log warning at line 204-206 — it does NOT influence status or artifact writing.

### 7. Artifact writer

The artifact code at `runner.py:176-199` runs **unconditionally** (not gated on status). `storage.write_consultation(result, cwd=Path(cwd))` at line 199 writes three files:

- `summary.md` — YAML front-matter includes `status: failed` + `error: researcher failed: ...`; body contains the synthesizer's `final_answer` (coherent answer over surviving lanes)
- `transcript.md` — full conversation including all lane turns + failed lane's tombstone `RoleTurn`
- `metadata.json` — `{"status": "failed", "error": "researcher failed: ...", "final_answer": "..."}`

### 8. What the user sees — degraded but NOT silent

**Status poll** (`GET /v1/consult/{sid}`, `app.py:232`): returns `"status": "failed"` with error text. The CLI (`consultants/cli.py:142`) prints this as JSON.

**Result fetch** (`GET /v1/consult/{sid}/result`, `app.py:247`): returns `summary_markdown` (with `status: failed` in front-matter) and `metadata` (with `status: "failed"`). The summary body contains the synthesizer's answer — it's a **coherent partial answer** covering only the successfully-researched plan items.

**Failure mode perceived by user:** The consultation shows `status: "failed"`, but the summary.md contains a real (degraded) answer. If the UI/CLI filters by status, the user may never see the partial answer. If they open the summary manually, they get the partial answer with an error annotation.

### 9. Degradation granularity

- The synthesizer **does not know which plan item(s) the failed lane covered**. The researcher node's tombstone puts the error text into `research`, but the lane's `plan_item` assignment is in `state.get("plan_item")` which is a **per-Send field** that does NOT survive into the merged state (it's `Optional[str]` with no reducer at `graph.py:79`). The synthesizer sees "something failed" but not *what specifically* was lost.
- The answer is coherent but may have blind spots — the failed lane's research question is never answered.
- Token and budget tracking is accurate: `total_prompt_tokens` correctly sums the failed lane's 0 tokens against the successful lanes' real usage.

### 10. Recommended hardening change

In `consultants/engine/council.py:557-562`, the tombstone return already exists (added after the audit-high finding). But the error text is opaque to the synthesizer about WHICH plan item failed. **Add the failed `plan_item` (from the Send payload) into the tombstone error text**:

**`consultants/engine/council.py:556-566`** — Change:
```python
err_text = f"(researcher lane failed: {e})"
```
To:
```python
plan_item = state.get("plan_item", "")
err_text = (f"(researcher lane failed: {e}; "
            f"plan item: {plan_item[:200]})")
```

This surfaces the specific gap in the synthesizer's context so it can annotate its answer with "the investigation of [plan item] was not completed due to an upstream error." Without this, the synthesizer generates a confidently-degraded answer over the surviving lanes only, and the user cannot distinguish "researched and found nothing" from "research never completed."

### Summary table: failure mode by layer

| Layer | What happens | Path:Line |
|---|---|---|
| **`run_loop`** | Exception propagates uncaught from `chat_fn(payload)` | `runner.py:230` → no `try` |
| **`researcher_node`** | `except Exception` catches, returns tombstone delta with `error` + `_role_failed` + `research: [err_text]` | `council.py:555-573` |
| **CouncilState merge** | `research` additive reducer appends error to successful lanes; `error`/`_role_failed` (non-additive) persist via last-writer | `graph.py:64,74-75` |
| **Synthesizer input** | Error text appears as a literal "RESEARCHER REPORT" entry in the prompt | `council.py:245-254` |
| **Status decision** | `terminal_status = "failed"` because `final_state.get("error")` is truthy | `runner.py:174` |
| **Artifact writer** | `write_consultation` runs unconditionally with `status="failed"`, writes partial answer | `runner.py:199` |
| **User perceives** | Status = "failed" but summary.md has a real partial answer; synthesizer lacks knowledge of *which* plan items died | No `plan_item` in error text (`council.py:562`) |

---

## Researcher (round 1)

## Failure Trace: One Parallel Researcher Lane Crashes Inside `run_loop`

The scenario: Send fan-out spawns 3 parallel researcher lanes (`consultants/engine/graph.py:358-385`). Lane 2 hits an HTTP timeout inside `run_loop` or a `tool_executor` returning a non-string, raising `Exception` at `claude_hooks/agent_loop/runner.py:230` (chat_fn) or `runner.py:122` (tool_executor → `execute_tool_calls` at `runner.py:300`).

### 1. The exception IS caught — by `researcher_node`, not by the graph runner

The `run_loop` call at `council.py:548` is wrapped in `try/except Exception` at `council.py:547-554`. The propagating exception from `run_loop` lands here and is converted to a **tombstone return** at `council.py:563-573`:

```python
# council.py:563-573  —  key lines
return {
    "error": f"researcher failed: {e}",      # line 564
    "_role_failed": "researcher",             # line 565
    "research": [err_text],                   # line 566  — additive reducer
    "research_rounds_used": 1,                # line 567
    "turns": [RoleTurn(...)],                 # line 568-572
}
```

`research` is additive, so the crashed lane's `["(researcher lane failed: {e})"]` concatenates with the successful lanes' findings. `error` and `_role_failed` are **non-reducer keys** (no `Annotated[..., add]` in `CouncilState` at `graph.py:73-74`). They use **last-write-wins** default merge. The successful lanes don't return these keys, so the crashed lane's values persist.

### 2. The `compiled.stream` loop at `runner.py:137-161` completes normally

Because `researcher_node` caught the exception and returned a dict, the LangGraph Send sub-invocation succeeds. The stream drains without raising. `final_state` at `runner.py:139-140` accumulates every lane's merged output — including the crashed lane's tombstone data.

After the loop, at `runner.py:172-174`:

```python
node_error = final_state.get("error")       # line 172 — picks up crashed lane's error
node_failed = final_state.get("_role_failed")
terminal_status = "failed" if node_error else "completed"  # line 174 → "failed"
```

**The graph does NOT crash. But the state's `error` key poisons the status check.**

### 3. The synthesizer's input messages — sees the error as a research report

`synthesizer_node` at `council.py:718-723` calls `build_synthesizer_messages` (`council.py:245-266`):

```python
# council.py:248-254
parts = [
    f"USER QUESTION:\n{question.strip()}",
    f"\nPLANNER'S PLAN:\n{plan.strip()}",
]
for i, r in enumerate(research_rounds, start=1):
    parts.append(f"\nRESEARCHER REPORT (round {i}):\n{r.strip()}")
```

`research_rounds` is the merged list from all lanes. The synthesizer sees:
- Lane 1's real findings (e.g., `survey_project output, file paths`)
- Lane 2's error text (`(researcher lane failed: HTTP timeout after 3 retries)`)
- Lane 3's real findings

The crashed lane's error is just another research report entry. The synthesizer's prompt (`council.py:156-168`) doesn't tell it how to handle failure markers in research reports. It may or may not mention the gap, depending on the model. It will typically produce an answer based on the surviving lanes' data, possibly omitting the crashed lane's intended findings.

### 4. Storage writer — writes `status=failed` with potentially useful content

At `runner.py:176-199`:

```python
result = storage.ConsultationResult(
    final_answer=final_state.get("final_answer", ""),   # line 186 — synthesizer's output
    turns=list(final_state.get("turns") or []),          # line 187
    status=terminal_status,                              # line 189 — "failed"
    error=node_error,                                    # line 190 — "researcher failed: ..."
    ...
)
storage.write_consultation(result, cwd=Path(cwd))        # line 199
```

The three on-disk files (`storage.py:204-207`) are written:
- `summary.md` with YAML front-matter `status: failed` + body = synthesizer's answer
- `transcript.md` with all turns including the crashed lane's error
- `metadata.json` with `"status": "failed"` and `"error": "researcher failed: ..."`

### 5. The user-facing verdict

**The council produces a coherent-but-degraded answer, then marks it as `status=failed`.** The synthesizer's `final_answer` is usually non-empty and relevant (based on the surviving lanes). But the `/v1/consult/{sid}` poll returns `{"status": "failed", "error": "researcher failed: ..."}` (`app.py:53-54`). The CLI or consumer sees `failed` and may discard or distrust the `summary.md`.

**Failure mode the user perceives:** "Consultation failed" despite visible text in the result. The user has to open `summary.md` to determine the answer is actually usable. The `status=failed` flag is a false negative — the error from one parallel lane poisoned the global `state["error"]` even though the surviving lanes + synthesizer produced valid output.

---

## Recommended Hardening Change

**File:** `consultants/engine/council.py`, lines 563-564

**Problem:** The tombstone return in the parallel-lane path sets `error` and `_role_failed` on the global state, which becomes the council's `status: failed` even when other lanes succeeded and the synthesizer produced valid output.

**Fix:** Conditionally suppress `error`/`_role_failed` when the researcher is running as a fan-out lane (has `plan_item`). In that context, the error signal is already carried by the `"research": [err_text]` entry — the synthesizer sees it. Suppressing the global error flag lets the consultation complete with `status=completed` and a degraded-but-honest answer.

```python
# --- council.py lines 555-573, replace the current except block ---
    except Exception as e:
        log.exception("researcher_node failed: %s", e)
        err_text = f"(researcher lane failed: {e})"
        # Fan-out lanes: propagate error as research content (visible
        # to synthesizer) but NOT as global state flags that poison
        # the entire council's status. The single-researcher path
        # (critic re-route, no plan_item) still sets error so a lone
        # researcher failure correctly marks the consultation failed.
        is_fanout_lane = state.get("plan_item") is not None
        return {
            **(  # conditional keys
                {"error": f"researcher failed: {e}",
                 "_role_failed": "researcher"}
                if not is_fanout_lane else {}
            ),
            "research": [err_text],
            "research_rounds_used": 1,
            "turns": [RoleTurn(
                role="researcher", round=this_round, content=err_text,
                prompt_tokens=0, completion_tokens=0,
                duration_seconds=0.0,
            )],
        }
```

**Rationale:** One parallel lane failing should degrade the answer (missing findings from that lane) — not kill the whole consultation. The error text in the `research` list lets the synthesizer write "Lane X failed; here's what the other lanes found." Without this change, a single timeout in one of 3 parallel lanes produces `status=failed` with perfectly good answers from the other two lanes, forcing the user to manually inspect the artifact to discover the answer is usable.

---

## Critic (round 3)

DECISION: ready

The three rounds of research converge on the same failure path: exception caught inside `council.py:555`, tombstone return with research list and error key, state merge appends error text, synthesizer sees it as an ordinary research report, runner sets status=failed at `runner.py:174`, artifact writer writes partial answer as summary.md. All required trace points (graph.py reducers, runner.py stream loop, synthesizer input construction, storage.py writer) are cited with exact path:line. The reports also supply three concrete hardening proposals at specific lines. No concrete missing fact remains.

---

## Synthesizer

The council produces a coherent (degraded) final answer, but **marks it `status=failed`**. The user sees a partial answer that looks authoritative, missing the failed lane’s findings, with no built-in warning that research was incomplete.

**Failure trace (exception → user):**

1. **`agent_loop/runner.py:230`** – `chat_fn(payload)` raises (HTTP timeout exhausts retries at `chat_client.py:285`, or tool-executor exception at `runner.py:300`). No `try`/`except` wraps line 230 inside the loop – exception propagates out of `run_loop`.

2. **`council.py:555`** – `researcher_node`’s `except Exception` catches it. Returns tombstone delta: `{"error": "researcher failed: …", "research": ["(researcher lane failed: …)"], "turns": [RoleTurn(content=err_text)], "research_rounds_used": 1}`.  
   `error` is a plain key (no reducer, `graph.py:73`), so it persists via last-write-wins; `research` uses `operator.add` (`graph.py:64`), so the failure text is appended to the successful lanes’ findings.

3. **`runner.py:137-161`** (the `compiled.stream` loop) – no exception escapes because the node handled it. Stream drains normally. After loop, `runner.py:172-174`:  
   `node_error = final_state.get("error")` → truthy → `terminal_status = "failed"`.

4. **`council.py:245-254`** (`build_synthesizer_messages`) – builds the prompt from `state.get("research") or []`. The synthesizer sees:  
   ```
   RESEARCHER REPORT (round 1): <lane-0 findings>
   RESEARCHER REPORT (round 2): <lane-1 findings>
   RESEARCHER REPORT (round 3): (researcher lane failed: HTTP timeout after 8 retries)
   ```
   No special indicator distinguishes the failed lane; the prompt (`council.py:156-168`) does not instruct the synthesizer to handle partial failures.

5. **`runner.py:186-199`** – `ConsultationResult` is built with `final_answer=final_state.get("final_answer", "")` (synthesizer’s output) and `status="failed"`.  
   `storage.write_consultation` (`runner.py:199`) writes `summary.md` (YAML front-matter `status: failed` + full answer body), `transcript.md`, `metadata.json` (`"status": "failed"`). Writes unconditionally (no status gate).

**User-perceived failure mode:**  
A `status=failed` label on a *coherent, well-formatted partial answer*. The answer looks complete and cites paths from surviving lanes, but the missing lane’s questions are unaddressed. To discover *why* it’s marked failed, the user must inspect `transcript.md` and find the tombstone. The synthesizer never warns that evidence is incomplete.

---

**One hardening change** (highest impact, minimal risk):

In **`consultants/engine/council.py:563-566`**, modify the tombstone return so that when the researcher is running as a **fan-out lane** (detectable via `state.get("plan_item") is not None`), the `error` and `_role_failed` keys are **omitted** from the return dict. The failure signal is already carried in the `research` list – the synthesizer sees the `(researcher lane failed: ...)` text. Omitting the global `error` key lets the runner set `terminal_status = "completed"` while the answer is still degraded and explicit about the gap.

```python
# council.py, lines 555-573 — replace current except block
    except Exception as e:
        log.exception("researcher_node failed: %s", e)
        err_text = f"(researcher lane failed: {e})"
        is_fanout_lane = state.get("plan_item") is not None
        ret = {
            "research": [err_text],
            "research_rounds_used": 1,
            "turns": [RoleTurn(
                role="researcher", round=this_round,
                content=err_text,
                prompt_tokens=0, completion_tokens=0,
                duration_seconds=0.0,
            )],
        }
        if not is_fanout_lane:
            # Only the single-researcher path (critic re-route) poisons status
            ret["error"] = f"researcher failed: {e}"
            ret["_role_failed"] = "researcher"
        return ret
```

**Rationale:** A single parallel lane failing should degrade the answer (synthesizer sees the error text as a report entry) but not kill the whole consultation. This change makes one lane’s timeout produce `status=completed` with a partial answer that the synthesizer can honestly qualify (e.g., “the investigation of [plan item] failed, but findings from the other lanes are below”). It does not require schema changes, new state fields, or alterations to the synthesizer prompt.

---
