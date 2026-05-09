---
session_id: csl-2026-05-09-0657-7477
created: "2026-05-09T06:57:40"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "deepseek-v4-flash:cloud"
  planner: "deepseek-v4-flash:cloud"
  researcher: "deepseek-v4-flash:cloud"
  synthesizer: "deepseek-v4-flash:cloud"
topology: council
effort: high
duration_seconds: 375.02
status: completed
cwd: /tmp/claude-hooks-bench-bench-baseline-2026-05-07-Uaj8
---

The council produces a coherent (partial) answer **and** surfaces as `status="failed"`. The synthesizer runs and writes a plausible answer into `final_answer`, but the runner's status check sees a non-None `"error"` key from the failed researcher lane and sets `status="failed"`. The user perceives a contradiction: a readable answer with an error flag.

---

## Failure-path trace

1. **Exception origin** – Inside `run_loop` (`consultants/engine/agent_loop/runner.py:230`), an unhandled exception (e.g. HTTP timeout after retry exhaustion) propagates uncaught.  
2. **Caught in `researcher_node`** – `council.py:555` catches it and returns a tombstone dict containing `"error": "researcher failed: ..."`, `"_role_failed": "researcher"`, and `"research": ["(researcher lane failed: ...)"]`. The function does **not** re-raise.  
3. **LangGraph merges via additive reducers** – `graph.py:64` declares `research: Annotated[list, operator.add]`, so the failed lane's tombstone text is concatenated with surviving lanes' findings. `error` and `_role_failed` at `graph.py:74-75` are plain `Optional[str]` (no custom reducer), but the critic and synthesizer nodes (`council.py:~680`, `~730`) return dicts that do **not** include the `"error"` or `"_role_failed"` keys – they only return their own output keys (`"critique"`, `"final_answer"`). Therefore LangGraph's default dict-merge reducer **preserves** the failed lane's values (they are never overwritten).  
4. **Synthesizer input** – `build_synthesizer_messages` (`council.py:245-266`) reads `state.get("research") or []` (line 253) and builds the user message with each element as a `RESEARCHER REPORT`. The tombstone string `"(researcher lane failed: ...)"` appears as a report – the synthesizer receives no structured failure signal.  
5. **Stream loop** – `runner.py:136-161` iterates normally; no exception is raised because the failed node returned a dict.  
6. **Status determination** – `runner.py:171-174`: `node_error = final_state.get("error")` → non-None, so `terminal_status = "failed"`.  
7. **Artifact writer** – `runner.py:177-199` writes `ConsultationResult` with `final_answer` from the synthesizer, `status="failed"`, and the error string. `storage.py` writes all three artifacts.

The synthesizer **does** produce a coherent sentence answer. The degradation: the answer may omit or hallucinate findings for the dimension the failed lane was supposed to cover. The user sees `status="failed"` and a plausible answer, creating confusion over trustworthiness.

---

## Recommended hardening change (one)

**Make the synthesizer explicitly aware that a researcher lane failed**, so it can caveat its answer or state gaps. Change `build_synthesizer_messages` to accept an `error` string and inject a warning section just before the final instruction.

`consultants/engine/council.py` line 245 – modify signature to:  
```python
def build_synthesizer_messages(
    question: str,
    plan: str,
    research_rounds: list[str],
    critic: str | None = None,
    self_critic: bool = False,
    research_error: str | None = None,  # new parameter
) -> list[dict]:
```
Then after building `parts` and before appending the final "Now write the answer…" string (around line 257), add:
```python
if research_error:
    parts.append(
        "\n## ⚠️  Partial Failure\n"
        "One or more researcher lanes failed. The failed lane's reports appear as "
        "'(researcher lane failed: ...)'. Do not fabricate evidence for the missing "
        "areas. Explicitly note gaps in your answer."
    )
```
Call it from `synthesizer_node` (`council.py:715-722`) by passing `state.get("error", "")`. This directly addresses the root cause – the synthesizer currently has no structured way to know a lane failed – and requires no schema or reducer changes. The synthesizer will now see the failure signal and produce a more honest, annotated answer.
