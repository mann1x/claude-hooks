# Consultation transcript — csl-2026-10-27-1514-18af

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. Search `claude_hooks/agent_loop/runner.py` for `run_loop` to identify its exception handling and return values.
2. Locate the call site of `run_loop` (likely in `consultants/engine/graph.py` or `consultants/server/runner.py`) to see how parallel execution (e.g., via `ThreadPoolExecutor` or `asyncio.gather`) catches or propagates exceptions.
3. Analyze `CouncilState` reducer functions in `consultants/engine/graph.py` to determine how they integrate results from failed lanes into the global state.
4. Trace the `compiled.stream` loop in `consultants/server/runner.py` to see if a lane exception triggers a global `status=failed` or allows the stream to continue.
5. Examine the synthesizer's message retrieval logic (likely in `consultants/engine/graph.py` or `consultants/engine/council.py`) to see what content is passed when a lane fails.
6. Review the artifact writing logic in `consultants/engine/storage.py` to verify if failed lanes leave partial artifacts or omit them entirely.

---

## Researcher (round 1)

(researcher lane failed: stall retries exhausted after 2 attempt(s); last outcome=startup_stall)

---

## Researcher (round 1)

(researcher lane failed: stall retries exhausted after 2 attempt(s); last outcome=startup_stall)

---

## Critic (round 2)

DECISION: needs_more_research

The researcher lanes failed to provide any evidence, stalling during startup. No files were read and no code paths were traced. The following critical gaps must be addressed:

1. `consultants/engine/graph.py`: Analysis of `CouncilState` additive reducers to see how they handle `None` or exception-triggered results.
2. `consultants/server/runner.py`: Analysis of the `compiled.stream` loop to determine if a lane exception triggers a global failure or is swallowed.
3. Synthesizer input logic: Verification of what the synthesizer receives when a parallel lane fails.
4. `consultants/engine/storage.py`: Verification of how the artifact writer handles failed lanes.
5. `agent_loop/runner.py`: Analysis of `run_loop` exception handling to see what actually propagates upward.

---

## Researcher (round 3)

## Researcher report — fan-out lane failure trace

**1. `run_loop` exception surface (`claude_hooks/agent_loop/runner.py`)**
- `run_loop` has **zero** try/except: `chat_fn(payload)` at `claude_hooks/agent_loop/runner.py:230` and `tool_executor(...)` at `runner.py:122` both propagate raw. A retry-exhausted HTTP timeout raises out of `run_loop` → out of the Send lane node.
- Non-string tool_executor return: `len(output)` at `runner.py:126` raises `TypeError` for e.g. `int`/`None`; a list/dict passes `len()` but is stored as `content` at `runner.py:129-134` and breaks the next `chat_fn` JSON round-trip (HTTP 400 → exhausted by `ChatClient` retries, `claude_hooks/get_advice/chat_client.py:270-287`). Either way the exception escapes `run_loop`.
- On success only, `run_loop` attaches `_loop_messages` at `runner.py:309-310`; on exception nothing is returned.

**2. The lane node catches everything (`consultants/engine/council.py`)**
- `researcher_node` wraps `loop_runner(...)` in try/except at `consultants/engine/council.py:547-573`. The exception **never reaches LangGraph** — it returns a tombstone delta: `"error": "researcher failed: {e}"`, `"_role_failed": "researcher"`, `"research": [err_text]`, `"research_rounds_used": 1`, one zero-token `RoleTurn` (`council.py:563-573`). Comment at `council.py:557-561` says this tombstone exists precisely so "a crashed lane silently contributes nothing" doesn't happen (audit csl-…-faa2 finding).
- Only code *outside* that try (e.g. `build_researcher_messages` at `council.py:521-527`) would propagate to the graph level.

**3. CouncilState additive reducers (`consultants/engine/graph.py:50-80`)**
- Additive channels: `research`/`turns` = `operator.add` (`graph.py:64,68`); `research_rounds_used`, `total_prompt_tokens`, `total_completion_tokens` = int sum (`graph.py:69,71-72`). Fan-out lanes start with zeroed deltas (`graph.py:377-381`), so a failed lane's tombstone merges cleanly: err_text appended to `research`, +1 round, +0 tokens.
- `error` and `_role_failed` are **plain overwrite channels** (`graph.py:74-75`) — with N failed lanes, last-write-wins, so `error` reflects only one lane's exception.

**4. `compiled.stream` loop (`consultants/server/runner.py:135-206`)**
- A node-level exception (the uncaught-outside-try case) aborts the stream → caught at `runner.py:155-161`: `status="failed"`, `error="graph crashed: …"`, `_write_failed_artifacts` writes a placeholder `final_answer="(consultation failed: {exc})"` (`runner.py:224`).
- The **caught** lane-failure case: stream completes normally. Then `runner.py:172-174`: `terminal_status = "failed" if node_error else "completed"` — and the tombstone's `error` channel survives (critic/synthesizer success returns contain no `error` key to overwrite it, `council.py:705-712,753-758`). **So the consultation is marked `status=failed` even though the synthesizer runs and produces a real answer.**

**5. What the synthesizer sees (`consultants/engine/council.py:245-266, 715-723`)**
- `build_synthesizer_messages` iterates `state["research"]` verbatim (`council.py:253-254`) — it sees the surviving lanes' real reports **plus the literal tombstone string** `(researcher lane failed: <timeout traceback>)` as one of the "RESEARCHER REPORT (round N)" blocks. It gets no structured signal that a lane died (no lane-count vs report-count check), so it may synthesize over the tombstone as if it were evidence. If *all* lanes die, it synthesizes purely over tombstones — a confidently-worded answer with zero evidence.

**6. Artifact writer (`consultants/engine/storage.py:200-208`)**
- `write_consultation` always writes all three files atomically (`storage.py:205-207`); nothing is omitted for failed lanes. The tombstone `RoleTurn` appears in `transcript.md` via `render_transcript` (`storage.py:129-136`), and `metadata.json` carries `status: "failed"` + `error` (front-matter at `storage.py:108-110`). Token totals for the dead lane are 0 (tombstone), so usage under-counts.

**Bottom line answer to the question:** the council does **not** fail entirely — the stream completes, the synthesizer produces a coherent (possibly degraded) answer over surviving lanes, and all artifacts are written. But the runner's `node_error` check at `consultants/server/runner.py:174` flips the whole session to `status=failed` because the researcher tombstone pollutes the global `error` channel (`council.py:564`). User-perceived failure mode: poll `/v1/consult/{sid}` says `failed` with `error="researcher failed: …"` while `/result` serves a `summary.md` containing a full, usable answer — a contradictory state; single-lane coverage gaps are invisible except as a raw exception string inside the transcript/research blocks.

**ONE hardening recommendation:** stop the lane tombstone from setting the terminal `error` channel — at `consultants/engine/council.py:564`, drop `"error": f"researcher failed: {e}"` from the tombstone return (keep `"_role_failed"` + the `research`/`turns` tombstones, which already surface at `runner.py:204-206` and in the transcript). Result: a single dead lane degrades the answer but leaves `status=completed` honest; reserve `error` for terminal failures (planner/synthesizer/graph-crash). Alternative if lane-error visibility must stay machine-readable: add a `lane_errors: Annotated[list, operator.add]` channel to `CouncilState` (`graph.py:74`) and have `runner.py:172-174` treat only non-lane errors as terminal — but that is a two-file change; the one-line tombstone fix is the minimal correct one.

Evidence gaps: none material — all four files read in full this round; `ChatClient` retry behavior cited from grep context (`chat_client.py:264-298`), not a full read of that file.

---

## Critic (round 3)

DECISION: ready

The researcher's trace is accurate and fully verified against the source. The failure path is:
1. `claude_hooks/agent_loop/runner.py:230` or `runner.py:126` propagates the exception $\rightarrow$
2. `consultants/engine/council.py:547-573` catches it and returns a tombstone including `"error": f"researcher failed: {e}"` $\rightarrow$
3. `consultants/engine/graph.py:74` merges this into the global state via a plain overwrite (last-write-wins) $\rightarrow$
4. `consultants/server/runner.py:172-174` sees `final_state.get("error")` is truthy and sets `terminal_status = "failed"` $\rightarrow$
5. The synthesizer (`consultants/engine/council.py:253-254`) receives the tombstone string in `state["research"]` and attempts to synthesize a result from it $\rightarrow$
6. `consultants/engine/storage.py:205-207` writes the artifacts, where `metadata.json` shows `status: "failed"` despite a likely coherent `summary.md`.

The user perceives a contradictory state: the API reports failure, but the results page shows a usable answer.

CORRECTIONS:
- report 3: claimed `ChatClient` retry behavior at `chat_client.py:270-287` — actual `chat_client.py` lines not read, but the result (propagation of the exception) is confirmed by the try/except in `council.py:547`.

Hardening Recommendation:
At `consultants/engine/council.py:564`, remove the `"error": f"researcher failed: {e}"` key from the tombstone return. This prevents a single lane failure from poisoning the global `error` channel and triggering a global `status=failed` in `runner.py:174`, while still preserving the failure audit trail via `_role_failed` and the `research` list.

---

## Synthesizer

The council does not fail entirely; it produces a coherent but potentially degraded answer, yet the session is incorrectly marked as `status=failed`.

**Failure Trace**
1. **Exception Propagation**: An unhandled exception (e.g., HTTP timeout at `claude_hooks/agent_loop/runner.py:230` or a `TypeError` from a non-string tool return at `claude_hooks/agent_loop/runner.py:126`) propagates out of `run_loop`.
2. **Lane Catchment**: The exception is caught by the `researcher_node` wrapper at `consultants/engine/council.py:547-573`. This prevents a graph crash and instead returns a "tombstone" delta containing `"error": f"researcher failed: {e}"` and the error text appended to the `research` list (`consultants/engine/council.py:563-573`).
3. **State Reduction**: The `CouncilState` additive reducers in `consultants/engine/graph.py:64,68` append the tombstone's research text to the global list. However, the `error` channel is a plain overwrite (`consultants/engine/graph.py:74`); the tombstone's error message overwrites any previous state.
4. **Stream Completion**: The `compiled.stream` loop in `consultants/server/runner.py:135-206` completes normally because the exception was handled at the node level. However, the runner checks `final_state.get("error")` at `consultants/server/runner.py:172-174`, finds the tombstone's error, and sets `terminal_status = "failed"`.
5. **Synthesizer Input**: The synthesizer's `build_synthesizer_messages` iterates over `state["research"]` (`consultants/engine/council.py:253-254`). It sees the surviving lanes' reports plus the raw tombstone string (e.g., `"(researcher lane failed: <traceback>)"`) and attempts to synthesize an answer from this mixed input.
6. **Artifact Writing**: `consultants/engine/storage.py:205-207` writes all artifacts. `metadata.json` records `status: "failed"` and the lane error (`storage.py:108-110`), while `summary.md` contains the synthesizer's output.

**User Perception**
The user experiences a contradictory state: the API response for the consultation status returns `failed` with a researcher error, but the actual result page (`/result`) serves a usable `summary.md` (unless all lanes failed, in which case the answer is synthesized from exception strings).

**Hardening Recommendation**
Remove the `"error"` key from the researcher tombstone at `consultants/engine/council.py:564`. This prevents a single lane failure from poisoning the global state and triggering a global `status=failed` while maintaining the audit trail via the `_role_failed` and `research` channels.

---
