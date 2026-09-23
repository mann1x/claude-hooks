---
session_id: csl-2026-09-23-2043-ae24
created: "2026-09-23T20:43:40"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "glm-5.3-flash:cloud"
  planner: "glm-5.3-flash:cloud"
  researcher: "glm-5.3-flash:cloud"
  synthesizer: "glm-5.3-flash:cloud"
topology: council
effort: high
duration_seconds: 116.57
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-09-23/tmp/claude-hooks-bench-bench-baseline-2026-05-07-m9iJ
---

**Bottom line: the consultation does not crash — one lane's unhandled exception is caught by the researcher node, converted into a tombstone, and the council still produces a real answer. But the session is paradoxically stamped `status="failed"` end-to-end even though a coherent, degraded answer exists and is served.**

## 1. CouncilState reducers — the exception never reaches them as an exception

- `research`, `turns` are `Annotated[list, operator.add]` and the token/round counters are int-sum reducers (`consultants/engine/graph.py:64-72`); each Send lane is seeded with empty `research`/`turns` (`consultants/engine/graph.py:377-378`), so lane deltas merge additively.
- `error` and `_role_failed` are plain `Optional[str]` channels with **no reducer** (`consultants/engine/graph.py:74-75`).
- The node wrappers are pass-through closures with no try/except (`consultants/engine/graph.py:145-157`, `graph.py:171-180`). `run_loop` itself has no try/except at all — a non-string from `tool_executor` raises TypeError at `len(output)` (`claude_hooks/agent_loop/runner.py:122-126`), and retry-budget exhaustion re-raises from `chat_fn` (`claude_hooks/get_advice/chat_client.py:285-305 [no chat_fn at this line; line is in chat]`, via `claude_hooks/agent_loop/runner.py:230`). `traced_node` logs and re-raises (`consultants/engine/trace.py:179-181 [no traced_node at this line; line is in chat]`), so nothing upstream swallows it.

**The catch point**: `researcher_node` wraps `loop_runner(...)` in `except Exception` and returns a tombstone — `error="researcher failed: {e}"`, `_role_failed="researcher"`, `research=[err_text]`, a `RoleTurn` with the failure text (`consultants/engine/council.py:555-573`; the comment at council.py:557-561 states this exists to prevent a lane failure from discarding surviving lanes' work, per audit csl-…-faa2). So LangGraph never sees an exception from the lane; the graph always continues.

## 2. `compiled.stream` loop in server/runner.py

- `compiled.stream(initial, stream_mode=["updates","values"])` (`consultants/server/runner.py:137-138`); `values` payloads overwrite `final_state` (runner.py:139-141), `updates` flip progress.
- The graph-level `except` at runner.py:155-161 (in `run_council`) only fires if an exception escapes the graph — it sets `status="failed"`, `error="graph crashed: {e}"`, and calls `_write_failed_artifacts` with the placeholder `"(consultation failed: {exc})"` and `turns=[]` (runner.py:211-237). **In that branch, surviving lanes' research is discarded from the artifact.** For a single lane failure this branch does not run.
- The branch that *does* run: `terminal_status = "failed" if node_error else "completed"` (runner.py:172-174). Since the tombstone wrote the `error` channel, `node_error` is truthy → **`status="failed"` despite a full graph completion and a real `final_answer`** (runner.py:186). Same false-failure applies to a critic flake, which also sets `error` while recovering with `critic_decision="ready"` (council.py:681-694).

## 3. What the synthesizer sees

- `build_synthesizer_messages` (council.py:245-266) renders **every** entry of `state["research"]` as `RESEARCHER REPORT (round N)`. Because `research` is `operator.add`, the tombstone string `"(researcher lane failed: <exception>)"` arrives **as a peer researcher report, structurally indistinguishable from real reports** (council.py:253-254; tombstone text at council.py:562, 566).
- The `error` and `_role_failed` channels are **not** passed into the synthesizer's messages, and `SYNTHESIZER_SYSTEM` (council.py:156-168) contains no instruction on interpreting failure markers — whether the final answer acknowledges the gap is left entirely to the LLM's discretion. Worst case: the raw exception string gets woven into the answer as if it were a finding.
- If **all** lanes fail, the synthesizer sees only tombstones and still emits a "final answer" synthesized over pure failure text. (A synthesizer crash itself lands in its own placeholder path, council.py:727-747.)

## 4. storage.py artifact writer

- `write_consultation` (consultants/engine/storage.py:200-208) always writes all three files atomically (`_atomic_write_text`, storage.py:174-192) and writes whatever state it's given. The degraded path yields: real `summary.md` with the synthesizer's answer, real turns, but YAML front-matter carrying `status: "failed"` + the tombstone error (storage.py:108-110).
- There is **no structured per-lane error field** — lane-failure detail survives only as tombstone text inside `research`/turns (ConsultationResult, storage.py:54-72).
- Consumer surface: `/result` serves `summary_markdown` regardless of status (`consultants/server/app.py:246-279`), while the session poll reports `status: "failed"` with `"researcher failed: ..."` (app.py:53-70, runner.py:201-202).

## What the user perceives

A degraded answer: poll/metadata/sessions-index all say `status: "failed"`, yet `/result` serves a real, coherent answer built from the surviving lanes — possibly with a literal `(researcher lane failed: …)` string quoted as evidence. Full failure (no answer) happens only if the exception escapes the node-level catch (e.g., in `_extract_text`, council.py:575), the synthesizer itself crashes, or — caveat, flagged unverified locally — **two+ lanes fail in the same superstep**, where concurrent writes to the reducer-less `error`/`_role_failed` channels (graph.py:74-75) would likely raise LangGraph's `InvalidUpdateError`, hit runner.py:155-161, and produce the placeholder-answer path with surviving work discarded. No test covers the two-lane case, and no test asserts terminal status after a lane tombstone (`tests/test_consultants_council.py:531-555` covers the single-lane tombstone; `tests/test_consultants_council.py:514-529` asserts `error` is set).

## One hardening change

`consultants/server/runner.py:174 [no error at this line; line is in run_council]` — change `terminal_status = "failed" if node_error else "completed"` so that any run with a non-empty `final_answer` and a `_role_failed` value that is **not** `"synthesizer"` reports `status="completed"` with the lane/critic error surfaced as a warning field rather than the terminal status. This beats the alternative (stripping `error` from the tombstone at council.py:564) because it preserves the tombstone's visibility in the transcript/answer, keeps the `error` channel meaningful for genuinely terminal failures, and also fixes the critic-flake false-failure case (council.py:681-694). A regression test asserting `terminal_status` after a single-lane tombstone should accompany it, since that behavior is currently untested.
