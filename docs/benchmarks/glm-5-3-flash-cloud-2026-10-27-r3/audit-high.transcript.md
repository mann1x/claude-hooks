# Consultation transcript — csl-2026-10-27-1222-d927

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

I need to write the plan now. I have confirmed: `consultants/engine/graph.py`, `consultants/engine/storage.py`, `consultants/engine/council.py`, `consultants/server/runner.py`, `claude_hooks/agent_loop/runner.py` (from survey), `claude_hooks/_parallel.py`. I did not read the file contents, so the plan steps must include verification of symbols and line numbers.

Plan steps for the researcher:

1. Read `consultants/engine/graph.py` — find the Send-API fan-out (likely `send_command`/parallel researcher lanes), the CouncilState additive reducers (e.g. reducers merging lane results into state), and how an exception from a lane propagates or is caught (look for try/except around `agent_loop.runner.run_loop` invocation, and whether LangGraph-style `Command`/Send nodes use `add_messages` reducers that would swallow or raise). Cite path:line.

2. Read `consultants/server/runner.py` — find `compiled.stream(...)` loop, check for try/except around each streamed update, what happens on exception (re-raise → status=failed, or caught → partial state), and where `status` ("completed"/"failed") is written (probably to consultancy.json via storage.py). Cite path:line.

3. Read `claude_hooks/agent_loop/runner.py` — locate `run_loop` and `tool_executor`; confirm what exception types escape (HTTP timeout retry budget in provider call, non-string tool result assertion/TypeError) and whether run_loop itself has internal try/except that converts errors to string content vs. propagates. Cite path:line.

4. Read `consultants/engine/council.py` — the synthesizer node: what input messages it builds (does it read all lane results from state, or only completed ones?), what happens when a lane's result is absent from state (does the reducer leave the lane's key missing, and does the synthesizer prompt include a placeholder/error marker?). Cite path:line.

5. Read `consultants/engine/storage.py` — artifact writer: what gets persisted when an exception interrupts the stream (partial consultancy.json? status=failed? orphan session dir under `.claude-hooks/consultants/`), and whether the final answer is written before or after the stream loop. Cite path:line.

6. Cross-check `consultants/engine/trace.py` and `consultants/engine/sessions_index.py` for how failures are recorded/traced (optional but cheap). Cite path:line.

7. Verify empirically: check tests — `tests/` for consultant engine tests (e.g. grep for `run_loop` mock raising, `status.*failed` in tests) to confirm intended semantics; if no test covers a lane exception, note the gap.

Then final answer must trace the four numbered questions and recommend exactly ONE hardening change with path:line.

I should write the numbered list only, nothing else. Each step one sentence, pointing at where to look. Note unchecked items inside steps.</think>1. Read `consultants/engine/graph.py` end-to-end: locate the Send-API fan-out (the node that returns `Send(...)`/parallel lane commands), the CouncilState reducer definitions (likely `Annotated[..., operator.add]` or custom additive reducers on lane-result keys), and any try/except wrapping the per-lane call into `claude_hooks/agent_loop/runner.py:run_loop` — record whether a raised exception aborts the super-step (LangGraph re-raises and discards the whole batch, including sibling lanes' completed results) or is caught per-lane; cite path:line for each (file contents not yet read — verify symbol names, don't assume).

2. Read `consultants/server/runner.py`: find the `compiled.stream(...)` loop and determine what a node exception does there — does the loop have a try/except that converts the error into a `status="failed"` record, does it re-raise to the FastAPI handler in `consultants/server/app.py`, and does it still write partial artifacts; cite path:line.

3. Read `claude_hooks/agent_loop/runner.py` (`run_loop` and `tool_executor`): confirm which exceptions actually escape — retry-budget exhaustion on the provider HTTP call (find the retry wrapper, likely in `claude_hooks/` provider/transport code — locate it) and the type error from a non-string tool result (check whether `tool_executor` asserts/casts to str or returns raw) — and whether `run_loop` catches internally and returns an error-string content instead of raising; cite path:line.

4. Read `consultants/engine/council.py` synthesizer node: reconstruct the exact input messages it builds from CouncilState after a lane dies — is the dead lane's key missing entirely (synthesizer sees N-1 lanes, no placeholder), an error marker string injected by a reducer, or does the synthesizer never run because the exception aborted the graph — and quote the synthesizer prompt construction lines; cite path:line.

5. Read `consultants/engine/storage.py`: determine what the artifact writer persists on failure — whether `consultancy.json` (e.g. under `.claude-hooks/consultants/<session-id>/`) is written with `status="failed"`, written partially mid-stream, or not written at all, and whether per-lane transcripts survive for the dead lane; cite path:line (cross-check an existing session dir `.claude-hooks/consultants/csl-2026-10-27-1220-58b1/consultancy.json` for the real schema).

6. Grep `tests/` for existing coverage of this failure path (patterns: `run_loop` raising/mocked to raise, `side_effect=`, `status.*failed`, `Send(`) in consultant engine/server tests — if none exists, state that the degraded-vs-failed answer rests on the code read alone; cite the test file or report the empty grep.

7. Synthesize the verdict: from steps 1–5, decide the binary question (coherent degraded answer vs. whole consultation `status=failed`), name the user-visible failure mode for the degraded case (e.g. answer silently missing one perspective with no error surfaced), and propose exactly ONE hardening change with path:line — most likely a per-lane try/except at the fan-out node in `consultants/engine/graph.py` that injects a lane-error marker into state instead of raising, but only commit to that if step 1 confirms the exception currently escapes the node.

---

## Researcher (round 1)

## Findings

**1. Fan-out + reducers (`consultants/engine/graph.py`)**
- CouncilState declares additive reducers: `research: Annotated[list, operator.add]`, `turns`, `research_rounds_used`, `total_*_tokens` (graph.py:64-72); Send payload seeds each lane with empty `research`/`turns`/zero counters so deltas don't double-count (graph.py:375-381).
- Fan-out router: `council.group_items_into_lanes` → list of `Send("researcher", {...})` (graph.py:349-386); conditional edge replaces planner→researcher (graph.py:399-404).
- **No try/except at the graph layer** — but the exception never reaches LangGraph's super-step barrier: `researcher_node` wraps `run_loop` in its own try/except (see below). Sibling lanes' results are therefore not discarded; each crashed lane contributes a tombstone delta that the `operator.add` reducers concat.

**2. Lane exception handling (`consultants/engine/council.py`)**
- `researcher_node` calls `loop_runner(...)` inside try/except at council.py:547-573. On ANY exception it returns a tombstone: `error: "researcher failed: {e}"`, `_role_failed: "researcher"`, `research: [f"(researcher lane failed: {e})"]`, plus a zero-token `RoleTurn` (council.py:562-573). Comment explicitly says this was the audit-high (csl-...-faa2) fix for "synthesizer writes a confidently-degraded answer over surviving lanes".
- What escapes `run_loop` itself: `chat_fn` failures propagate — `ChatClient.chat` re-raises after exhausting `max_retries` (chat_client.py:285 `raise RuntimeError(...)`, chat_client.py:302 `raise` for URLError/OSError, chat_client.py:305). `run_loop` has no internal try/except around `chat_fn(payload)` (runner.py:230) or `tool_executor` (runner.py:122 via `execute_tool_calls`) — both propagate to the node wrapper. `tool_executor` non-string is actually impossible in the shipped path: `caliber_proxy/tools.py:439` coerces `return result if isinstance(result, str) else str(result)` and never raises (tools.py:436-438); a custom executor returning non-str still flows through `execute_tool_calls` unvalidated (runner.py:122-133) and would only fail later at `len(output)` in logging (runner.py:126) — inside the same try/except.

**3. `compiled.stream` loop (`consultants/server/runner.py`)**
- Stream loop at runner.py:137-154: `"values"` mode captures `final_state` after each superstep (runner.py:139-141); `"updates"` flips `state.progress[node] = "done"`. Outer `except Exception` at runner.py:155-161 sets `state.status = "failed"`, `state.error = f"graph crashed: {e}"`, writes `_write_failed_artifacts` and returns — but this branch only fires for graph-level crashes (e.g. langgraph internal, planner conditional-edge failure), NOT for a lane exception, which is absorbed upstream.
- Post-stream: `node_error = final_state.get("error")` → `terminal_status = "failed" if node_error else "completed"` (runner.py:172-174). **Key semantic: ANY lane tombstone sets `error` in state → the whole consultation is persisted as `status="failed"` even though the synthesizer still runs and produces a real answer.**

**4. Synthesizer input (`consultants/engine/council.py`)**
- `synthesizer_node` reads `state.get("research") or []` (council.py:720) — the additive reducer leaves no missing key; a crashed lane's tombstone `"(researcher lane failed: HTTP timeout exhausted retries)"` IS in the list, so the synthesizer sees the failure marker inline as `RESEARCHER REPORT (round N)` (council.py:253-254). It does not receive `state["error"]`/`_role_failed` — only the tombstone text. Synthesizer's own LLM failure → placeholder `final_answer="(consultation incomplete: synthesizer error: ...)"` (council.py:739-741).

**5. Artifact writer (`consultants/engine/storage.py`)**
- `write_consultation` runs AFTER the stream loop (runner.py:199), atomically (`.tmp` + `os.replace`, storage.py:174-192) writing `summary.md`/`transcript.md`/`metadata.json` under `.claude-hooks/consultants/<sid>/` (storage.py:200-208). With a lane failure: `status: failed` in front-matter (storage.py:108), `error` line present (storage.py:109-110), but `final_answer` is the synthesizer's REAL degraded answer (runner.py:186), and the tombstone turn appears in transcript.md via the additive `turns` reducer (storage.py:129-136). Graph-crash path writes `final_answer="(consultation failed: {exc})"`, empty turns (runner.py:224-232).

**6. Failure recording elsewhere**
- `traced_tool` re-raises after recording the error span (trace.py:220-222); `TracedChat` likewise (trace.py:179-181). `app.py` `_run_with_state` catches runner-level crashes → `state.status="failed"`, appends to `sessions_index` with terminal status (app.py:320-344). Test coverage exists for the lane-tombstone path: `tests/test_consultants_council.py:531-555` (`test_loop_failure_emits_tombstone_research`, "HTTP timeout exhausted retries") and runner-crash → failed at `tests/test_consultants_server.py:285-296`.

**Answer to the trace question:** The council does NOT fail hard on a lane exception. `researcher_node`'s tombstone (council.py:555-573) keeps the graph alive; surviving lanes' results merge normally; the synthesizer sees the tombstone text and produces a real (degraded) answer. However, runner.py:172-174 maps any `state["error"]` to `terminal_status="failed"`, so the user gets `status=failed` metadata + a `error:` line in summary.md front-matter **while the summary body still contains a coherent answer** — a misleading "failed" stamp on a usable answer. The only hard-fail paths are graph-level crashes (runner.py:155-161) or synthesizer-node LLM failure (placeholder answer, still status=failed).

**ONE hardening change:** In `consultants/server/runner.py:174`, don't blanket-map `node_error` → `"failed"`. Distinguish lane-level (recoverable) from terminal failures — e.g. treat `final_state.get("final_answer")` non-empty and not matching the placeholder pattern as `"completed_with_errors"`, reserving `"failed"` for missing/placeholder `final_answer` (synthesizer crash, council.py:739) or graph crashes. Single-line anchor: `consultants/server/runner.py:174` (`terminal_status = "failed" if node_error else "completed"`).

---

## Researcher (round 1)

## Findings

**1. CouncilState reducers (`consultants/engine/graph.py`)**
- Additive channels: `research: Annotated[list, operator.add]`, `turns: Annotated[list, operator.add]`, `research_rounds_used` / `total_*_tokens` int-sum (graph.py:64-72). `error` and `_role_failed` are plain non-additive channels (graph.py:74-75) — last writer wins if two lanes fail.
- Fan-out via `Send("researcher", {...})` with per-lane `plan_item`/`lane_idx` and empty additive seeds (graph.py:358-385). **No try/except exists at the fan-out node or in the wrappers** (`_wrap_researcher`, graph.py:145-157) — but none is needed, see below.

**2. The exception does NOT escape the node — it's already tombstoned**
- `researcher_node` wraps `loop_runner(...)` in `try/except Exception` at `consultants/engine/council.py:547-573`. Any raise (retry exhaustion, tool crash) becomes a tombstone return: `error: "researcher failed: {e}"`, `_role_failed: "researcher"`, `research: ["(researcher lane failed: {e})"]`, plus a zero-token `RoleTurn` (council.py:562-573). The comment at council.py:557-561 states this was the audit-high (csl-...-faa2) hardening. So the graph continues normally: critic → synthesizer all run.
- What escapes `run_loop` itself (`claude_hooks/agent_loop/runner.py:138-311`): no internal try/except; `chat_fn` failures propagate. Retry budget lives in `ChatClient.chat` — 8 retries with exponential backoff (`claude_hooks/get_advice/chat_client.py:31, 270-282`), then `raise RuntimeError(f"ollama chat HTTP {e.code}: ...")` (chat_client.py:285) or bare re-raise of `URLError/OSError` (chat_client.py:302).
- The "non-string tool result" case never raises: `claude_hooks/caliber_proxy/tools.py:439` coerces via `return result if isinstance(result, str) else str(result)`, and `execute()` is documented "Never raises" (tools.py:420-423, errors returned as `error: ...` strings). Even if the executor raised, `traced_tool` re-raises (trace.py:220-222) → caught by the same tombstone.

**3. What the synthesizer sees**
- Not a silent gap: `build_synthesizer_messages` iterates `state["research"]` (council.py:253-254), which now contains the tombstone string as a "RESEARCHER REPORT (round i)" entry alongside the surviving lanes' real reports. The synthesizer is told a lane failed, in-band. Synthesizer node itself has its own try/except producing a placeholder `final_answer` "(consultation incomplete: ...)" if IT dies (council.py:727-747).

**4. Runner stream loop + artifacts**
- `compiled.stream` loop at `consultants/server/runner.py:136-161`: any exception that does escape (LangGraph internals, reducer crash) → `status="failed"`, `_write_failed_artifacts` with placeholder `final_answer=f"(consultation failed: {exc})"` and `turns=[]` (runner.py:224-225) — dead lanes' transcripts lost in that branch only.
- **The actual bug**: runner.py:172-174 — `node_error = final_state.get("error")`; `terminal_status = "failed" if node_error else "completed"`. Because the lane tombstone sets `error`, a single dead lane marks the whole consultation `status="failed"` even though the synthesizer ran and wrote a real answer from N-1 lanes. Artifacts are still written (runner.py:177-199) with `status="failed"` + `error` in the YAML front-matter (storage.py:108-110); `transcript.md` includes the tombstone turn (storage.py:129-136). Planner's premise about `consultancy.json` is stale — real artifacts are `summary.md`/`transcript.md`/`metadata.json` (storage.py:32-34, 200-207); that filename only exists inside old transcript.db content.
- Note: the planner's proposed hardening (per-lane try/except in graph.py) is **already implemented** at council.py:555 — do not duplicate it.

**5. Test coverage**
- Lane failure: `tests/test_consultants_council.py:514-529` (`loop_runner` raising → `error` set) and `:531-560` (tombstone must land in additive `research` so the synthesizer doesn't produce a silently degraded answer). Synthesizer failure placeholder: `:649-660`. Server-level crash: `tests/test_consultants_server.py:285-297`.
- **Gap**: no test pins the runner-level semantics of a lane tombstone — i.e., nothing asserts whether a degraded-but-answered consultation should be `status="failed"` (current behavior, runner.py:174) or `completed`. The verdict on user-visible status rests on the code read alone.

**Verdict**: The council produces a coherent answer — the dead lane's error is tombstoned into the additive `research` channel (council.py:562-573), the synthesizer runs and sees the failure marker plus surviving lanes, and `summary.md` is written with a real answer. But the user *perceives* `status=failed` (poll endpoint, sessions index, YAML front-matter) because runner.py:174 treats any lane error as terminal, even though `final_answer` is genuine — a false-negative status, not a silent degradation.

**ONE hardening change**: `consultants/server/runner.py:174` — differentiate lane-level tombstones from terminal failure, e.g. `terminal_status = "failed" if (node_error and node_failed == "synthesizer") else "completed"` (keep `error` recorded for observability), so a consultation that answered from N-1 lanes reports `completed` with the lane failure noted, while a synthesizer crash stays `failed`.

---

## Critic (round 2)

Write the verdict. I've verified most load-bearing claims. Let me assess what I confirmed vs. what I didn't.

Verified:
- council.py:547-573: tombstone try/except around loop_runner — confirmed exactly as reported (report 1 and 2).
- council.py:245-265: build_synthesizer_messages iterates research_rounds, appends "RESEARCHER REPORT (round i)" — confirmed.
- council.py:715-747: synthesizer_node reads state.get("research") or [], own try/except with placeholder final_answer — confirmed.
- runner.py:136-161: compiled.stream loop, except → status failed, _write_failed_artifacts — confirmed.
- runner.py:171-174: node_error → terminal_status = "failed" — confirmed.
- runner.py:177-199: ConsultationResult with final_answer=final_state.get("final_answer",""), status=terminal_status, error=node_error; write_consultation after loop — confirmed.
- runner.py:211-235: _write_failed_artifacts with placeholder "(consultation failed: {exc})" — confirmed.
- graph.py:60-80: additive reducers research/turns operator.add, int sums; error/_role_failed plain channels — confirmed.
- graph.py:345-386: fanout Send with empty seeds — confirmed.
- chat_client.py:265-305: retry budget, raise RuntimeError at 285, re-raise URLError/OSError at 302 — confirmed.
- tools.py:420-439: execute() never raises, coerces non-str to str — confirmed. So the "non-string tool result" scenario in the question never raises in the shipped path.
- runner.py:110-135 (agent_loop): tool_executor call at 122, len(output) at 126 — confirmed; a custom executor returning non-str would raise TypeError at len() inside execute_tool_calls, which propagates to researcher_node's tombstone. Fine.
- storage.py:100-140: front-matter status/error lines, render_summary body = final_answer, render_transcript iterates turns — confirmed.

Not verified (tool budget ran out):
- The test citations: tests/test_consultants_council.py:514-560 and tests/test_consultants_server.py:285-297 — my read_file call failed ("your tools are off"). So test coverage claims are UNVERIFIED.
- storage.py:174-192 atomic write (.tmp + os.replace) and storage.py:200-208 paths — not directly read; I read 100-140 only. The claim about atomic write is unverified but non-load-bearing for the verdict (the verdict rests on runner.py:199 calling write_consultation after the stream, which I confirmed, and the front-matter content, which I confirmed).
- council.py:253-254 cited by reports — I confirmed via reading 245-265, the loop is at 253-254. Good.
- graph.py:64-72 — confirmed at 60-80.

Reports' discrepancies: Report 1 had a garbled cite "caliber_proxy/tools.py:439 [no tool_executor at this line; line is in execute]" — self-flagged, and the actual line 439 is the coercion return, which I verified. Report 2 cited chat_client.py:31 [no chat at this line; module scope] for DEFAULT_MAX_RETRIES — the provided comment block confirms DEFAULT_MAX_RETRIES = 8 at line 31. Both self-corrected; no real corrections needed. Actually report 1's garble is a formatting artifact, not a factual error — line 439 does contain the coercion. No corrections block needed... Actually let me check: report 1 said "chat_client.py:285 raise RuntimeError" — confirmed. Report 2 said the same. Report 1 cited council.py:547-573 and 562-573 — confirmed. Report 2 cited council.py:555-573, 557-561 comment — confirmed (comment at 557-561).

One nuance both reports agree on and I verified: terminal_status at runner.py:174 maps ANY node_error (including a single lane tombstone) to "failed", while the synthesizer still runs and final_answer is real (runner.py:186). So the answer to the user question: the council produces a coherent degraded answer, but status=failed — the user perceives a "failed" stamp on an actually-usable answer. Both reports converge on this; the recommended hardening is the same anchor (runner.py:174) in both.

Discrepancy between reports: report 2 says test gap "no test pins runner-level semantics of lane tombstone"; report 1 says test coverage exists for tombstone path (council-level). Not contradictory: council-level tombstone tests exist, runner-level status semantics untested. I couldn't verify either test citation.

Decision: ready. The load-bearing chain is verified by my own reads. The only unverified items are the test citations — list as UNVERIFIED. The verdict doesn't rest on tests; it rests on code I read directly.

Confidence: high, ~0.9. The chain is fully confirmed at every hop I checked. Slight deduction for unverified test cites and unread storage.py:174-208 atomic-write claim.

Format: DECISION line, short paragraph, UNVERIFIED items, CONFIDENCE last. No corrections block (nothing I read contradicted the reports). Keep concise.</think>DECISION: ready

The load-bearing chain is verified by direct reads, and both reports converge on the same trace with the same anchor. Confirmed: (1) `consultants/engine/graph.py:64-72` — `research`/`turns` are `operator.add` channels, `error`/`_role_failed` plain last-writer channels; fan-out via `Send("researcher", ...)` with empty additive seeds at `graph.py:358-385`, no try/except needed at the graph layer. (2) `consultants/engine/council.py:547-573` — `researcher_node` catches any `run_loop` exception and returns a tombstone (`error: "researcher failed: {e}"`, `research: ["(researcher lane failed: {e})"]`, zero-token `RoleTurn`), with the comment at 557-561 stating this is the deliberate audit-high fix; so sibling lanes and the graph survive. (3) Escape sources confirmed: retry exhaustion raises at `claude_hooks/get_advice/chat_client.py:285` (RuntimeError) / `:302` (URLError/OSError re-raise), and the "non-string tool result" scenario cannot raise in the shipped path — `claude_hooks/caliber_proxy/tools.py:420-439` `execute()` is documented "Never raises" and coerces via `str(result)` at :439; a custom executor returning non-str would only fail at `len(output)` in `claude_hooks/agent_loop/runner.py:126`, still inside the node's tombstone. (4) Synthesizer input: `council.py:718-723` reads `state.get("research") or []`, and `council.py:253-254` renders each entry as `RESEARCHER REPORT (round i)` — the tombstone string is seen in-band, no missing key, no placeholder. (5) `consultants/server/runner.py:136-161` stream loop only hard-fails on graph-level crashes (`_write_failed_artifacts`, runner.py:211-235, placeholder answer, empty turns); the lane-tombstone path flows to runner.py:172-174 where `terminal_status = "failed" if node_error else "completed"` stamps the whole consultation failed despite a real `final_answer` persisted at runner.py:186/199 — the user-visible mode is a `status=failed` front-matter (`storage.py:108-110`) wrapping a coherent summary body (`storage.py:117-120`). Both reports independently recommend the same single hardening: differentiate lane tombstones from terminal failure at `consultants/server/runner.py:174` (e.g. keyed on `_role_failed == "synthesizer"` or a non-placeholder `final_answer`), keeping `error` recorded. No corrections to either report — every line I read matched its citation, including the two self-flagged garbled cites (chat_client.py:31 `DEFAULT_MAX_RETRIES = 8` and tools.py:439 coercion both check out).

UNVERIFIED: test citations `tests/test_consultants_council.py:514-560` and `tests/test_consultants_server.py:285-297` (tombstone-in-research and server-crash tests) — my read of the test file did not execute before turn budget ran out; the verdict above rests on the code reads, not on these tests. UNVERIFIED: `storage.py:174-192` atomic-write claim (`.tmp` + `os.replace`) — read only storage.py:100-140; non-load-bearing since runner.py:199's post-stream `write_consultation` call is confirmed.

---

## Synthesizer

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

---
