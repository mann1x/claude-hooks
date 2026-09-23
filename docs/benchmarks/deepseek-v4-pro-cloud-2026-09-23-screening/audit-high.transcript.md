# Consultation transcript — csl-2026-09-23-1817-4d32

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. Inspect consultants/engine/graph.py for CouncilState reducer definitions and the Send-API fan-out node(s) that call agent_loop.runner.run_loop per researcher lane; identify whether each node wraps run_loop in try/except and which state fields it returns on exception.
2. Examine consultants/server/runner.py around the compiled.stream loop; locate exception handling and determine whether a lane exception aborts the stream or is captured per node output.
3. Trace the graph edges and state updates after lane nodes in consultants/engine/graph.py to see if one lane's exception prevents other lanes from updating state or blocks the synthesizer node.
4. Inspect the synthesizer node's input selection in consultants/engine/graph.py to determine exactly which CouncilState messages/fields it reads and whether failed lane outputs are included as error text or omitted.
5. Inspect storage.py artifact writer to see how status, answer, and error fields are derived from CouncilState after the stream, and what is persisted when a lane raises.
6. Trace the concrete exception sources in agent_loop.runner.run_loop and tool_executor: locate HTTP retry exhaustion and non-string tool result handling, and confirm whether they raise unhandled exceptions or return error values that graph nodes may treat as success/failure.

---

## Researcher (round 1)

## Failure path trace

**1. CouncilState additive reducers (`consultants/engine/graph.py`)**
- `research` is `Annotated[list, operator.add]` and `turns`, `research_rounds_used`, `total_*_tokens` are additive — `consultants/engine/graph.py:64-72`. Parallel `Send` lanes merge cleanly because each lane returns a delta.
- Fan-out `Send` payload seeds per-lane `research: []`, `turns: []`, counters `0` at `consultants/engine/graph.py:377-381`, so one lane's failure tombstone is concatenated, not lost.
- Node wrappers (`_wrap_researcher`) do **not** wrap `run_loop`; they delegate to `council.researcher_node` — `consultants/engine/graph.py:145-157`.

**2. `compiled.stream` loop (`consultants/server/runner.py`)**
- The stream iterates `["updates", "values"]` at `consultants/server/runner.py:137-154`. A lane exception does **not** abort the stream because `researcher_node` catches it before LangGraph sees it.
- The only stream abort path is an exception escaping a node wrapper; that hits `except` at `consultants/server/runner.py:155-161`, sets `status="failed"` and calls `_write_failed_artifacts`.
- After the stream, the runner reads `final_state["error"]` and sets `terminal_status = "failed" if node_error else "completed"` — `consultants/server/runner.py:171-174`.

**3. Exact lane exception handling (`consultants/engine/council.py`)**
- `researcher_node` wraps the `loop_runner(...)` call in `try/except` at `consultants/engine/council.py:547-554`. An HTTP timeout or a `TypeError` from a non-string tool output inside `run_loop` propagates out of `run_loop` (no try/except in `claude_hooks/agent_loop/runner.py` around `final = chat_fn(payload)` at `runner.py:230` or `len(output)` at `runner.py:126`) and is caught here.
- The catch returns a tombstone: `"error": f"researcher failed: {e}"`, `"_role_failed": "researcher"`, `"research": [err_text]`, `"research_rounds_used": 1`, and a `RoleTurn` — `consultants/engine/council.py:555-573`. Test coverage confirms this exact shape at `tests/test_consultants_council.py:531-561`.
- Critic/synthesizer then run normally; the graph does not stall.

**4. Synthesizer input**
- `synthesizer_node` calls `build_synthesizer_messages(... state.get("research") or [] ...)` — `consultants/engine/council.py:715-723`.
- The failed lane's tombstone string `"(researcher lane failed: HTTP timeout...)"` is one entry in `research`, so the synthesizer sees it as a `RESEARCHER REPORT (round N)` — `consultants/engine/council.py:245-266`. It gets the surviving lanes' real reports plus one visible failure marker.

**5. Storage artifact writer (`consultants/engine/storage.py`)**
- Runner constructs `ConsultationResult` with `final_answer=final_state.get("final_answer","")`, `status=terminal_status`, `error=node_error` — `consultants/server/runner.py:177-199`.
- `storage.write_consultation` writes `summary.md`, `transcript.md`, `metadata.json` atomically — `consultants/engine/storage.py:200-208`.
- `render_summary` emits YAML front-matter with `status: "failed"` and `error: "researcher failed: ..."`, followed by the real `final_answer` body — `consultants/engine/storage.py:117-120`.

## Conclusion

**Both happen: the council still produces a coherent answer, but the consultation is surfaced as `status=failed`.** The synthesizer still runs and writes a real `final_answer` from surviving lanes plus the failure tombstone; the user perceives a failed consultation with a usable but potentially degraded answer, because the global `error` field triggers `terminal_status = "failed"` in `consultants/server/runner.py:174`, even though only one lane failed and the pipeline recovered.

## Recommended single hardening change

In `consultants/engine/council.py:564`, remove `"error": f"researcher failed: {e}"` from the researcher lane tombstone return, keeping `_role_failed`, `research`, `turns`, and `research_rounds_used`. The lane failure remains visible to the synthesizer (via `research` tombstone) and to the transcript (`turns`), but it no longer sets the global `error` that marks the entire consultation failed in `consultants/server/runner.py:174`. That makes lane-level failures degrade instead of fail the whole run.

---

## Researcher (round 1)

## Findings

**Key verdict up front:** a lane-level `run_loop` exception does **not** crash the graph. `researcher_node` catches it and returns a tombstone, so the synthesizer still runs and produces a real answer — but the tombstone also sets `error`, which the runner then converts into `status="failed"`. The user gets a **real answer body wrapped in a `failed` status** (a false-negative failure), and the failed lane's sub-question may be silently uncovered.

### 1. Exception is caught inside `researcher_node`, not propagated

- `researcher_node` wraps the entire `loop_runner(...)` call in `try/except Exception` at `consultants/engine/council.py:547-555`. Any exception from `run_loop` (HTTP retry exhaustion or tool-executor raise) lands in the tombstone return at `consultants/engine/council.py:563-573`, which sets `error`, `_role_failed`, a visible `research: [err_text]`, `research_rounds_used: 1`, and a `RoleTurn` tombstone.
- The comment at `consultants/engine/council.py:555-561` states the intent explicitly: make a crashed lane visible to the synthesizer instead of silently contributing nothing (audit-high `csl-...-faa2`).
- HTTP retry exhaustion source: `ChatClient.chat` raises `RuntimeError` at `claude_hooks/get_advice/chat_client.py:285` (or re-raises `URLError/OSError` at `claude_hooks/get_advice/chat_client.py:302`). That propagates through `run_loop`'s unguarded `final = chat_fn(payload)` at `claude_hooks/agent_loop/runner.py:230` and out to `researcher_node`.
- Non-string tool result: the production executor `execute()` coerces everything to `str` and never raises (`claude_hooks/caliber_proxy/tools.py:420-439`), so a non-string cannot escape that path. A *custom* executor returning a non-string would not raise inside `run_loop` either — it flows into `content` at `claude_hooks/agent_loop/runner.py:129-134` and would only blow up later at `json.dumps(body)` in `claude_hooks/get_advice/chat_client.py:209` on the next chat call, still landing in the same `researcher_node` tombstone.

### 2. CouncilState additive reducers merge the failure into shared state

- `research: Annotated[list, operator.add]` — `consultants/engine/graph.py:64` — the failed lane's `"(researcher lane failed: …)"` string is **concatenated** alongside surviving lanes' reports.
- `turns: Annotated[list, operator.add]` — `consultants/engine/graph.py:68` — the failed lane's tombstone `RoleTurn` is appended.
- `research_rounds_used`, `total_prompt_tokens`, `total_completion_tokens` use int-sum reducers — `consultants/engine/graph.py:69-72`.
- `error: Optional[str]` and `_role_failed: Optional[str]` have **no reducer** — `consultants/engine/graph.py:74-75` — so under LangGraph's default replace/retain semantics, the failed lane's `error` persists and is *not* cleared by surviving lanes (which return no `error` key).

### 3. `compiled.stream` loop does not see an exception

- The `try/except` at `consultants/server/runner.py:155-161` (`state.status = "failed"`, `_write_failed_artifacts`) is **not** triggered by a lane exception — the node already caught it. It only fires on a genuine graph-level crash (fanout router, critic route, uncaught node).
- `final_state` is the last `"values"` payload (`consultants/server/runner.py:139-141`), so it contains the merged `error`, `research`, `turns`.
- After the stream drains, `node_error = final_state.get("error")` at `consultants/server/runner.py:172` is truthy → `terminal_status = "failed"` at `consultants/server/runner.py:174`.

### 4. What the synthesizer actually sees

- `synthesizer_node` builds messages from `state["question"]`, `state.get("plan")`, `state.get("research")`, `state.get("critique")` — `consultants/engine/council.py:718-723`. It **does not read `error` or `_role_failed`**.
- `build_synthesizer_messages` renders every entry of `research` as `RESEARCHER REPORT (round N)` — `consultants/engine/council.py:253-254`. So the failure appears to the synthesizer only as one report string, e.g. `(researcher lane failed: ollama chat HTTP 500: …)`, mixed in with real lane reports. The prompt (`consultants/engine/council.py:257-260`) still instructs it to write a direct, concrete final answer — nothing tells it to abort or flag the gap.
- No conditional edge gates on `error`: topology is unconditional through `plan_topology` (`consultants/engine/graph.py:212-220`) and `route_after_critic` checks only `critic_decision` (`consultants/engine/council.py:366-390`). So the synthesizer **always runs** after a lane failure.

### 5. storage.py artifact writer

- `ConsultationResult` is built at `consultants/server/runner.py:177-198`: `final_answer=<real synth answer>` (`:186`), `status="failed"` (`:189`), `error=<node_error>` (`:190`), `turns=<merged incl. tombstone>` (`:187`).
- `write_consultation` writes three files (`consultants/engine/storage.py:200-208`). `render_summary` puts `status: failed` and `error: …` in the YAML front-matter but the **real answer as the body** (`consultants/engine/storage.py:117-120`). `metadata.json` via `asdict` likewise carries `"status": "failed"` with a populated `final_answer` (`consultants/engine/storage.py:166-169`).
- `state.status = terminal_status` and `state.error = node_error` at `consultants/server/runner.py:201-202` make the poll endpoint (`/v1/consult/{sid}`) report `failed`, even though a coherent answer exists on disk.

### Degraded-answer failure mode

The synthesizer produces a real answer over the surviving lanes' evidence. The failed lane's sub-question is unanswered, and the only signals are (a) the error string inside the `research` list (which the synth may or may not echo), and (b) the tombstone turn in `transcript.md`. The user perceives a **failed consultation whose `summary.md` still contains a plausible answer** — with no guarantee that the answer mentions the missing lane coverage. If the synthesizer doesn't echo the failure marker, the gap is fully silent.

### Recommended single hardening change

`consultants/server/runner.py:171-174` — stop treating a *lane* tombstone as terminal failure when a real answer was produced; reserve `failed` for synthesizer failure or no-answer:

```python
node_error = final_state.get("error")
node_failed = final_state.get("_role_failed")
if node_failed == "synthesizer" or not final_state.get("final_answer"):
    terminal_status = "failed"
else:
    terminal_status = "completed"
```

This fixes the false-negative (answer exists → `completed`, with the `_role_failed` warning already logged at `consultants/server/runner.py:204-206`), while still classifying a synthesizer tombstone (`consultants/engine/council.py:736-747`, which sets `_role_failed="synthesizer"`) or a graph that produced no `final_answer` as genuinely `failed`. The failure remains visible to the user via the research entry, the `turns` tombstone in `transcript.md`, and the `_role_failed` warning.

---

## Critic (round 2)

DECISION: ready

The two reports converge on the end-to-end path and answer the user's core question: the lane exception is caught in `researcher_node` (council.py:547-573), merged via additive reducers while `error` persists (graph.py:64-75), does not abort `compiled.stream` (runner.py:137-161), and yields `terminal_status="failed"` despite a real `final_answer` (runner.py:171-174). The synthesizer sees the failure only as one `research` tombstone entry and always runs (council.py:715-723; graph.py:212-220). Storage writes `status: failed` with the real answer body (storage.py:117-120, 200-208). Round2 adds concrete exception sources (chat_client.py:285/302, runner.py:230) and resolves the non-string-tool-output path. For the single hardening change, use round2's `runner.py:171-174` status gate, not round1's `council.py:564` error removal — it preserves the failure signal while correcting the false-negative status. Minor line-number annotations are approximate but consistent enough to synthesize.

---

## Synthesizer

**Bottom line: the consultation produces a real, coherent `final_answer`, but it is surfaced to the user as `status=failed` — a false-negative caused by the lane tombstone's `error` field, not by an actual pipeline crash.**

1. **Additive reducers** — `research` and `turns` use `operator.add` (list concat) and the counters use int-sum, so parallel `Send` lanes merge deltas cleanly (`consultants/engine/graph.py:64-72 [no Send at this line; line is in CouncilState]`). But `error` and `_role_failed` have **no reducer** (`consultants/engine/graph.py:74-75`), so once a failed lane writes `error`, LangGraph's default semantics keep it — surviving lanes return no `error` key and cannot clear it.

2. **The exception never reaches `compiled.stream`'s handler** — the `try/except` around `compiled.stream` (`consultants/server/runner.py:137-161`) is *not* triggered; it only fires on graph-level crashes. The lane exception is caught earlier inside `researcher_node`, which wraps the entire `loop_runner(...)` call in `try/except Exception` (`consultants/engine/council.py:547-555`). The concrete sources — HTTP retry exhaustion raising from `chat_client.chat` and propagating through the unguarded `final = chat_fn(payload)` (`claude_hooks/agent_loop/runner.py:230`) — land in that catch. (A custom non-string tool result doesn't raise in `run_loop`; it blows up later at the next `json.dumps` in the chat client, still landing in the same tombstone.)

3. **The catch returns a tombstone** with `error`, `_role_failed: "researcher"`, `research: ["(researcher lane failed: ...)"]`, `research_rounds_used: 1`, and a `RoleTurn` (`consultants/engine/council.py:562-573`). The graph does **not** stall: topology is unconditional through `plan_topology` and `route_after_critic` keys only on `critic_decision`, so the synthesizer always runs.

4. **What the synthesizer sees** — `synthesizer_node` builds messages from `question`, `plan`, `research`, and `critique` only (`consultants/engine/council.py:718-723`). It does **not** read `error` or `_role_failed`. The failed lane's tombstone string is just one entry in the `research` list, rendered as `RESEARCHER REPORT (round N)` alongside the surviving lanes' real reports (`consultants/engine/council.py:253-254`). The prompt still instructs it to write a direct final answer (`consultants/engine/council.py:257-260`) — nothing tells it to abort or flag the gap, so the missing sub-question may be silently dropped if the synthesizer doesn't echo the marker.

5. **Storage and status** — after the stream drains, `node_error = final_state.get("error")` is truthy, so `terminal_status = "failed" if node_error else "completed"` (`consultants/server/runner.py:171-174`). `ConsultationResult` is built with the **real** `final_answer` but `status="failed"` and the lane `error` (`consultants/server/runner.py:177-198`). `write_consultation` writes three files (`consultants/engine/storage.py:200-208`); `render_summary` puts `status: failed` and the error in YAML front-matter with the real answer as the body (`consultants/engine/storage.py:117-120`). The poll endpoint then reports `failed` because `state.status`/`state.error` are set from `terminal_status`/`node_error` (`consultants/server/runner.py:201-202`).

**Perceived failure mode:** the user gets a failed consultation whose `summary.md` still contains a plausible answer. The failed lane's sub-question may be uncovered, and the only signals are the tombstone string in the research list, the tombstone `RoleTurn` in `transcript.md`, and the `_role_failed` warning logged at `consultants/server/runner.py:204-206 [no _role_failed at this line; line is in run_council]`.

**One hardening change** — fix the false-negative at `consultants/server/runner.py:171-174`: reserve `failed` for synthesizer failure or no answer, not a lane tombstone:

```python
node_error = final_state.get("error")
node_failed = final_state.get("_role_failed")
if node_failed == "synthesizer" or not final_state.get("final_answer"):
    terminal_status = "failed"
else:
    terminal_status = "completed"
```

This keeps a synthesizer tombstone (`consultants/engine/council.py:736-747`, which sets `_role_failed="synthesizer"`) or an answerless graph as genuinely `failed`, while a degraded-but-answered lane failure becomes `completed` with the failure still visible via `research`, `turns`, and the `_role_failed` warning.

---
