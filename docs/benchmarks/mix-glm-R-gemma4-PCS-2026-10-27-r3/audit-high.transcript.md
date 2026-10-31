# Consultation transcript — csl-2026-10-27-1544-61fc

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. In `consultants/server/runner.py`, locate the `compiled.stream` loop and identify the mechanism used to execute parallel researcher lanes (e.g., `asyncio.gather`, `TaskGroup`) to determine if exceptions are caught locally or propagate to the main loop.
2. In `consultants/engine/graph.py`, analyze `CouncilState` and its additive reducers to verify if they explicitly handle missing lane results or `Exception` objects when aggregating state.
3. Trace the data flow from the parallel execution site in `consultants/server/runner.py` to the synthesizer's input construction to identify exactly what message content (e.g., empty string, error message, or omission) is passed when a lane fails.
4. In `consultants/engine/storage.py`, examine the artifact writer to see if it is invoked per-lane or globally and whether a lane exception prevents the final consultancy state from being persisted.
5. In `consultants/server/runner.py`, find the logic that determines the final API response `status` to verify if a single lane's unhandled exception triggers a `status=failed` for the entire request.

---

## Researcher (round 1)

## Findings

**Exception origin (run_loop / executor)**
- `run_loop` has no internal try/except: `final = chat_fn(payload)` at `claude_hooks/agent_loop/runner.py:230`, and tool exec at `claude_hooks/agent_loop/runner.py:300` → `execute_tool_calls` → `output = tool_executor(name, args_str, cwd)` + `len(output)` at `claude_hooks/agent_loop/runner.py:122-126`. A non-string tool result raises `TypeError` right there (uncaught); an exhausted retry budget raises `RuntimeError("ollama chat HTTP …")` at `claude_hooks/get_advice/chat_client.py:285-287` (retry budget = `DEFAULT_MAX_RETRIES = 8`, `chat_client.py:31`). Either way the exception propagates out of `run_loop` into the researcher node.

**1. CouncilState reducers (`consultants/engine/graph.py`)**
- The exception is **caught inside the node, not by LangGraph**: `researcher_node` wraps `loop_runner(...)` in `except Exception` at `consultants/engine/council.py:547-573` and returns a **tombstone partial-state**: `error`, `_role_failed="researcher"`, `research: ["(researcher lane failed: …)"]`, `research_rounds_used: 1`, one zero-token `RoleTurn`. So the Send fan-out never sees a raise; the lane "completes" with an error payload.
- Additive reducers merge the tombstone cleanly: `research: Annotated[list, operator.add]` (`consultants/engine/graph.py:64`), `turns` (`graph.py:68`), token sums (`graph.py:71-72`). Surviving lanes' reports concat with the tombstone.
- Critical detail: `error` / `_role_failed` (`graph.py:74-75`) have **no reducer** — last-write-wins. With multiple failed lanes, only one lane's error string survives (nondeterministic which); with one failed lane it persists to final state.

**2. `compiled.stream` loop (`consultants/server/runner.py`)**
- The stream loop at `consultants/server/runner.py:137-154` only catches exceptions that escape the graph (`except Exception` at `runner.py:155-161` → `status="failed"`, `_write_failed_artifacts`). A lane tombstone does **not** take this path — the graph finishes normally.
- Instead the node-error path fires: `node_error = final_state.get("error")` → `terminal_status = "failed" if node_error else "completed"` at `runner.py:172-174`. **One failed lane flips the entire consultation to `status=failed`**, even though the synthesizer still runs and produces a real answer.

**3. Synthesizer input**
- `synthesizer_node` → `build_synthesizer_messages(state["question"], plan, state.get("research") or [], critique)` at `council.py:718-723`; every `research` entry is rendered as `RESEARCHER REPORT (round N)` at `council.py:253-254`. The synthesizer therefore sees: surviving lanes' real reports **plus** the literal tombstone string `"(researcher lane failed: <exc>)"` as one round. It is not told this is an error marker — it's just another report — so it typically synthesizes a degraded-but-coherent answer from the surviving lanes (and may cite the failure text). This is by design: `council.py:557-561` comments that without the tombstone the synthesizer would "write a confidently-degraded answer over the surviving lanes"; the marker makes the gap visible.

**4. Storage (`consultants/engine/storage.py`)**
- `runner.py:177-199` builds `ConsultationResult` with the real `final_answer`, full `turns` (including the tombstone turn), but `status="failed"` + `error=node_error`; `storage.write_consultation` at `runner.py:199` → `storage.py:200-208` writes summary.md (front-matter carries `status:` + `error:` at `storage.py:108-110`), transcript.md (tombstone turn rendered as a normal `## Researcher` section, `storage.py:129-136`), metadata.json (`storage.py:166-169`).
- The `_write_failed_artifacts` path (`runner.py:211-237`, placeholder answer `"(consultation failed: …)"`, empty turns) is only hit when the graph itself crashes — not on lane failure.

**Verdict**
- The council **does produce a coherent (degraded) answer** — the synthesizer runs on surviving lanes + tombstone and `summary.md` contains a genuine answer. But the session is marked `status=failed` with `error="researcher failed: …"` (`runner.py:201-202`, surfaced via `SessionState.public_dict` at `consultants/server/app.py:61-75`). Perceived failure mode: a user/CLI polling `GET /v1/consult/{sid}` sees `status=failed` and will likely discard a summary that is actually mostly valid; the answer exists only if something reads `/result` or the artifacts anyway. Multi-lane failure additionally collapses to one error string (last-write-wins on `graph.py:74`).

**One hardening change (recommended)**
- `consultants/server/runner.py:174` — replace `terminal_status = "failed" if node_error else "completed"` with a three-way check: `"failed"` only when `_role_failed` is `synthesizer` (or planner, where no answer can exist), `"degraded"` (or `"completed"` with a warning) when `_role_failed == "researcher"`/`"critic"` and `final_answer` is non-empty. This matches the existing tombstone design intent (`council.py:557-561`) and stops a single lane failure from masking a usable answer. Secondary (not chosen): give `error`/`_role_failed` an additive reducer in `graph.py:74-75` so multi-lane failures don't collapse to one string.

---

## Researcher (round 1)

Report — trace of a single fan-out lane raising inside `run_loop`:

**1. CouncilState reducers (consultants/engine/graph.py)**
- Fan-out: planner conditional edge spawns ≤3 lanes via `Send("researcher", {...})` with per-lane `plan_item`/`lane_idx` and zeroed additive channels — `consultants/engine/graph.py:349-386`.
- Additive channels: `research: Annotated[list, operator.add]` (`graph.py:64`), `turns` (`graph.py:68`), token counters (`graph.py:71-72`). Non-additive: `error`, `_role_failed` (`graph.py:74-75`) — last-writer-wins per superstep.

**2. The exception never escapes the node — it's caught at the lane level**
- `researcher_node` calls `loop_runner(...)` inside `try` (`consultants/engine/council.py:547-554`). Any exception from `run_loop` — HTTP timeout exhausting `chat_fn` retries, or a `tool_executor` returning a non-string (raises `TypeError` at `len(output)` in `claude_hooks/agent_loop/runner.py:122-126`, propagating out of `run_loop`) — is caught at `council.py:555`.
- Tombstone return (`council.py:562-573`): `error="researcher failed: …"`, `_role_failed="researcher"`, `research=["(researcher lane failed: …)"]`, plus a zero-token `RoleTurn`. The comment at `council.py:557-561` says this was added deliberately (audit csl-...-faa2) so a crashed lane is *visible* to downstream roles instead of silently contributing nothing.
- So the other lanes are unaffected; LangGraph merges each lane's delta; the graph continues researcher → critic → synthesizer. The graph-crash path in `consultants/server/runner.py:155-161` (`status=failed`, `_write_failed_artifacts` with `final_answer="(consultation failed: …)"` and empty `turns`, `runner.py:211-237`) is **not** hit for this scenario — only for exceptions that escape a node entirely.

**3. What downstream roles see**
- Critic sees the tombstone as a "RESEARCHER REPORT (round i)" bullet (`council.py:233-241`); on its own failure it defaults to `DECISION: ready` (`council.py:677-694`), so the pipeline can't stall.
- Synthesizer's input (`build_synthesizer_messages`, `council.py:245-266`) contains: the surviving lanes' real reports **plus** the literal string `(researcher lane failed: <exc>)` rendered as if it were a researcher report (`council.py:253-254`). It is not told which lane index failed or that it's a tombstone — only the raw exception text distinguishes it.

**4. Status determination (consultants/server/runner.py)**
- After the stream drains: `node_error = final_state.get("error")` → `terminal_status = "failed" if node_error else "completed"` (`runner.py:172-174`). One lane's tombstone therefore flips the **entire consultation to `status=failed`**, even though the synthesizer ran and produced a real `final_answer` (`runner.py:186` reads it regardless).
- Artifacts still write normally: `storage.write_consultation(result, ...)` at `runner.py:199` with `status=terminal_status` and `error=node_error` (`runner.py:189-190`); `state.status`/`state.error` set at `runner.py:201-202`. Poll API surfaces it via `SessionState.status` (`consultants/server/app.py:53,64`); outer runner crash catch at `app.py:320-324` is a separate layer.

**5. Storage (consultants/engine/storage.py)**
- Global, not per-lane: `write_consultation` writes all three files atomically (`storage.py:200-208`, tmp+rename at `storage.py:174-192`). `summary.md` front-matter carries `status: failed` + `error` (`storage.py:108-110`); body is the synthesizer's answer (`storage.py:117-120`). The failed lane's tombstone turn appears in `transcript.md` (`storage.py:129-136`). Nothing is lost.

**Verdict**
- The council **does produce a coherent answer** — surviving lanes' evidence reaches the synthesizer, which writes a real `final_answer` into `summary.md`. The user's perceived failure mode is a *mismatch*: the `/v1/consult/{sid}` poll and metadata.json say `status=failed` with `error="researcher failed: …"`, while `summary.md` contains a usable (mildly degraded — one lane's plan items uninvestigated, and the tombstone string sits inline in the synthesizer's "report" list) answer. Only if the exception escaped `researcher_node` (it can't for `run_loop`/tool-executor errors) would the whole thing hard-fail via `runner.py:155-161`.

**One hardening change**
- `consultants/server/runner.py:174` — treat role-level tombstones as degraded-success, not failure: `terminal_status = "failed" if (node_error and final_state.get("_role_failed") == "synthesizer") else "completed_with_degradation"` (keeping `error` populated). Today a single lane's HTTP timeout marks the whole consultation failed even when N-1 lanes and the synthesizer succeeded; distinguishing synthesizer-failure (terminal, no answer) from lane-failure (answer exists) makes the status match what the user actually receives in `summary.md`.

---

## Critic (round 2)

DECISION: ready

The researcher's trace is accurate and verified against the source. The failure path is clearly delineated: exceptions in `run_loop` (e.g., `TypeError` at `claude_hooks/agent_loop/runner.py:126` or `RuntimeError` from `chat_client.py`) are caught by the `researcher_node` wrapper (`consultants/engine/council.py:547-555`), which returns a tombstone state. This tombstone is merged via additive reducers (`consultants/engine/graph.py:64,68`), ensuring the synthesizer sees the failure as a string (`consultants/engine/council.py:562`) rather than a silent gap. However, the presence of any `error` string in the final state triggers `terminal_status = "failed"` in the server runner (`consultants/server/runner.py:174`), causing a mismatch where the user sees a "failed" status despite receiving a coherent, synthesized answer in the artifacts (`consultants/server/runner.py:177-199`).

CORRECTIONS:
- None.

---

## Synthesizer

The council produces a coherent but degraded answer, but the session is surfaced to the user as `status=failed`.

**Failure Path Trace**
1.  **CouncilState Reducers (`consultants/engine/graph.py`):** Exceptions in `run_loop` (e.g., `TypeError` at `claude_hooks/agent_loop/runner.py:126 [no run_loop at this line; line is in execute_tool_calls]` or `RuntimeError` at `claude_hooks/get_advice/chat_client.py:285`) are caught by the `researcher_node` wrapper (`consultants/engine/council.py:555`). The node returns a "tombstone" state containing an error string and a marker in the `research` list (`consultants/engine/council.py:562-573`). These are merged via additive reducers for `research` and `turns` (`consultants/engine/graph.py:64 [no turns at this line; line is in CouncilState], 68`), while `error` and `_role_failed` use last-writer-wins (`consultants/engine/graph.py:74-75`).
2.  **Compiled Stream Loop (`consultants/server/runner.py`):** Because the exception is handled within the node, it does not escape to the `compiled.stream` loop's global exception handler (`runner.py:155-161`). The graph completes its traversal normally.
3.  **Synthesizer Input:** The synthesizer receives the surviving researcher reports plus the literal tombstone string `(researcher lane failed: <exc>)` rendered as a standard researcher report (`consultants/engine/council.py:253-254`). It synthesizes a response based on the available evidence.
4.  **Storage Artifact Writer (`consultants/engine/storage.py`):** The `runner.py` logic determines the final status via `terminal_status = "failed" if node_error else "completed"` (`runner.py:172-174`). Even if the synthesizer produced a valid `final_answer`, the presence of the researcher's tombstone error flips the status to `failed`. `storage.write_consultation` then writes the real answer to `summary.md` but marks the front-matter as `status: failed` (`consultants/engine/storage.py:108-110 [no md at this line; line is in _yaml_front_matter]`).

**Perceived Failure Mode**
The user perceives a system failure (e.g., `GET /v1/consult/{sid}` returns `status=failed`), potentially leading them to discard the result. However, the `summary.md` artifact actually contains a usable, though slightly degraded, answer.

**Recommended Hardening**
Modify `consultants/server/runner.py:174` to distinguish between terminal failures (synthesizer/planner) and degraded successes (researcher/critic):
`terminal_status = "failed" if (node_error and final_state.get("_role_failed") == "synthesizer") else "completed"` (or introduce a `"degraded"` status).

---
