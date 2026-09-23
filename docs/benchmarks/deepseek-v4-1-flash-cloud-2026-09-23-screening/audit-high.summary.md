---
session_id: csl-2026-09-23-1812-8c9a
created: "2026-09-23T18:12:16"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "deepseek-v4.1-flash:cloud"
  planner: "deepseek-v4.1-flash:cloud"
  researcher: "deepseek-v4.1-flash:cloud"
  synthesizer: "deepseek-v4.1-flash:cloud"
topology: council
effort: high
duration_seconds: 143.86
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-09-23/tmp/claude-hooks-bench-bench-baseline-2026-05-07-2JCu
---

**Bottom line: the consultation does NOT fail — LangGraph never sees the exception. `researcher_node` catches it and returns a tombstone dict that flows through the additive reducers, the synthesizer runs over (live lanes + tombstone), and a real `final_answer` is written. But `consultants/server/runner.py:174 [no final_answer at this line; line is in run_council]` then stamps `status="failed"` on it, so the user gets a coherent degraded answer wearing a total-failure label.**

**1. Reducers — the exception never reaches them as an exception**
- `research: Annotated[list, operator.add]` `consultants/engine/graph.py:64`, `turns` `:68`, and int-sum `research_rounds_used`/`critic_reroutes_used`/`total_*_tokens` `:69-72`. `error` and `_role_failed` are bare `Optional[str]` with no reducer — last-write-wins `:74-75`.
- The Send payload seeds `research: []`, `turns: []`, `research_rounds_used: 0`, `total_*_tokens: 0` per lane so each lane's delta is additive-only `consultants/engine/graph.py:377-381`; lanes emitted at `:359-384`, conditional edge at `:399-404`.
- **The lane's exception is converted to state *before* the superstep.** `researcher_node` wraps the runner: `try: final = loop_runner(...)` `consultants/engine/council.py:547-554`, `except Exception as e:` `:555` → returns `error` `:564`, `_role_failed="researcher"` `:565`, `research: [err_text]` `:566`, `research_rounds_used: 1` `:567`, `turns` `:568-572`, where `err_text = f"(researcher lane failed: {e})"` `:562`. That is a normal partial-state dict, so reducers fire, sibling lanes and the synthesizer are unaffected, and nothing propagates to the graph. Regression-locked by `tests/test_consultants_council.py:531-561`.

**2. `compiled.stream` loop — the crashing branch is never taken**
- `for mode, payload in compiled.stream(initial, stream_mode=["updates","values"])` `consultants/server/runner.py:137-138`; `values` snapshots become `final_state` `:139-141`. The loop drains cleanly.
- The outer `except Exception` `consultants/server/runner.py:155-161` (→ `state.status = "failed"` `:157`, `_write_failed_artifacts` `:160`, `return` `:161`) **is dead code on this path** — the node already turned the failure into a state update.
- Post-drain: `node_error = final_state.get("error")` `:172`, `node_failed = final_state.get("_role_failed")` `:173`, and the binary `terminal_status = "failed" if node_error else "completed"` **`:174`**. One tombstone sets `error`, so `terminal_status` flips to `"failed"` even though `final_state.get("final_answer", "")` `:186` is populated.

**3. What the synthesizer sees**
- Its only state read is `build_synthesizer_messages(state["question"], state.get("plan",""), state.get("research") or [], state.get("critique"), ...)` `consultants/engine/council.py:718-723`. It never inspects `error` or `_role_failed`.
- `build_synthesizer_messages` formats each entry verbatim as `RESEARCHER REPORT (round {i}): {r.strip()}` `consultants/engine/council.py:253-254`. So a 3-lane fan-out with 1 crash yields 2 real reports **plus one report whose text is literally `(researcher lane failed: <exception>)`** — a visible, labelled gap. Note the synthesizer is unguarded against a crash of its own `_single_shot` `:726`; that case returns `_role_failed="synthesizer"` with placeholder `final_answer=f"(consultation incomplete: synthesizer error: {e})"` `consultants/engine/council.py:735-741`, which is the genuinely terminal one.

**4. Storage artifact writer**
- `write_consultation` is called on the success path only, not in a `finally` — `consultants/server/runner.py:199`; atomic tmp+`os.replace` internals per the researcher report at `consultants/engine/storage.py:200-208 [no replace at this line; line is in write_consultation]`.
- Front-matter emits `status: <result.status>` `consultants/engine/storage.py:108` and, when `result.error` is truthy, `error: "<msg>"` `:109-110`; `render_summary` puts `result.final_answer.rstrip()` under that front-matter `:117-120`.
- Net artifact: **a readable, correctly-scoped answer body with `status: failed` + `error: researcher failed: ...` in the YAML.** Same value propagates to `SessionState` via `state.status = terminal_status` `consultants/server/runner.py:201 [no SessionState at this line; line is in run_council]` / `state.error = node_error` `:202` and into `metadata.json`.

**User-perceived failure mode:** an over-flagged partial success. The answer is degraded-but-honest (the tombstone is in `research`, so synthesis is labelled, not silently wrong), yet any consumer that gates on `status` treats a 2-of-3-lane answer as a total failure. Secondary defect worth knowing: because `error`/`_role_failed` are bare `graph.py:74-75`, two concurrent lane failures collapse — the second tombstone overwrites the first, so only one failure is recorded.

**Recommendation (one change):** rewrite the binary mapping at **`consultants/server/runner.py:174 [no final_answer at this line; line is in run_council]`** to gate on whether a usable answer exists and whether the *terminal* node failed:

```python
answer = (final_state.get("final_answer") or "").strip()
terminal_status = "failed" if (not answer or node_failed == "synthesizer") else "completed"
```

This keeps the two-value schema (`storage.py` documents only `completed`/`failed`; no change to app/UI status enums), still flows `error` and `_role_failed` into `metadata.json` diagnostics `consultants/server/runner.py:189-190 [no json at this line; line is in run_council]`, and only calls it failed when the synthesizer actually died (`consultants/engine/council.py:738`) or no answer was produced. Prefer this over introducing a new `"degraded"` status, which would require widening the status vocabulary in the session/state layer and every downstream `status` consumer.

Also note for completeness: the exception origin is genuinely unguarded upstream — `final = chat_fn(payload)` sits inside a bare `for i in range(config.max_iterations)` with no try `claude_hooks/agent_loop/runner.py:215`, `:230`; non-string tool returns are not coerced (`output = tool_executor(...)` `:122`, then `len(output)` `:126` raises `TypeError`). The retry-exhausted HTTP raise is at `claude_hooks/get_advice/chat_client.py:285-287` (relayed from research; not independently re-read). The `researcher_node` tombstone is the only thing standing between those and a real abort.
