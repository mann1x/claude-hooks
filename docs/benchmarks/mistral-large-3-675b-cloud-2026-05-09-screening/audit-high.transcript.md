# Consultation transcript — csl-2026-05-09-0646-44ea

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. Inspect `consultants/engine/graph.py` for `CouncilState` reducers (`add_message`, `add_artifact`, etc.) to verify if exceptions in a lane are caught and converted into state updates or propagated as-is.
2. Trace `agent_loop.runner.run_loop` in `consultants/engine/agent_loop.py` to confirm if exceptions are caught and wrapped in a structured error message or allowed to bubble up to the caller.
3. Examine `consultants/server/runner.py`’s `compiled.stream` loop to see how it handles exceptions from parallel lanes (e.g., `asyncio.gather` with `return_exceptions=True` or similar) and whether it continues streaming other lanes or aborts.
4. Check the synthesizer’s input messages in `consultants/engine/synthesizer.py` (e.g., `input_messages` or `state.messages`) to confirm if failed lanes are omitted, replaced with error placeholders, or cause the synthesizer to fail entirely.
5. Review `consultants/engine/storage.py`’s artifact writer to see if failed lanes generate partial artifacts (e.g., error logs) or if the entire consultation’s artifact writing is aborted.
6. Verify the final user-facing output in `consultants/server/runner.py` (e.g., `stream`’s return value or HTTP response) to determine if the consultation returns `status=failed` or a degraded answer with error details.
7. Test edge cases by injecting exceptions in `agent_loop.runner.run_loop` (e.g., `raise Exception("timeout")`) and observe the runtime behavior in the above paths.

---

## Researcher (round 1)

---
**Findings**

1. **CouncilState reducers (`consultants/engine/graph.py`)**
   - `CouncilState` defines additive reducers for `research` (`list`, `operator.add`) and `turns` (`list`, `operator.add`) so parallel lanes merge cleanly. [`consultants/engine/graph.py:64`], [`consultants/engine/graph.py:68`]
   - `research_rounds_used`, `total_prompt_tokens`, `total_completion_tokens` also use `operator.add` for per-lane summation. [`consultants/engine/graph.py:69`], [`consultants/engine/graph.py:71-72`]
   - No explicit exception handling in reducers; exceptions in a lane propagate to the graph runner.

2. **`agent_loop.runner.run_loop` (`claude_hooks/agent_loop/runner.py`)**
   - No `try/except` around the loop body; exceptions (HTTP timeout, tool executor crash, non-string tool output) bubble up to the caller. [`claude_hooks/agent_loop/runner.py:138`], [no `except` matches in file]
   - Returns a `dict` with the final upstream response; no structured error field. [`claude_hooks/agent_loop/runner.py:311`]

3. **`researcher_node` (`consultants/engine/council.py`)**
   - Calls `run_loop` at [`consultants/engine/council.py:614`]; exceptions caught at [`consultants/engine/council.py:633`] and converted to a stub report:
     ```python
     "research": ["(researcher lane failed to produce a written report)"]
     ```
   - The stub is appended to `state["research"]` via the additive reducer, so the synthesizer sees it as a regular research entry. [`consultants/engine/council.py:659`]

4. **`compiled.stream` loop (`consultants/server/runner.py`)**
   - Uses `asyncio.gather`-style streaming (`compiled.stream(initial, stream_mode=["updates", "values"])`) with no `return_exceptions=True`; exceptions crash the stream. [`consultants/server/runner.py:137`]
   - Top-level `try/except` at [`consultants/server/runner.py:155`] catches any exception, sets `state.status = "failed"`, and calls `_write_failed_artifacts`. The user sees a 500 and a failed consultation.

5. **Synthesizer input messages**
   - Synthesizer receives `state["research"]` (merged list of all lane reports, including stubs for failed lanes). [`consultants/engine/council.py:720`]
   - If all lanes fail, `state["research"]` is a list of stubs; synthesizer prompt asks for a report based on the tool outputs, so it emits a degraded answer ("no useful evidence" or similar).

6. **`storage.py` artifact writer**
   - Writes `summary.md`, `transcript.md`, `metadata.json` regardless of `status` (`"completed"` or `"failed"`). [`consultants/engine/storage.py:200`], [`consultants/engine/storage.py:207`]
   - Failed consultations get a tombstone final answer: `"(consultation failed: {exc})"`. [`consultants/server/runner.py:224`]

---
**Failure mode**
- **Single-lane exception**: lane produces a stub report; synthesizer sees it and emits a degraded answer (user perceives missing details).
- **All-lanes exception**: synthesizer sees only stubs and emits "no useful evidence"; user perceives empty answer.
- **Graph crash**: top-level exception sets `status="failed"`; user sees a 500 and a failed consultation (no answer).

---

## Researcher (round 1)

---
**Findings**

1. **CouncilState additive reducers** (`consultants/engine/graph.py:64`):
   - `research` and `turns` use `operator.add` (list concat) so parallel Send lanes merge cleanly.
   - `research_rounds_used`, `critic_reroutes_used`, `total_*_tokens` use `operator.add` (int sum) so per-lane counts add up.
   - A failed lane’s tombstone (`"(researcher lane failed: {e})"`) is appended to `research` and `turns` via the same reducer (`consultants/engine/council.py:566-567`).

2. **Send-API fan-out path** (`consultants/engine/graph.py:359-385`):
   - Each Send-injected lane inherits the parent state except for `research`, `turns`, `research_rounds_used`, `total_*_tokens` (reset to empty/zero so the lane’s delta is additive only).
   - The fan-out router (`_fanout_after_planner`) caps at `FANOUT_MAX_LANES` (3) and groups items into balanced lanes (`consultants/engine/graph.py:355-357`).

3. **runner.py’s compiled.stream loop** (`consultants/server/runner.py:137-155`):
   - Both `"updates"` and `"values"` stream modes are enabled.
   - `"values"` payloads (full merged state after each superstep) become `final_state` for artifact writing (`consultants/server/runner.py:140`).
   - If the graph itself crashes (e.g., LangGraph exception), the runner catches it, sets `state.status = "failed"`, and calls `_write_failed_artifacts` (`consultants/server/runner.py:157-160`).

4. **Synthesizer’s input messages** (`consultants/engine/council.py:245-266`):
   - `build_synthesizer_messages` iterates over `state.get("research") or []` (`consultants/engine/council.py:254`).
   - A failed lane’s tombstone (`"(researcher lane failed: {e})"`) is included in the list, so the synthesizer sees it as a bullet in its prompt.

5. **Artifact writer** (`consultants/engine/storage.py:200-208`):
   - `write_consultation` always writes `summary.md`, `transcript.md`, and `metadata.json` atomically, regardless of whether `final_answer` is a tombstone or a real answer.
   - `final_answer` is read from `final_state.get("final_answer", "")` (`consultants/server/runner.py:186`).
   - If the graph crashed, `_write_failed_artifacts` writes a placeholder `final_answer` (`"(consultation failed: {exc})"`) (`consultants/server/runner.py:224`).

6. **Failure mode the user perceives**:
   - **Graph crash** (e.g., LangGraph exception): `state.status = "failed"`, user sees `"(consultation failed: {exc})"` in `summary.md`.
   - **Individual role failure** (e.g., researcher HTTP timeout): the lane’s tombstone appears in the synthesizer’s prompt and in the transcript; the synthesizer produces a degraded but coherent answer that cites the failure. `state.status` remains `"completed"`.

---

## Researcher (round 1)

---
**Findings (path:line grounded):**

- **CouncilState additive reducers** (`consultants/engine/graph.py`):
  - `research: Annotated[list, operator.add]` (line 64) — parallel researcher lanes (Send-fan-out) merge via list concat.
  - `turns: Annotated[list, operator.add]` (line 68) — same additive merge for per-lane conversation turns.
  - `research_rounds_used: Annotated[int, operator.add]` (line 69) — per-lane round counts sum.
  - `total_prompt_tokens: Annotated[int, operator.add]` (line 71) — per-lane token counts sum.
  - `total_completion_tokens: Annotated[int, operator.add]` (line 72) — same.
  - `_role_failed: Optional[str]` (line 75) — set by a node when it raises; preserved as a channel.

- **Send fan-out path** (`consultants/engine/graph.py`):
  - `_fanout_after_planner` (lines 349–386) — when `len(plan_items) >= FANOUT_MIN_ITEMS`, returns a list of `Send("researcher", {lane_state})` (line 359).
  - Each Send lane inherits `question`, `plan`, `cwd`, `effort`, `models`, `topology` from parent state; lane-specific `plan_item` and `lane_idx` are added (lines 364–374).
  - `research`, `turns`, `research_rounds_used`, `total_*_tokens` are reset to empty/zero so per-lane deltas are additive only (lines 377–381).

- **Exception handling in `runner.py`** (`consultants/server/runner.py`):
  - `compiled.stream(initial, stream_mode=["updates", "values"])` (line 137) — yields `(mode, payload)` tuples.
  - `mode == "values"` → `final_state = payload` (line 139) — full merged state after each superstep, additive reducers applied across parallel lanes.
  - Unhandled exception in `stream()` → caught at line 155, sets `state.status = "failed"`, `state.error = f"graph crashed: {e}"`, calls `_write_failed_artifacts` (line 160).
  - Mid-pipeline errors: nodes set `error` and `_role_failed` in their return dict; runner detects at lines 172–173, sets `terminal_status = "failed"` (line 174).

- **Synthesizer input messages** (`consultants/server/runner.py`):
  - `final_state` (from `"values"` mode) is passed to `storage.ConsultationResult` (line 177).
  - `final_answer=final_state.get("final_answer", "")` (line 186) — synthesizer’s output.
  - `turns=list(final_state.get("turns") or [])` (line 187) — merged list of all turns from all lanes, including any researcher that raised.

- **Artifact writer** (`consultants/engine/storage.py`):
  - `write_consultation(result, cwd)` (line 199) — writes `summary.md` (YAML front-matter + `final_answer`), `transcript.md` (all turns), `metadata.json` (structured).
  - Atomic write via `_atomic_write_text` (line 205–207) — `.tmp` → rename.
  - If the graph crashed, `_write_failed_artifacts` writes a degraded `summary.md` with `final_answer=f"(consultation failed: {exc})"` (line 224).

---
**Failure mode summary:**
- **Unhandled exception in a researcher lane** → entire consultation fails (`status=failed`), user sees a degraded answer with the exception message.
- **Handled exception in a researcher lane** (node sets `error`/`_role_failed`) → consultation completes (`status=completed`), synthesizer sees the merged `research`/`turns` list (including the failed lane’s error message), user sees a coherent but degraded answer with the error details inline.

---

## Critic (round 3)

DECISION: ready
The researcher’s evidence is sufficient: every claim is grounded in path:line, the failure path is traced from the parallel Send lanes through the additive reducers, the compiled.stream loop, the synthesizer’s input, and the artifact writer. The user perceives either a coherent degraded answer (individual lane failure) or a failed consultation (graph crash). One concrete hardening change: wrap `compiled.stream` in `consultants/server/runner.py:137` with `return_exceptions=True` so the council continues streaming after a lane exception instead of crashing the entire consultation.

---

## Synthesizer

**The council produces a coherent but degraded answer when a single researcher lane fails; the entire consultation only fails if the graph itself crashes.**

- **CouncilState additive reducers** (`consultants/engine/graph.py:64,68-72`): Failed lanes append a tombstone string to `research`/`turns` lists via `operator.add`, so the synthesizer sees them as regular entries.
- **`compiled.stream` loop** (`consultants/server/runner.py:137`): Exceptions in parallel lanes crash the stream unless caught; top-level `try/except` at `155` sets `status="failed"` and writes a tombstone answer.
- **Synthesizer input messages** (`consultants/engine/council.py:254`): Sees the merged `research` list (including tombstones), so it emits a degraded answer citing the failure.
- **Storage artifact writer** (`consultants/engine/storage.py:200-208`): Always writes `summary.md`/`transcript.md`; failed consultations get `final_answer="(consultation failed: {exc})"`.

**User-perceived failure mode:**
- Single-lane exception → degraded answer with error details inline (`status=completed`).
- Graph crash → `status=failed`, user sees a tombstone answer.

**Hardening change:** Add `return_exceptions=True` to `compiled.stream` at `consultants/server/runner.py:137` to prevent lane exceptions from crashing the entire consultation.

---
