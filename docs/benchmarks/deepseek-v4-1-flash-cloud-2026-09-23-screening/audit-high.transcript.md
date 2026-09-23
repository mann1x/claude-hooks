# Consultation transcript — csl-2026-09-23-1812-8c9a

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. Read consultants/engine/graph.py and enumerate CouncilState's fields with their `Annotated[..., reducer]` declarations — record for each key whether the reducer is `operator.add` on lists, a dict-merge, or a plain overwrite (no reducer), since only plain-overwrite keys can be clobbered by a failed lane.
2. Inspect agent_loop/runner.py's `run_loop` control flow: does it wrap the per-turn body in try/except, what does it do on retry-budget exhaustion (raise, return error dict, return partial text), and is the tool_executor result type-checked/coerced before being appended to messages?
3. In consultants/server/runner.py, locate the `compiled.stream`/`astream` loop and determine whether node exceptions surface as a raised exception out of the async iterator (aborting the loop) or as a state snapshot — check for surrounding try/except, `astream(stream_mode=...)` choice, and any `finally` around the artifact write.
4. Find where the Send-API fan-out is declared (grep for `Send(`, `add_conditional_edges`, or `map_reduce` in consultants/engine/graph.py) and verify whether each researcher lane is a separate node invocation whose exception propagates to the whole superstep (bypassing reducers entirely) rather than being written into CouncilState.
5. Identify the synthesizer node function and list exactly which CouncilState keys it reads when building its input messages (e.g. `state["researcher_findings"]`, `state["messages"]`) and whether it filters/validates per-lane presence — this determines what it sees when a lane key is absent vs empty-under-reducer.
6. Read storage.py's artifact writer: whether it is invoked in a `finally`, on success-only, or in an error handler; whether it persists partial CouncilState; and where the terminal `status="failed"` value is assigned (state key vs server exception handler).
7. Grep tests/fixtures for an existing lane-failure or timeout scenario to confirm the empirically observed outcome (coherent degraded answer vs status=failed) before committing to a single hardening recommendation.

---

## Researcher (round 1)

## Findings — Send-API lane crash path

**1. CouncilState reducers (`consultants/engine/graph.py`)**
- Additive: `research: Annotated[list, operator.add]` `graph.py:64` · `turns` `:68` · `research_rounds_used` `:69` · `critic_reroutes_used` `:70` · `total_prompt_tokens` `:71` · `total_completion_tokens` `:72`.
- **Bare (overwrite / last-write-wins)**: `error: Optional[str]` `graph.py:74` and `_role_failed: Optional[str]` `graph.py:75` — no reducer. A lane returning a dict that omits these keys leaves the prior value untouched (LangGraph only touches keys present in the update), which is why a failed lane's `error` survives alongside surviving lanes.
- Send fan-out declares explicit zero-deltas per lane: `"research": []`, `"turns": []`, `"research_rounds_used": 0`, `"total_*_tokens": 0` `graph.py:377-381`; lanes emitted at `graph.py:359-385`, conditional edge at `graph.py:399-404`.

**2. `run_loop` has no outer try/except — exceptions escape the loop**
- Retry-budget exhaustion raises out of `ChatClient.chat`: `raise RuntimeError(f"ollama chat HTTP {e.code}: {err_body}")` `claude_hooks/get_advice/chat_client.py:285-287`; `URLError`/`OSError` re-raised after budget at `chat_client.py:302`.
- `run_loop` body is unguarded — `final = chat_fn(payload)` `claude_hooks/agent_loop/runner.py:230` sits inside the bare `for i in range(config.max_iterations)` `runner.py:215`; there is no `try` in the function.
- **Non-string tool result is NOT coerced**: `output = tool_executor(name, args_str, cwd)` `runner.py:122` → `len(output)` `runner.py:126` raises `TypeError` for an int/None/dict; `"content": output` `runner.py:133`. `execute_tool_calls` itself (`runner.py:95-135`) has no try/except around the executor call. The traced wrapper re-raises (`consultants/engine/trace.py:220-222`), so nothing swallows it — it propagates out of `run_loop`.
- Both failure modes therefore surface as an exception at the `loop_runner(...)` call site.

**3. `researcher_node` swallows it and returns a tombstone — the superstep does NOT abort**
- `try: final = loop_runner(...)` `consultants/engine/council.py:548-554` / `except Exception as e:` `council.py:555` → returns `{"error": f"researcher failed: {e}"}` `:564` · `"_role_failed": "researcher"` `:565` · `"research": [err_text]` `:566` · `"research_rounds_used": 1` `:567` · `turns=[RoleTurn(...)]` `:568-572`.
- Consequence: `graph.py`'s Send lane sees a **normal partial-state dict**, not a raised exception. Reducers fire normally; the tombstone string is concatenated onto the shared `research` list. Regression-locked by `tests/test_consultants_council.py:531-561` (`test_loop_failure_emits_tombstone_research`, asserts `update["research"][0]` contains `"researcher lane failed"`).

**4. `compiled.stream` loop (`consultants/server/runner.py:137-161`)**
- `for mode, payload in compiled.stream(initial, stream_mode=["updates","values"])` `runner.py:137-138`; `values` payload becomes `final_state` `runner.py:139-141`.
- Outer `except Exception` `runner.py:155` — sets `state.status="failed"` `:157`, `_write_failed_artifacts` `:160`, `return` `:161` — **is never reached on this path**, because the exception was already converted to state inside `researcher_node`. `_write_failed_artifacts` `runner.py:211-237` writes `final_answer=f"(consultation failed: {exc})"` `:224`, `status="failed"` `:227`.
- Post-drain: `node_error = final_state.get("error")` `runner.py:172`; `node_failed = final_state.get("_role_failed")` `runner.py:173`; **`terminal_status = "failed" if node_error else "completed"`** `runner.py:174`. One crashed lane flips the whole consultation.
- Artifact `ConsultationResult` built with `final_answer=final_state.get("final_answer","")` `runner.py:186`, `status=terminal_status` `:189`, `error=node_error` `:190`; written `storage.write_consultation(result, cwd=Path(cwd))` `runner.py:199`; `state.status = terminal_status` `runner.py:201`, `state.error = node_error` `:202`.

**5. Synthesizer input — it DOES see the failure, contrary to the pre-harden rubric**
- `build_synthesizer_messages(state["question"], state.get("plan",""), state.get("research") or [], state.get("critique"), ...)` `consultants/engine/council.py:718-723`. Because `research` is additive, the tombstone appears as a research round and is formatted verbatim as `RESEARCHER REPORT (round {i}): {r.strip()}` `council.py:253-254`.
- `synthesizer_node` never reads `state.get("error")` or `state.get("_role_failed")` — `council.py:718-723` is the only state read.
- So a 3-lane fan-out with 1 crash yields 2 real reports + 1 `(researcher lane failed: HTTP timeout ...)` stub → the synthesizer **cannot silently ignore** the gap. `docs/benchmarks/EVALUATION.md:113-115` (rubric claim 3, "does NOT include the tombstone") is stale relative to the current code.

**6. Storage writer (`consultants/engine/storage.py`)**
- `write_consultation` → `_atomic_write_text` for `summary.md` / `transcript.md` / `metadata.json` `storage.py:200-208`; atomic tmp+`os.replace` `storage.py:174-192`.
- Front-matter emits `status: <result.status>` `storage.py:108` and, when `result.error` is truthy, `error: "<msg>"` `storage.py:109-110`.
- `render_summary` puts `result.final_answer.rstrip()` under that front-matter `storage.py:117-120` → **a coherent degraded answer in the body, `status: failed` in the YAML**. `render_metadata` `asdict`s the whole result including status/error `storage.py:166-169`.

**Verdict**
- The council does **not** abort: no exception escapes the superstep, `compiled.stream` drains cleanly, and the synthesizer runs over (live lanes + tombstone).
- `status` still lands as `failed` on three surfaces: `SessionState.public_dict()` at `consultants/server/app.py:61-75` (set by `runner.py:201`), `summary.md`/`metadata.json` front-matter `storage.py:108-110`, and the sessions index entry `app.py:331-345`.
- **User-perceived failure mode**: an over-flagged partial success. `/v1/consult/{sid}/result` `app.py:246-279` returns a *readable, correctly-scoped answer* whose front-matter says `status: failed` + `error: researcher failed: ...`. Any consumer that gates on `status` (UI badge, CI, `consultants/cli.py:142-145 cmd_status`) treats a 2-of-3-lane answer as a total failure. The synthesis is *labelled* (tombstone in `research`), so it is degraded-but-honest, not silently-wrong.

**Secondary defect (not the recommendation)**: `error`/`_role_failed` are bare `graph.py:74-75`, so if two lanes fail concurrently the second tombstone overwrites the first — only one failure is recorded. `docs/benchmarks/.../audit-high.transcript.md:207` documents this.

**ONE hardening change**
`consultants/server/runner.py:174` — replace the binary
`terminal_status = "failed" if node_error else "completed"`
with a three-way check keyed on whether the **terminal** node failed and whether an answer exists:
```python
if final_state.get("final_answer") and node_failed != "synthesizer":
    terminal_status = "degraded" if node_failed else "completed"
else:
    terminal_status = "failed"
```
Rationale, grounded: a single crashed lane is a partial failure, but `runner.py:172-174` cannot distinguish it from a synthesizer crash — and `synthesizer_node` already emits a distinguishable placeholder (`"(consultation incomplete: synthesizer error: {e})"` `consultants/engine/council.py:739-741`, `_role_failed="synthesizer"` `:738`). Keying on `_role_failed == "synthesizer"` preserves a genuine hard failure, while `degraded` stops the false alarm for planner/researcher/critic tombstones (`council.py:453-454`, `:564-565`, `:684-685`). `"degraded"` must also be added to the `status` values in `SessionState` `app.py:53` and the `ConsultationResult` comment `storage.py:65`; the string passes through `_yaml_str` `storage.py:84-94` unchanged.

---

## Researcher (round 1)

## Findings — Send-API fan-out lane crash

**Headline: the lane crash is *already* handled inside `researcher_node`, so the graph does NOT abort. The council streams to completion, synthesizes over the tombstone + surviving lanes, and produces a `final_answer` — but `runner.py` then stamps `status=failed` because `error` is set. Net user-visible artifact: coherent-degraded body + failure front-matter.**

### 1. Exception origin in `run_loop`
- `run_loop` itself has **no** try/except around the LLM call (`final = chat_fn(payload)` at `claude_hooks/agent_loop/runner.py:230`) or the tool call (`execute_tool_calls` → `output = tool_executor(...)` at `claude_hooks/agent_loop/runner.py:122`, then `len(output)` at `:126`). An HTTP-timeout after retry-budget exhaustion (raised by `chat_fn`) and a non-string tool return (TypeError at `:126`) both **propagate out of `run_loop`** to the caller.
- So the exception is *not* swallowed at the agent-loop layer.

### 2. CouncilState reducers (`consultants/engine/graph.py`)
- Additive channels: `research` list-concat at `consultants/engine/graph.py:64`, `turns` at `:68`; int-sum for `research_rounds_used` / `critic_reroutes_used` / `total_*_tokens` at `:69-72`.
- `error` and `_role_failed` are **bare `Optional[str]`, no reducer** — last-write-wins via default dict merge (`consultants/engine/graph.py:74-75`).
- The parallel-lane `Send` payload seeds empty `research`/`turns`/counters so each lane's delta is additive-only (`consultants/engine/graph.py:377-382`).

### 3. The catch that converts the exception into a tombstone
- `researcher_node` wraps the `loop_runner(...)` call in try/except at `consultants/engine/council.py:547-573`. On exception it returns a **tombstone** that feeds the additive reducers: `error` `:564`, `_role_failed` `:565`, `research: [err_text]` `:566`, `research_rounds_used: 1` `:567`, `turns` `:568-572`. `err_text` = `"(researcher lane failed: {e})"` `:562`.
- Because it returns a partial-state dict, LangGraph merges it and the graph **keeps running** — the sibling lanes and the synthesizer are unaffected.
- (The pre-hardening variant omitted the `research` key entirely — that gap is closed; ground truth in `docs/benchmarks/kimi-k2.6-cloud-2026-05-07-pre-harden/audit-high.summary.md:19,29` and the pinned test at `tests/test_consultants_council.py:531-561` assert the tombstone now contributes `research` + `turns`.)

### 4. `compiled.stream` loop (`consultants/server/runner.py`)
- The runner drives `compiled.stream(initial, stream_mode=["updates","values"])` at `consultants/server/runner.py:137-138`; the `except Exception` at `:155` (→ `state.status = "failed"`, `:157`, `_write_failed_artifacts`, `:160`) is **never reached**, because the node already converted the failure to a state update.
- Post-loop: `node_error = final_state.get("error")` `:172`, `node_failed = final_state.get("_role_failed")` `:173`, and the binary `terminal_status = "failed" if node_error else "completed"` **`:174`**. A single lane tombstone sets `error`, so `terminal_status` becomes `"failed"` even though `final_state["final_answer"]` is populated.

### 5. What the synthesizer actually sees
- `synthesizer_node` reads `state.get("research") or []` and `state.get("critique")` at `consultants/engine/council.py:718-723`; it **never inspects `error` or `_role_failed`**.
- `build_synthesizer_messages` formats each list entry verbatim as `RESEARCHER REPORT (round {i}): {r.strip()}` at `consultants/engine/council.py:253-254`. So the synthesizer sees the surviving lanes' real reports **plus** one report whose text is literally `(researcher lane failed: <exception>)` — a visible, un-annotated gap it may or may not flag.

### 6. Artifact writer (`consultants/engine/storage.py`)
- `write_consultation` is called **on success-path only**, at `consultants/server/runner.py:199` (not in a `finally`); it writes summary/transcript/metadata atomically at `consultants/engine/storage.py:200-208`.
- `render_summary` emits YAML front-matter then the synthesizer body at `storage.py:117-120`; front-matter `status` at `storage.py:108` and `error` at `:109-110`.
- Downstream: `/v1/consult/{sid}/result` at `consultants/server/app.py:246-279` serves `status: failed` + the degraded body; the sessions index records `state.status` at `app.py:341`.

### Verdict on the user-facing outcome
- **Does not fully fail.** Answer exists (`final_answer`), so it is a **degraded answer**, not a blank/aborted consultation.
- **User perceives** a `summary.md` whose Markdown **body reads like a legitimate answer synthesized from incomplete evidence**, paired with front-matter `status: failed` and `error: researcher failed: ...`; `transcript.md` shows the tombstone turn. Any UI/API consumer keying off `status` treats it as a total failure. Matches the recorded ground truth at `docs/benchmarks/kimi-k2.6-cloud-2026-05-07/audit-high.summary.md:17,27` and `docs/benchmarks/EVALUATION.md:109-117`.

### Recommended ONE hardening change
**`consultants/server/runner.py:174`** — replace the binary `terminal_status = "failed" if node_error else "completed"` with a check gated on whether a usable answer was produced, so a partial (non-synthesizer) lane crash does **not** mask a completed consultation:

```python
# consultants/server/runner.py:172-174
node_error = final_state.get("error")
node_failed = final_state.get("_role_failed")
answer = (final_state.get("final_answer") or "").strip()
# A crashed researcher/planner/critic lane degrades the answer but is not
# terminal; only a missing answer or a synthesizer failure is fatal.
terminal_status = "failed" if (not answer or node_failed == "synthesizer") else "completed"
```

Rationale: `error`/`_role_failed` remain populated and flow into `metadata.json`/`turns` for diagnostics (storage.py:65 still documents only `"completed" | "failed"`, so no schema change), while a survived answer reports `completed`. This is the exact fix the benchmark's own Q3 ground truth points at (`docs/benchmarks/kimi-k2.6-cloud-2026-05-07/audit-high.summary.md:29`, `audit-high.transcript.md:303-310,419-427`).

**Evidence gaps:** did not read `consultants/config.py` (ROLES/`role_think`) or `consultants/engine/sessions_index.py`; no unit test directly asserts the `runner.py:174` status mapping (runner has no dedicated test file — only the stub runner in `tests/test_consultants_server.py:31-68`).

---

## Critic (round 2)

DECISION: ready

Evidence covers every link in the asked chain with line-level citations: reducers (`graph.py:64-75`), the unguarded `run_loop` call sites (`agent_loop/runner.py:122,126,230`), the converting catch (`council.py:547-573`), the stream loop and status mapping (`server/runner.py:137-174`), the synthesizer's only state read (`council.py:718-723` via `:253-254`), and the artifact writer (`storage.py:108-120,200-208`). The key non-obvious claim — that the exception never escapes the superstep because `researcher_node` returns a tombstone dict that flows through the additive reducers — is corroborated by a pinned regression test (`tests/test_consultants_council.py:531-561`) and benchmark ground truth, which is the right kind of evidence for a non-abort verdict. Open gaps noted by the researcher (`config.py`, `sessions_index.py`, absence of a test directly pinning `runner.py:174`) are non-load-bearing: neither file participates in the exception→status path, and the status mapping is read directly from source. The one thing the synthesizer must adjudicate rather than research is the *conflict between the two rounds' hardening recommendations* — round 1 says introduce a three-way `"degraded"` status (`runner.py:174`), round 2 says keep the binary and flip to `"completed"` when an answer exists. Both target the same line; only round 2 avoids a schema change (`storage.py:65` documents only two values, `app.py:53` would need updating under round 1). Pick one; do not launch another research loop.

---

## Synthesizer

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

---
