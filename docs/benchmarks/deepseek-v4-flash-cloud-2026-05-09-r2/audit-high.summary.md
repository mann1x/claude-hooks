---
session_id: csl-2026-05-09-0756-6bce
created: "2026-05-09T07:56:05"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "deepseek-v4-flash:cloud"
  planner: "deepseek-v4-flash:cloud"
  researcher: "deepseek-v4-flash:cloud"
  synthesizer: "deepseek-v4-flash:cloud"
topology: council
effort: high
duration_seconds: 489.74
status: completed
cwd: /tmp/claude-hooks-bench-bench-baseline-2026-05-07-Nvir
---

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
