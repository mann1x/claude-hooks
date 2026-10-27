---
session_id: csl-2026-10-27-1155-5d08
created: "2026-10-27T11:55:00"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "glm-5.3-flash:cloud"
  planner: "glm-5.3-flash:cloud"
  researcher: "glm-5.3-flash:cloud"
  synthesizer: "glm-5.3-flash:cloud"
topology: council
effort: high
duration_seconds: 187.40
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-27/tmp/claude-hooks-bench-bench-baseline-2026-05-07-GKFg
---

**Bottom line: the consultation does NOT crash — it completes with a degraded-but-coherent answer — but it is surfaced as `status=failed` anyway.** The user polling the API sees a hard failure with `error="researcher failed: ..."`, while `/result` returns a fully readable `summary.md` containing a real answer synthesized from the surviving lanes. The failure is contained inside the researcher node before it ever reaches LangGraph.

## 1. CouncilState's additive reducers

- The lane exception never escapes the node. `researcher_node` wraps the entire `run_loop` call in `try/except Exception` (`consultants/engine/council.py:547-555 [no run_loop at this line; line is in researcher_node]`) and returns a **tombstone** instead of raising: `{"error": "researcher failed: {e}", "_role_failed": "researcher", "research": ["(researcher lane failed: {e})"], "research_rounds_used": 1, "turns": [RoleTurn(...)]}` (`council.py:563-573`). The comment at `council.py:557-561` states this is deliberate — a crashed lane must not silently vanish.
- The tombstone's `research` list **concatenates** into the merged state via the additive reducer `research: Annotated[list, operator.add]` (`consultants/engine/graph.py:64`); `turns` likewise (`graph.py:68`), token/round counters sum (`graph.py:69-72`). Surviving lanes' findings are preserved alongside the tombstone.
- `error` and `_role_failed` (`graph.py:74-75`) are plain last-writer-wins keys with no reducer — fine for one failing lane; two simultaneous lane failures would contend on these (see Unverified).
- What actually raises out of `run_loop`: retry-budget exhaustion → `RuntimeError(f"ollama chat HTTP ...")` (`claude_hooks/get_advice/chat_client.py:285-287 [no run_loop at this line; line is in chat]`) or a bare re-raise of `URLError`/`OSError` after the budget (`chat_client.py:302`); a non-string tool result → `TypeError` at `len(output)` in `execute_tool_calls` (`claude_hooks/agent_loop/runner.py:122-126` — no type check or coercion exists; `ToolExecutor = Callable[..., str]` at `runner.py:70` is unenforced). All are `Exception` subclasses → all caught at `council.py:555`.

## 2. runner.py's compiled.stream loop

- `compiled.stream(initial, stream_mode=["updates","values"])` at `consultants/server/runner.py:137-138`; the last `values` payload becomes `final_state` (`runner.py:139-140`).
- A graph-level crash (exception escaping a node) is caught at `runner.py:155-161` → `status="failed"` and `_write_failed_artifacts` with placeholder `final_answer="(consultation failed: {e})"` (`runner.py:224`). **This path is not taken in the single-lane case** because the tombstone contains the exception.
- Normal path: `node_error = final_state.get("error")` and `terminal_status = "failed" if node_error else "completed"` (`runner.py:172-174`). The tombstone's `error` string is present → **the whole session is marked `failed` even though the synthesizer ran and produced a real answer** (`final_answer` read from `final_state` at `runner.py:186`, persisted at `runner.py:199`).

## 3. What the synthesizer actually sees

- `synthesizer_node` builds its messages from `state.get("research") or []` (`council.py:720`) via `build_synthesizer_messages` (`council.py:245-266`), which renders each entry verbatim as `RESEARCHER REPORT (round N)` (`council.py:253-254`).
- So the synthesizer literally sees the string `"(researcher lane failed: HTTP timeout ...)"` as one of its reports, interleaved with the surviving lanes' real findings. It is **never told** which entries are tombstones — it must infer degradation from the text — and it never reads `state["error"]` or `_role_failed`. It still writes an answer over the surviving lanes' evidence.

## 4. storage.py artifact writer

- The degraded consultation is **fully persisted** via the same success path (`runner.py:177-199` → `storage.write_consultation`, `consultants/engine/storage.py:200-208`): atomic `summary.md` / `transcript.md` / `metadata.json`. YAML front-matter carries `status: failed` + `error:` (`storage.py:108-110`); the body is the real `final_answer` verbatim (`storage.py:119`). The tombstone turn also appears in `transcript.md` (`storage.py:129-136`).

## Perceived failure mode

The caller polling status sees `status=failed` with `error="researcher failed: ..."`; the result endpoint returns a coherent, mostly-correct `summary_markdown` plus metadata that says `failed` (endpoint shapes: `consultants/server/app.py:61-75 [no failed at this line; line is in public_dict], 266-279` — report-sourced, see Unverified). Downstream consumers keying on `status` record a usable consultation as a total failure — a contradictory signal, not a crash.

## One hardening change

`consultants/server/runner.py:174` — replace the unconditional `terminal_status = "failed" if node_error else "completed"` with lane-aware degradation: when a lane-level tombstone exists (`final_state.get("_role_failed")`) and the synthesizer produced a non-empty real `final_answer`, set `terminal_status = "completed"` while retaining `error` (logged at `runner.py:202-206`); reserve `status="failed"` for a synthesizer failure (placeholder `"(consultation incomplete: ...)"`, `council.py:739-741`) or a graph crash (`runner.py:155-161, 224`), where no real answer exists. Both research lanes independently converged on this exact change.

## Unverified

- **Two lanes failing in the same superstep**: likely `InvalidUpdateError` from concurrent writes to the reducer-less `error` channel (`graph.py:74-75`) → graph crash path → true `failed`. Not checked against the installed langgraph version; no test covers it. Outside the question's single-lane premise.
- **app.py endpoint shapes/line numbers** — flagged by the critic as report-sourced; both lanes agreed on the shape but the exact lines were not independently read.
- **`tools.execute` str-coercion** (`claude_hooks/caliber_proxy/tools.py:439`) — only the "never raises" docstring (`tools.py:420-424`) was confirmed; immaterial, since `runner.py:126` rejects non-string returns regardless.

The single-lane path is test-anchored: `tests/test_consultants_council.py:531-556` (`test_loop_failure_emits_tombstone_research`) asserts `"researcher lane failed"` and `"HTTP timeout"` land in `update["research"][0]`.
