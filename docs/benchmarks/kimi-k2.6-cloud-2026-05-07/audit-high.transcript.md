# Consultation transcript — csl-2026-05-07-0813-faa2

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. Inspect `consultants/engine/graph.py` to locate the `CouncilState` class and its additive reducers (e.g., `messages`, `researcher_outputs`) and determine whether an exception in one parallel lane leaves an exception object, an error string, or a gap in the reduced list.
2. Examine `consultants/server/runner.py` around the `compiled.stream(...)` call to identify the exact try/except boundaries per parallel researcher lane and verify whether an unhandled exception in `agent_loop.runner.run_loop` terminates the entire stream or is isolated.
3. Locate the `agent_loop.runner.run_loop` implementation (check `consultants/engine/runner.py` or imports in `consultants/server/runner.py`) and confirm that HTTP timeouts or non-string tool outputs raise uncaught exceptions rather than returning error-wrapped states.
4. Trace the synthesizer node input wiring in `consultants/engine/graph.py` to identify which state key feeds the synthesizer and inspect whether failed lanes produce tombstone messages, omissions, or raw exception objects in that input list.
5. Read `consultants/server/storage.py` to find the artifact writer and check whether it writes `status=failed` when upstream lanes except, or persists a degraded/partial answer synthesized from incomplete inputs.
6. Correlate findings from steps 1–5 to classify the exact user-visible failure mode (coherent degraded answer vs. `status=failed`) and cite `path:line` for every claim in the trace.
7. Recommend exactly one hardening change with a concrete `path:line` in `consultants/server/runner.py` or `consultants/engine/graph.py` where per-lane exceptions should be caught and normalized into error messages before state reduction.

---

## Researcher (round 1)

Here is the concrete failure trace, followed by the outcome and a single hardening recommendation.

## 1.  CouncilState additive reducers (`consultants/engine/graph.py`)

The state schema uses `operator.add` reducers for the fields that fan-out lanes merge:

- `research: Annotated[list, operator.add]` at `consultants/engine/graph.py:64`
- `turns: Annotated[list, operator.add]` at `consultants/engine/graph.py:68`
- `research_rounds_used: Annotated[int, operator.add]` at `consultants/engine/graph.py:69`
- `total_prompt_tokens: Annotated[int, operator.add]` at `consultants/engine/graph.py:71`
- `total_completion_tokens: Annotated[int, operator.add]` at `consultants/engine/graph.py:72`

`error` and `_role_failed` have **no** additive reducer (`Optional[str]` at lines 74-75).  When a failed lane returns an `error` dict and a successful lane returns `research`, LangGraph merges them with default dict-merge semantics: the `research` list is the concatenation of all successful lanes, but `error` is simply overwritten (last-writer-wins among the parallel nodes).

## 2.  The `compiled.stream` loop (`consultants/server/runner.py`)

The runner consumes the graph in streaming mode:

```python
for mode, payload in compiled.stream(
        initial, stream_mode=["updates", "values"]):
```
at `consultants/server/runner.py:137-138`.

The **only** `except` around the stream is:

```python
except Exception as e:
    log.exception("council graph invocation failed: %s", e)
    state.status = "failed"
    state.error = f"graph crashed: {e}"
    ...
    _write_failed_artifacts(state, cwd, question, e)
    return
```
at `consultants/server/runner.py:155-161`.

Because `researcher_node` catches the exception internally and returns a plain dict, **no exception reaches this handler**.  The stream drains normally, yielding updates for all successful lanes plus the error dict from the failed lane.

After the loop, the runner inspects the merged state:

```python
node_error = final_state.get("error")
node_failed = final_state.get("_role_failed")
terminal_status = "failed" if node_error else "completed"
```
at `consultants/server/runner.py:172-174`.

## 3.  What the synthesizer actually sees

`researcher_node` wraps the `run_loop` call in a try/except:

```python
try:
    final = loop_runner(
        payload, cwd,
        config=cfg,
        tool_specs=tool_specs,
        chat_fn=chat_client.chat,
        tool_executor=tool_executor,
    )
except Exception as e:
    log.exception("researcher_node failed: %s", e)
    return {"error": f"researcher failed: {e}",
            "_role_failed": "researcher"}
```
at `consultants/engine/council.py:530-541`.

Notice the **absence** of `"research"` in the error return.  The additive reducer therefore concatenates the successful lanes' findings but **omits the failed lane entirely**.  When the graph proceeds to synthesizer (via the unconditional `researcher -> synthesizer` edge in `plan_topology` at `consultants/engine/graph.py:219`), `synthesizer_node` calls:

```python
msgs = build_synthesizer_messages(
    state["question"], state.get("plan", ""),
    state.get("research") or [],
    state.get("critique"),
    self_critic=self_critic,
)
```
at `consultants/engine/council.py:673-678`.

`build_synthesizer_messages` iterates `research_rounds` (the `research` list) at `consultants/engine/council.py:253-254`.  The synthesizer receives **only the successful lanes' reports**; it has no indication that a lane crashed.

## 4.  The `storage.py` artifact writer

The runner persists the result regardless of `node_error`:

```python
result = storage.ConsultationResult(
    ...
    final_answer=final_state.get("final_answer", ""),
    turns=list(final_state.get("turns") or []),
    status=terminal_status,
    error=node_error,
    ...
)
storage.write_consultation(result, cwd=Path(cwd))
```
at `consultants/server/runner.py:177-199`.

`storage.write_consultation` at `consultants/engine/storage.py:200-208` writes three files:
- `summary.md` — `render_summary` at `consultants/engine/storage.py:117-120` emits YAML front-matter containing `status: failed` and `error: "researcher failed: ..."`, followed by the synthesizer's (degraded) body text.
- `transcript.md` — `render_transcript` at `consultants/engine/storage.py:123-158` iterates `result.turns`.  Because the failed lane returned **no** `turns`, the transcript simply skips that lane entirely.
- `metadata.json` — `render_metadata` at `consultants/engine/storage.py:166-169` serializes the full `ConsultationResult` including `status: "failed"` and the `error` string.

## Verdict: degraded answer, not a total crash

The council **does not** fail as `status=failed` with a blank answer.  The stream completes, the synthesizer runs with a **partial** `research` list, and the user receives:

- A `summary.md` whose body contains a synthesizer-generated answer based on incomplete evidence.
- Front-matter tagging the consultation as `status: failed` with `error: researcher failed: ...`.
- A transcript that silently omits the failed lane.

The perceived failure mode is a **silent gap**: the user gets an answer that looks coherent but is missing findings from the failed lane, and the only clue is the `status: failed` header plus the missing research section in the transcript.

## Recommended hardening (one change)

**Location:** `consultants/engine/council.py:538-541`

**Change:** when `researcher_node` catches an exception, return a placeholder `research` entry (and a corresponding `RoleTurn`) so the synthesizer knows a lane failed, instead of silently omitting it.

**Current code:**
```python
    except Exception as e:
        log.exception("researcher_node failed: %s", e)
        return {"error": f"researcher failed: {e}",
                "_role_failed": "researcher"}
```

**Hardened code:**
```python
    except Exception as e:
        log.exception("researcher_node failed: %s", e)
        err_text = f"(researcher lane failed: {e})"
        return {
            "error": f"researcher failed: {e}",
            "_role_failed": "researcher",
            "research": [err_text],
            "turns": [RoleTurn(
                role="researcher", round=this_round, content=err_text,
                prompt_tokens=0, completion_tokens=0, duration_seconds=0,
            )],
        }
```

This single change ensures the synthesizer prompt includes the failure stub, the transcript records the missing lane, and the additive reducers receive a delta for every lane.  The synthesizer can then explicitly note the gap in its final answer rather than producing a silently incomplete response.

---

## Researcher (round 1)

Here is the concrete trace, grounded in the code.

---

### 1. CouncilState additive reducers (`consultants/engine/graph.py`)

The fan-out uses `Send` to spawn parallel researcher lanes at `graph.py:358-383`.  The state schema declares:

```python
research: Annotated[list, operator.add]          # graph.py:64
turns: Annotated[list, operator.add]             # graph.py:68
total_prompt_tokens: Annotated[int, operator.add]    # graph.py:71
total_completion_tokens: Annotated[int, operator.add]# graph.py:72
```

These keys accumulate across lanes.  Crucially, **`error`** and **`_role_failed`** are **bare** `Optional[str]` with **no** additive annotation:

```python
error: Optional[str]        # graph.py:74
_role_failed: Optional[str] # graph.py:75
```

When a lane fails inside `researcher_node`, it returns:

```python
{"error": f"researcher failed: {e}", "_role_failed": "researcher"}  # council.py:540-541
```

It does **not** return `research`, `turns`, or token counters.  Therefore:
- `research` gets **nothing** from the failed lane (successful lanes still concatenate).
- `error` is set once; because it lacks an annotated reducer, later successful lanes that omit the key leave it untouched (last-write-wins if multiple lanes fail).

---

### 2. `compiled.stream` loop (`consultants/server/runner.py`)

The runner wraps the graph in a streaming loop:

```python
for mode, payload in compiled.stream(
        initial, stream_mode=["updates", "values"]):  # runner.py:137-138
```

The `except Exception` at `runner.py:155` **only** catches exceptions thrown **by the stream machinery itself** (e.g., graph compilation bugs).  It does **not** catch node-level exceptions, because every node wrapper in `council.py` has an internal `try/except` that swallows exceptions and returns a partial-state dict.  Thus, when `run_loop` throws inside a lane, the stream loop keeps ticking and eventually routes to the synthesizer as if nothing catastrophic happened.

---

### 3. What the synthesizer actually sees (`consultants/engine/council.py`)

`synthesizer_node` builds its input from:

```python
state.get("research") or []   # council.py:675
```

It **never inspects** `state.get("error")` or `state.get("_role_failed")`.  Consequently:

| Scenario | What synthesizer sees |
|---|---|
| 2 of 3 lanes succeed | `research = [lane1_text, lane2_text]` — no tombstone, no exception object, no indication that lane 3 is missing. |
| All lanes fail | `research = []` — empty list; synthesizer is asked to answer with zero evidence. |

The synthesizer produces a `final_answer` anyway because there is no gate that skips it when `error` is present.

---

### 4. Artifact writer (`consultants/engine/storage.py`)

After the stream drains, `runner.py:171-198` inspects the merged `final_state`:

```python
node_error = final_state.get("error")          # runner.py:172
terminal_status = "failed" if node_error else "completed"  # runner.py:174
```

It then constructs a `ConsultationResult` and persists it:

```python
storage.write_consultation(result, cwd=Path(cwd))  # runner.py:199
```

`write_consultation` at `storage.py:200-208` atomically writes:
- `summary.md` — YAML front-matter says `status: failed`, `error: "researcher failed: ..."`, but the **body** is still the synthesizer's `final_answer`.
- `metadata.json` — mirrors the `status: failed` and `error` fields.
- `transcript.md` — only contains turns from successful lanes; the failed lane leaves no trace here.

---

### Verdict: coherent answer or total failure?

**The council produces a degraded answer with `status=failed`.**  
The synthesizer runs on incomplete inputs, writes a plausible-looking answer into `final_answer`, but the runner stamps the artifact as `failed` because `node_error` is truthy.

**Failure mode the user perceives:**  
A confusing hybrid artifact — the Markdown body reads like a real consultation result, but the YAML header says `status: failed` and `error: researcher failed: <exception>`.  If all lanes failed, the synthesizer may emit a vacuous or hallucinated answer, still wrapped in a `failed` shell.

---

### Recommended hardening change

**Location:** `consultants/engine/council.py:672-678`  
**Problem:** `synthesizer_node` is blind to upstream lane failures; it synthesizes confidently from partial evidence.  
**Fix:** Inject the upstream error into the research list so the synthesizer can produce an honest, disclaimer-bearing partial answer instead of an unlabeled degraded one.

```python
# consultants/engine/council.py:672
def synthesizer_node(state: dict, *, chat_client, model: str,
                     think: Any = True,
                     self_critic: bool = False) -> dict:
    research_rounds = list(state.get("research") or [])
    node_error = state.get("error")
    if node_error:
        research_rounds.append(
            f"[SYSTEM NOTE: upstream researcher failure — {node_error}]"
        )
    msgs = build_synthesizer_messages(
        state["question"], state.get("plan", ""),
        research_rounds,
        state.get("critique"),
        self_critic=self_critic,
    )
    # ... rest of function unchanged
```

This single change ensures the synthesizer is explicitly informed of missing lanes, so its output will contain a visible disclaimer (e.g., *"Lane 2 failed with timeout; the following answer reflects only lanes 1 and 3"*) rather than a silent under-informed answer.

---

## Researcher (round 1)

(researcher lane produced no findings within the iteration budget; gaps remain — see plan)

---

## Critic (round 3)

DECISION: ready

The crashed lane returns an `error` dict without a `research` key, so the additive reducer at `consultants/engine/graph.py:64` appends nothing for that lane; `consultants/engine/graph.py:675-678` shows `synthesizer_node` reads only `state.get("research") or []` and never inspects `error` or `_role_failed`, guaranteeing the LLM synthesizes from partial evidence; and `consultants/server/runner.py:172-199` persists that degraded synthesis with `status: failed` YAML front-matter but a coherent body. No gaps remain.

Hardening: `consultants/engine/graph.py:677` — prepend a tombstone dict carrying `state.get("_role_failed")` and `state.get("error")` into the `research` list fed to `build_synthesizer_messages` so the synthesizer prompt explicitly surfaces the missing lane instead of silently omitting it.

---

## Synthesizer

The council does not crash; it streams to completion, synthesizes from partial evidence, and persists an artifact whose body is a coherent degraded answer while its YAML front-matter marks `status: failed`.

1. **CouncilState additive reducers.** The schema merges parallel lanes with `operator.add` on `research` (`consultants/engine/graph.py:64`) and `turns` (`consultants/engine/graph.py:68`). `error` and `_role_failed` are bare `Optional[str]` with no reducer (`consultants/engine/graph.py:74-75`). A crashed lane returns `{"error": "researcher failed: ...", "_role_failed": "researcher"}` (`consultants/engine/council.py:540-541`) and omits `research`. The additive reducer therefore appends nothing for that lane, while `error` is overwritten last-write-wins by the default dict merge.

2. **`compiled.stream` loop.** The runner drives the graph with `for mode, payload in compiled.stream(...)` (`consultants/server/runner.py:137-138`). The surrounding `except Exception` (`consultants/server/runner.py:155`) only catches stream machinery failures, not node-internal exceptions. Because `researcher_node` swallows exceptions internally (`consultants/engine/council.py:530-541`) and returns a partial-state dict, the stream never aborts and continues to the synthesizer.

3. **Synthesizer input messages.** `synthesizer_node` reads `state.get("research") or []` (`consultants/engine/council.py:675`) and never inspects `state.get("error")` or `state.get("_role_failed")`. It passes that list directly to `build_synthesizer_messages` (`consultants/engine/council.py:677`). The synthesizer therefore sees only successful lanes' outputs—no tombstone, no exception object, no indication that a lane is missing.

4. **Artifact writer.** After the stream drains, the runner checks `node_error = final_state.get("error")` (`consultants/server/runner.py:172`) and sets `terminal_status = "failed" if node_error else "completed"` (`consultants/server/runner.py:174`). `write_consultation` (`consultants/engine/storage.py:200-208`) writes `summary.md` via `render_summary` (`consultants/engine/storage.py:117-120`), emitting YAML front-matter `status: failed` and `error: ...` but still writing the synthesizer's `final_answer` into the body. `render_transcript` (`consultants/engine/storage.py:123-158`) iterates `result.turns`, so the crashed lane is silently omitted.

**Outcome.** The council produces a **degraded answer**, not a blank failure. The user receives a `summary.md` whose Markdown body reads like a real result synthesized from incomplete evidence, paired with YAML front-matter declaring `status: failed` and `error: researcher failed: ...`. The transcript silently omits the crashed lane. If all lanes fail, the synthesizer may emit a vacuous or hallucinated answer under the same `failed` header.

**Recommended hardening.** In `consultants/engine/council.py:538-541`, change the exception return in `researcher_node` to inject a tombstone `research` entry and `RoleTurn` so the additive reducers record the failure and the synthesizer sees an explicit gap:

```python
except Exception as e:
    log.exception("researcher_node failed: %s", e)
    err_text = f"(researcher lane failed: {e})"
    return {
        "error": f"researcher failed: {e}",
        "_role_failed": "researcher",
        "research": [err_text],
        "turns": [RoleTurn(
            role="researcher", round=this_round, content=err_text,
            prompt_tokens=0, completion_tokens=0, duration_seconds=0,
        )],
    }
```

This ensures the transcript records the missing lane and the synthesizer prompt contains a visible failure stub instead of a silently under-informed answer.

---
