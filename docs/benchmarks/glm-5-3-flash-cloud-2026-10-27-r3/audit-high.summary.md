---
session_id: csl-2026-10-27-1222-d927
created: "2026-10-27T12:22:11"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "glm-5.3-flash:cloud"
  planner: "glm-5.3-flash:cloud"
  researcher: "glm-5.3-flash:cloud"
  synthesizer: "glm-5.3-flash:cloud"
topology: council
effort: high
duration_seconds: 123.92
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-27/tmp/claude-hooks-bench-bench-baseline-2026-05-07-WSBV
---

**Bottom line: the consultation does not hard-fail on a lane exception — it produces a coherent, degraded answer — but the user is told `status=failed` anyway, because the runner maps any lane tombstone to a terminal failure. The perceived failure mode is a "failed" stamp on an actually-usable answer.**

**1. CouncilState additive reducers (`consultants/engine/graph.py`)**
The crashed lane never reaches the reducers as an exception. `research`/`turns` are `Annotated[list, operator.add]` channels with int-sum token counters (`consultants/engine/graph.py:64-72`); the fan-out seeds each lane with empty lists/zero counters via `Send("researcher", {...})` (`consultants/engine/graph.py:358-385`). `error` and `_role_failed` are plain last-writer channels (`consultants/engine/graph.py:74-75`) — if two lanes die, only the last tombstone survives in those keys.

**2. The exception is absorbed before the graph sees it**
`researcher_node` wraps the `run_loop` call in `try/except Exception` (`consultants/engine/council.py:547-573 [no run_loop at this line; line is in researcher_node]`) and returns a tombstone: `error: "researcher failed: {e}"`, `_role_failed: "researcher"`, `research: ["(researcher lane failed: {e})"]`, and a zero-token `RoleTurn` (`council.py:562-573`). The comment at `council.py:557-561` says this is deliberate — the fix for an audit finding where a crashed lane made the synthesizer write a confidently-degraded answer over surviving lanes. So the graph continues: critic → synthesizer all run, sibling lanes' results merge normally through the `operator.add` channels. Escape sources confirmed: retry exhaustion raises from `chat_fn` at `claude_hooks/get_advice/chat_client.py:285 [no chat_fn at this line; line is in chat]` (RuntimeError after 8 retries) or `:302` (URLError/OSError re-raise). The question's second scenario — a non-string tool result — **cannot raise in the shipped path**: `claude_hooks/caliber_proxy/tools.py:420-439` documents `execute()` as "Never raises" and coerces via `str(result)` at `tools.py:439`; only a custom executor returning non-str would fail (at `len(output)`, `claude_hooks/agent_loop/runner.py:126`), and that too lands in the same tombstone.

**3. The stream loop (`consultants/server/runner.py`)**
`compiled.stream` runs at `runner.py:136-161`; its `except Exception` (`runner.py:155-161`, `_write_failed_artifacts` at `runner.py:211-235` with placeholder `final_answer="(consultation failed: {exc})"` and empty `turns`) fires only for graph-level crashes (LangGraph internals, reducer crash) — not for a lane exception, which is tombstoned upstream. The lane path flows to `runner.py:172-174`: `node_error = final_state.get("error")`; `terminal_status = "failed" if node_error else "completed"`. Because the tombstone sets `error`, **one dead lane marks the whole consultation `failed`** even though `final_answer` is the synthesizer's genuine answer (`runner.py:186`) and artifacts are written normally (`runner.py:177-199`).

**4. What the synthesizer actually sees**
Not a silent gap: `synthesizer_node` reads `state.get("research") or []` (`council.py:718-723`), and `build_synthesizer_messages` renders every entry as a `RESEARCHER REPORT (round i)` block (`council.py:253-254`) — the tombstone string `"(researcher lane failed: HTTP timeout exhausted retries)"` appears in-band alongside the surviving lanes' real reports. The synthesizer does not read `state["error"]`/`_role_failed` directly; it only sees the marker text. If the synthesizer's own LLM call dies, it returns a placeholder `final_answer="(consultation incomplete: ...)"` (`council.py:727-747`) — that branch, not the lane branch, is the true degraded-to-unusable case.

**5. Artifact writer (`consultants/engine/storage.py`)**
`storage.write_consultation` runs after the stream loop (`runner.py:199`), writing `summary.md`/`transcript.md`/`metadata.json` (not `consultancy.json` — that filename doesn't exist in the shipped path). With a lane tombstone: YAML front-matter gets `status: failed` and the `error` line (`storage.py:108-110`), but the summary body is the real degraded answer (`storage.py:117-120`), and the tombstone turn appears in `transcript.md` via the additive `turns` reducer (`storage.py:129-136`). Only the graph-crash branch loses lane transcripts (`runner.py:211-235`).

**User-visible failure mode:** a coherent answer synthesized from N-1 lanes plus the in-band failure marker, delivered under a misleading `status=failed` stamp (poll endpoint, sessions index, and YAML front-matter all say "failed") — a false-negative status, not silent degradation.

**ONE hardening change — `consultants/server/runner.py:174`:** stop mapping every `node_error` to `"failed"`. The runner already extracts `_role_failed` (`runner.py:173`) but never uses it. Recommend: `terminal_status = "failed" if (node_error and node_failed != "researcher") else "completed"`, keeping `error=node_error` persisted (`runner.py:190`) for observability. A consultation that answered from surviving lanes then reports `completed` with the lane failure noted in artifacts, while a synthesizer crash or graph crash stays `failed`. (Note: the planner's proposed per-lane try/except at the fan-out is already implemented at `council.py:547-573` — do not duplicate it.)

Unverified: the test citations (`tests/test_consultants_council.py:514-560`, `tests/test_consultants_server.py:285-297`) could not be confirmed before budget ran out; the trace above rests on direct code reads only. Also unverified: `storage.py:174-192` atomic-write mechanics (non-load-bearing).
