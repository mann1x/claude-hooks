# Consultation transcript — csl-2026-09-23-2030-b3da

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. Read the CouncilState reducers and lane-merge annotations in `consultants/engine/graph.py` to determine whether a researcher node's exception propagates through the node or reducers swallow/absent a lane key.

2. Read `agent_loop/runner.py` `run_loop` end-to-end: locate the HTTP retry loop and tool_executor invocation to determine whether retry-budget exhaustion raises, returns an error payload, or can emit a non-string result into its return value.

3. Read the `compiled.stream(...)` loop in `consultants/server/runner.py`: check whether node chunks are wrapped in try/except, what happens to the loop and the `status` field when the generator raises mid-stream, and whether storage writes happen inside the loop, in a `finally`, or only after completion.

4. Read the synthesizer node's input-assembly code in `consultants/engine/graph.py` to establish exactly what a failed lane contributes to its messages: absent key, empty dict, error string, or nothing because the exception preempts assembly.

5. Read `consultants/server/storage.py`'s artifact writer and its call sites (from runner.py and graph.py) to verify whether a mid-stream exception still persists partial state and/or `status=failed`.

6. Find where the consultation API response/status is constructed in `consultants/server/` (runner or router) and confirm the user-visible outcome: `status=failed`, a degraded answer missing one lane, or an unhandled 500 — cite the exact return/raise site.

7. Synthesize the verdict (coherent / degraded-with-visible-gap / hard-fail) from steps 1–6 and select exactly one hardening insertion point — evaluate wrapping per-node exceptions in the `compiled.stream` loop in `consultants/server/runner.py` vs. catching inside `run_loop` in `agent_loop/runner.py` — and cite the exact path:line for the recommended change.

---

## Researcher (round 1)

## Findings

**1. CouncilState reducers (graph.py)**
- `research` and `turns` are `Annotated[list, operator.add]`; token/round counters are `Annotated[int, operator.add]` — `consultants/engine/graph.py:64`, `:68`, `:69-72`. Parallel Send lanes merge additively; `error` / `_role_failed` (`:74-75`) are plain channels (last-write-wins across lanes).
- Crucially, **exceptions never escape a researcher node into LangGraph**. `researcher_node` wraps the entire `run_loop` call in `try/except Exception` (`consultants/engine/council.py:547-554` catch at `:555`) and returns a **tombstone**: `research: ["(researcher lane failed: {e})"]`, `error`, `_role_failed: "researcher"`, a zero-token RoleTurn (`:562-573`). The docstring explicitly says this exists so "a crashed lane [doesn't] silently contribute nothing" (audit csl-...-faa2). So the reducers see a tombstone *entry*, not a missing key; the graph proceeds normally.

**2. Where the exception originates (agent_loop/runner.py)**
- Retry-budget exhaustion: `ChatClient.chat` raises `RuntimeError(...)` after the retry loop (`claude_hooks/get_advice/chat_client.py:285`, `:302`, `:305`). `run_loop` calls `final = chat_fn(payload)` bare (`claude_hooks/agent_loop/runner.py:230`) with no try/except → propagates.
- Non-string tool result: `execute_tool_calls` does `output = tool_executor(...)` then `len(output)` (`runner.py:122`, `:126`) — a non-string raises `TypeError` there, also uncaught → propagates through `run_loop` (`:311`) into `researcher_node`'s except.

**3. compiled.stream loop (server/runner.py)**
- The `try` around `compiled.stream(...)` (`consultants/server/runner.py:136-161`) is for graph-level crashes only. In the lane-failure scenario the node swallowed the exception, so the stream drains normally; the `except` at `:155-161` (`status="failed"`, `_write_failed_artifacts`) does **not** fire.
- Post-stream: `final_state` ("values" mode, `:137-141`) carries `error` + the merged tombstone. Then `node_error = final_state.get("error")` → `terminal_status = "failed" if node_error else "completed"` (`:172-174`). **Any** role's error key — including a single researcher lane's tombstone — flips the whole consultation to `failed`.

**4. Synthesizer input**
- `synthesizer_node` builds messages from `state.get("research") or []` (`consultants/engine/council.py:718-723`). It sees: the surviving lanes' real reports **plus** the literal string `"(researcher lane failed: <exception>)"` as one research bullet. Pipeline continues (critic → `route_after_critic` `:366-390` → synthesizer), so the synthesizer runs and produces a real `final_answer` over partial evidence — the degraded mode the tombstone comment at `:557-561` designed for.

**5. Storage**
- `ConsultationResult` is built with the real `final_answer` but `status=terminal_status` and `error=node_error` (`consultants/server/runner.py:177-199`) → `storage.write_consultation` (`consultants/engine/storage.py:200-208`) atomically writes `summary.md` / `transcript.md` / `metadata.json` (renderers `:117-120`, `:123-158`, `:166-169`). The tombstone turn appears in the transcript via `render_transcript` (`:129-136`).

## Verdict

**The council still produces a coherent (degraded) answer.** The synthesizer runs over surviving lanes + the tombstone; `summary.md` contains a genuine synthesized `final_answer`. But because a single lane's tombstone sets the state-level `error` key, `runner.py:174` labels the **entire consultation `status=failed`** — the poller (`SessionState.public_dict`, `consultants/server/app.py:61-70`) reports `failed` with `error: "researcher failed: ..."` while the artifact on disk holds a usable answer. Perceived failure mode: the user is told the consultation failed and may discard it, even though it completed with reduced evidence coverage. Total failure (`status=failed` with placeholder answer) only occurs when the synthesizer itself dies (`council.py:736-747`) or the graph crashes outside a node (`runner.py:155-161`).

## ONE hardening change

`consultants/server/runner.py:172-174` — make researcher-lane failures non-terminal: when `node_failed == "researcher"` and `final_state.get("final_answer")` is non-empty, set `terminal_status = "completed"` (optionally a `"degraded"` status), keeping `error` in metadata for the transcript, since the tombstone already guarantees the synthesizer saw the gap (`council.py:557-561`). Lane crashes are already handled in-band; the current code double-penalizes them at the runner level.

---

## Researcher (round 1)

## Findings

**1. CouncilState additive reducers (`consultants/engine/graph.py`)**
- `research`, `turns`, token counters use `Annotated[..., operator.add]` concat/sum reducers so parallel Send lanes merge additively (`consultants/engine/graph.py:64-72`); `error` and `_role_failed` are plain Optional channels — **last-value-wins**, so with multiple failing lanes only one lane's error string survives (`consultants/engine/graph.py:74-75`).
- Each Send lane starts with `"research": []`, `"turns": []`, zeroed counters so its delta is additive only (`consultants/engine/graph.py:377-381`).

**2. Failure path for the two named failure modes — the lane does NOT crash the graph**
- `run_loop` (`claude_hooks/agent_loop/runner.py:138`) contains **no try/except at all** (grep for `except|raise` in that file returns only `def run_loop`). A chat_fn timeout exhausting retries raises from `claude_hooks/get_advice/chat_client.py:302` (or `:305`), and a non-string tool output raises `TypeError` at `len(output)` in `claude_hooks/agent_loop/runner.py:126` — both propagate straight out of `run_loop`.
- `researcher_node` wraps the `loop_runner` call in try/except at `consultants/engine/council.py:547-555` and returns a **tombstone partial** (`consultants/engine/council.py:562-573`): `error="researcher failed: ..."`, `_role_failed="researcher"`, plus a synthetic `research` entry and `RoleTurn` containing `"(researcher lane failed: {e})"`. This is explicitly the fix for a prior audit finding ("without this, a crashed lane silently contributes nothing", `consultants/engine/council.py:557-561`).
- The tombstone merges via the additive reducers; the surviving lanes' reports concatenate alongside it. The `error` channel persists because subsequent nodes (critic at `council.py:705-712`, synthesizer at `council.py:753-758`) return partials without an `error` key, so LangGraph retains the prior value.
- Caveat: an exception **outside** the try block — e.g. in `build_researcher_messages` at `consultants/engine/council.py:521-527` — escapes the node and aborts the whole stream; that hits the hard-fail path in `runner.py:155-161` instead.

**3. `compiled.stream` loop (`consultants/server/runner.py`)**
- The try/except at `consultants/server/runner.py:136-161` only fires when the graph itself raises; the tombstone path never reaches it. `final_state` is the last `"values"` payload (`runner.py:139-141`).
- After the stream drains, `node_error = final_state.get("error")` → **`terminal_status = "failed" if node_error else "completed"`** at `consultants/server/runner.py:172-174`. This is the pivotal line: one lane's tombstone flips the whole consultation to `failed` even though every node ran and the synthesizer produced a full answer.

**4. Synthesizer input — what it actually sees**
- `synthesizer_node` calls `build_synthesizer_messages(state["question"], plan, state.get("research") or [], critique, ...)` (`consultants/engine/council.py:718-723`).
- `build_synthesizer_messages` renders each `research` entry as `"\nRESEARCHER REPORT (round {i}):\n{r}"` (`consultants/engine/council.py:253-254`). So the synthesizer sees the tombstone string `"(researcher lane failed: <exc>)"` formatted as a **legitimate researcher report**, indistinguishable in structure from a real lane's output. There is no explicit "this lane crashed" marker beyond the prose, and no signal that coverage is incomplete beyond reading that text.

**5. Storage (`consultants/engine/storage.py`)**
- Artifacts are fully written regardless: `storage.write_consultation` at `consultants/server/runner.py:199` → atomic tmp+rename writes of `summary.md`/`transcript.md`/`metadata.json` (`consultants/engine/storage.py:200-207`, `_atomic_write_text` at `:174-192`). `status="failed"` and `error` land in the YAML front-matter (`storage.py:108-110`) and `metadata.json` (`:166-169`). The tombstone turn appears in `transcript.md` via `render_transcript` (`storage.py:129-136`).
- The hard-fail branch `_write_failed_artifacts` (`runner.py:211-237`) writes a placeholder `"(consultation failed: {exc})"` summary — only reached if the stream itself raises.

**6. User-visible outcome (`consultants/server/app.py`)**
- Poll returns `status`/`error`/`progress` from `SessionState.public_dict` (`consultants/server/app.py:61-75`); the runner sets `state.status = terminal_status` and `state.error = node_error` at `runner.py:201-202`. `_run_with_state` (`app.py:313-324`) is only the outer belt for a runner crash, which doesn't happen here.

## Verdict

**Degraded-with-visible-gap, but mislabeled as `status=failed`.** The council does not hard-fail: all nodes complete, artifacts are written, and `summary.md` contains the synthesizer's real answer built from the surviving lanes plus a tombstone string. But `runner.py:174` promotes any lane-level `error` to terminal `status=failed`, so the CLI/poll reports a failed consultation whose `/result` endpoint simultaneously returns a coherent (if incomplete) answer. The failure mode the user perceives: a session flagged "failed" with `error="researcher failed: ..."` even though the summary body reads as a complete answer — the status and the artifact contradict each other, and downstream consumers keying on `status` will discard a usable answer.

## ONE hardening recommendation

Change the terminal-status logic at **`consultants/server/runner.py:174`**: distinguish terminal (synthesizer/planner) failures from lane-level ones — mark `failed` only when `final_state.get("final_answer")` is empty or the placeholder, or `_role_failed == "synthesizer"`; otherwise report `status="completed"` with the lane error surfaced as a non-fatal `warnings` field (e.g. into `ConsultationResult.error` retained but `status` computed from `final_answer` presence). Catching inside `run_loop` (`claude_hooks/agent_loop/runner.py:138`) is the wrong layer — `researcher_node`'s tombstone at `council.py:555` already contains the failure correctly; the bug is purely the runner's status promotion.

---

## Critic (round 2)

All load-bearing citations verified by direct reads:

- Reducers: `research`/`turns` `Annotated[list, operator.add]`, counters int-sum (`consultants/engine/graph.py:64,68-72`); `error`/`_role_failed` plain last-write-wins channels (`graph.py:74-75`) — matches round 2.
- Tombstone: `researcher_node` try/except at `consultants/engine/council.py:547-555`, tombstone return at `:562-573` with `error="researcher failed: ..."`, `_role_failed="researcher"`, `research: ["(researcher lane failed: {e})"]` — exact.
- Origin: `len(output)` at `claude_hooks/agent_loop/runner.py:126` after bare `tool_executor(...)` at `:122`; retry-exhaustion raises at `claude_hooks/get_advice/chat_client.py:285-287` and `:302` — exact.
- Runner: graph-crash guard `runner.py:136-161`; pivotal `terminal_status = "failed" if node_error else "completed"` at `consultants/server/runner.py:174`; `ConsultationResult` built `:177-199`, `write_consultation` at `:199`, `state.status` set `:201-202` — exact.
- Synthesizer: input from `state.get("research") or []` (`council.py:718-723`), tombstone rendered as a normal `RESEARCHER REPORT (round i)` bullet (`council.py:254`); synthesizer's own failure path returns placeholder `final_answer` + `error` (`council.py:736-747`) — exact.
- Storage/poll: `status`/`error` into YAML front-matter (`consultants/engine/storage.py:108-110`), transcript renders the tombstone turn (`:129-136`), poll exposes `status`/`error` via `public_dict` (`consultants/server/app.py:61-75`) — exact.

Round 1's self-flagged line ambiguity for the researcher_node cite was resolved by round 2's correct `council.py:547-555`. The verdict holds: single lane crash → synthesizer still produces a real `final_answer` over surviving lanes + tombstone string, artifacts fully written, but `runner.py:174` promotes the lane error to terminal `status=failed` — status and artifact contradict. The single hardening change (recompute terminal status at `runner.py:174` from `final_answer` presence / `_role_failed=="synthesizer"` rather than any `error`) is correctly located and correctly rejects the alternative of catching inside `run_loop`, since the tombstone already contains the failure in-band.

CORRECTIONS: (none — round 1's bracketed line-note was self-corrected in round 2; all round-2 claims verified.)

DECISION: ready

---

## Synthesizer

**Bottom line: the council does NOT hard-fail. It produces a real, synthesized answer — but the runner mislabels the entire consultation `status=failed`, so the user is told it failed even though the artifacts on disk contain a coherent answer.**

**1. CouncilState reducers (`consultants/engine/graph.py`)**

- `research`, `turns`, and token counters are additive: `Annotated[list, operator.add]` / int-sum (`consultants/engine/graph.py:64`, `:68-72`), so parallel Send lanes merge by concatenation/sum. Each lane starts with empty lists and zeroed counters (`graph.py:377-381`).
- `error` and `_role_failed` are plain Optional channels — **last-write-wins** (`graph.py:74-75`); with multiple failing lanes, only one error string survives.

**2. Exception origin → node boundary**

- `run_loop` has **no try/except at all** (`claude_hooks/agent_loop/runner.py:138`). Chat retry exhaustion raises `RuntimeError` from `claude_hooks/get_advice/chat_client.py:285-287`, `:302`; a non-string tool output raises `TypeError` at `len(output)` (`claude_hooks/agent_loop/runner.py:126`, bare call at `:122`). Both propagate straight out of `run_loop`.
- But the exception never reaches LangGraph: `researcher_node` wraps the loop call in try/except (`consultants/engine/council.py:547-555`) and returns a **tombstone partial** (`council.py:562-573`): `error="researcher failed: ..."`, `_role_failed="researcher"`, a synthetic `research` entry `"(researcher lane failed: {e})"`, and a zero-token RoleTurn. The reducers see a tombstone *entry*, not a missing key — the graph proceeds normally. (Caveat: an exception outside the try, e.g. in `build_researcher_messages` at `council.py:521-527`, escapes and hits the hard-fail path instead.)
- The `error` channel persists through critic and synthesizer because those nodes return partials without an `error` key (`council.py:705-712`, `:753-758`).

**3. `compiled.stream` loop (`consultants/server/runner.py`)**

- The try/except at `consultants/server/runner.py:136-161` is a graph-crash guard only; it does **not** fire in this scenario — the stream drains normally.
- The pivotal line is after the stream: `terminal_status = "failed" if node_error else "completed"` (`consultants/server/runner.py:172-174`). One lane's tombstone sets `error`, which **flips the whole consultation to `failed`** even though every node ran.

**4. Synthesizer input**

- `synthesizer_node` builds messages from `state.get("research") or []` (`consultants/engine/council.py:718-723`), so it sees the surviving lanes' real reports **plus** the tombstone string rendered as `"\nRESEARCHER REPORT (round {i}):\n{r}"` (`council.py:254`) — structurally indistinguishable from a real lane's output; nothing flags coverage as incomplete beyond the prose. The pipeline continues (critic → route → synthesizer) and produces a genuine `final_answer` over partial evidence. Total failure (placeholder answer) only occurs if the synthesizer itself dies (`council.py:736-747`) or the graph crashes outside a node.

**5. Storage (`consultants/engine/storage.py`)**

- Artifacts are fully written regardless: `write_consultation` (`consultants/server/runner.py:199`) does atomic tmp+rename writes of `summary.md` / `transcript.md` / `metadata.json` (`consultants/engine/storage.py:200-207`; `_atomic_write_text` `:174-192`). The tombstone turn appears in the transcript via `render_transcript` (`storage.py:129-136`), and `status="failed"` + the error land in the YAML front-matter and metadata (`storage.py:108-110`, `:166-169`). The placeholder-writer `_write_failed_artifacts` (`runner.py:211-237`) is only reached if the stream itself raises.

**User-perceived failure mode**

Degraded-but-mislabeled. The poll reports `status=failed` with `error="researcher failed: ..."` via `SessionState.public_dict` (`consultants/server/app.py:61-75`, set at `runner.py:201-202`), while `/result` and the on-disk `summary.md` simultaneously contain a coherent answer built from the surviving lanes. Status and artifact contradict each other; downstream consumers keying on `status` will discard a usable consultation that merely lost one lane of evidence.

**ONE hardening change**

Fix the status promotion at **`consultants/server/runner.py:174`**: mark `failed` only for terminal failures — `final_answer` empty/placeholder or `_role_failed == "synthesizer"`; when the failure is lane-level (`_role_failed == "researcher"`) and a real `final_answer` exists, report `completed` (or a `degraded` status) with the lane error surfaced as a non-fatal warning in metadata. Catching inside `run_loop` (`claude_hooks/agent_loop/runner.py:138`) is the wrong layer — `researcher_node`'s tombstone (`council.py:555`) already contains the failure in-band; the bug is purely the runner treating any lane `error` as terminal.

---
