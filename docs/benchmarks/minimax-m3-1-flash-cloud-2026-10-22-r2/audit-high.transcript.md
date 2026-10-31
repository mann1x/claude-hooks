# Consultation transcript — csl-2026-10-22-0816-0e41

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. Verified `consultants/engine/council.py:546-573`: `researcher_node` wraps `loop_runner(...)` in `try/except Exception`, so an exception escaping `agent_loop.runner.run_loop` is caught and converted into a tombstone update containing `error`, `_role_failed="researcher"`, one `research` entry, one research round, and one `RoleTurn`; it does not escape that node as an unhandled graph exception.

2. Verified the Send fan-out state deltas in `consultants/engine/graph.py:358-385` and reducers in `consultants/engine/graph.py:60-75`: each lane starts with zeroed additive counters/lists, then successful reports and the failed-lane tombstone are concatenated through `operator.add`; `research`, `turns`, `research_rounds_used`, and token totals survive concurrent lane merging, while `error` and `_role_failed` are ordinary non-additive fields whose last-writer behavior still needs confirmation in the LangGraph runtime.

3. Verify the exact compiler behavior in `consultants/server/runner.py` by locating `compiled.stream(...)`, its surrounding `try/except`, and the `status` assignment: determine whether any exception that bypasses the `researcher_node` catch is converted into `status="failed"`, and whether a completed stream containing `state["error"]` is nevertheless marked successful.

4. Verify what survives into synthesis in `consultants/engine/council.py:245-266` and `consultants/engine/council.py:715-723`: inspect the constructed user message to confirm it contains every additive `research` entry—including `(researcher lane failed: ...)`—plus the plan and optional critique, and determine whether the non-additive `error` field itself is ever sent to the synthesizer.

5. Read `consultants/engine/storage.py` to trace how `final_answer`, `state["error"]`, and the additive `turns` list are serialized into the consultation artifact, specifically whether the failed researcher tombstone is visible to artifact readers and whether artifact writing can itself change the externally reported status.

6. Inspect `claude_hooks/agent_loop/runner.py` to verify the two named failure origins: an exhausted HTTP retry that raises from `chat_fn`, and a non-string `tool_executor` result that violates an internal string assumption; record the exact exception sites and whether either can escape beyond `researcher_node`’s broad catch.

7. Cross-check the server’s status and response construction against the above state behavior, then select exactly one hardening location—preferably the point where `researcher_node` returns its tombstone at `consultants/engine/council.py:562-573` if the remaining files show that `error` is treated as terminal, otherwise the narrow server catch/status block in `consultants/server/runner.py`—and document the resulting user-visible outcome. This step is unfinished because the turn ended before `consultants/server/runner.py`, `consultants/engine/storage.py`, and `claude_hooks/agent_loop/runner.py` could be read.

---

## Researcher (round 1)

## Research report — researcher-lane exception in the Send fan-out

### 1. The exception never escapes the node (it is a tombstone, not a crash)

- `consultants/engine/council.py:547-554` — `researcher_node` calls `loop_runner(...)` (i.e. `claude_hooks/agent_loop/runner.py:138` `run_loop`) inside `try:`.
- `consultants/engine/council.py:555-573` — `except Exception as e:` returns a tombstone update: `"error": f"researcher failed: {e}"`, `"_role_failed": "researcher"`, `"research": [err_text]` where `err_text = f"(researcher lane failed: {e})"` (`council.py:562`), `"research_rounds_used": 1`, and one `RoleTurn` (`council.py:568-572`). No exception leaves the node, so LangGraph does not abort the superstep from this path.
- The two named origins do reach that `except`:
  - HTTP retry budget exhausted → `claude_hooks/get_advice/chat_client.py:285-287` raises `RuntimeError` (non-retryable HTTP) or re-raises `URLError/OSError` at `chat_client.py:302` after `max_retries=8` (`chat_client.py:31`, loop at `chat_client.py:212`); `TracedChat.chat` re-raises (`consultants/engine/trace.py:179-181`); the call site inside the loop is `claude_hooks/agent_loop/runner.py:230`.
  - non-string `tool_executor` result → `claude_hooks/agent_loop/runner.py:122` returns the raw object, `runner.py:126` does `len(output)` (TypeError for `int`), and `consultants/engine/trace.py:224-232` also does `len(out or "")` in its `finally` (TypeError for `int`) — either way an exception escapes `run_loop` into the same `except`.

### 2. Reducers: additive fields survive, `error`/`_role_failed` are not additive

- `consultants/engine/graph.py:64-72` — `research`, `turns`, `research_rounds_used`, `critic_reroutes_used`, `total_prompt_tokens`, `total_completion_tokens` are `Annotated[..., operator.add]`.
- `consultants/engine/graph.py:74-75` — `error: Optional[str]` and `_role_failed: Optional[str]` are **plain** (last-writer) fields, not additive.
- `consultants/engine/graph.py:358-385` — each `Send` lane starts with `"research": []`, `"turns": []`, `"research_rounds_used": 0`, token counters 0, so lane deltas merge without double counting.
- Consequence: the tombstone's `research` entry **is** concatenated into the shared list, so the synthesizer sees the failed lane as one more "report"; the `error` string survives into the merged state because successful lanes never write `error` (`council.py:658-664`), and no node clears it (synthesizer success path `council.py:753-758`).
- **Unverified risk (evidence insufficient):** with **two** lanes failing in the same superstep, two writes land on the non-additive `error`/`_role_failed` channels. LangGraph's `LastValue` channel is documented to reject concurrent writes, but `langgraph` is neither vendored nor installed in this repo (`glob **/langgraph/**/*.py` → no matches; `consultants/pyproject.toml:17` pins it as an external dep, `requirements-dev.txt` has no `langgraph`), so I could not confirm the runtime behavior. Flagging, not asserting.

### 3. `compiled.stream` loop: the graph is *not* marked failed by the exception

- `consultants/server/runner.py:137-138` — `for mode, payload in compiled.stream(initial, stream_mode=["updates", "values"])`.
- `consultants/server/runner.py:155-161` — the `except Exception` (graph-level crash) is the **only** path that sets `state.status = "failed"` and calls `_write_failed_artifacts`. A caught researcher-lane exception never reaches it.
- `consultants/server/runner.py:172-174` — **this is the pivot**: `node_error = final_state.get("error")`, `node_failed = final_state.get("_role_failed")`, and `terminal_status = "failed" if node_error else "completed"`. Because the tombstone set `error`, a single failed lane flips the whole session to `failed` even though the graph ran to completion and the synthesizer produced an answer.
- `consultants/server/runner.py:186-199` — `final_answer=final_state.get("final_answer", "")` is still written to disk via `storage.write_consultation(...)`, with `status="failed"` and `error=node_error`.

### 4. What the synthesizer actually sees

- `consultants/engine/council.py:718-723` — `synthesizer_node` passes only `question`, `plan`, `state.get("research")`, `state.get("critique")` into `build_synthesizer_messages`. The `error` field itself is **never** sent.
- `consultants/engine/council.py:253-254` — every `research` entry becomes a `"\nRESEARCHER REPORT (round {i}):\n{r}"` block, so the failed lane appears verbatim as `(researcher lane failed: <exc>)` in the user message.
- `consultants/engine/council.py:164-165` — the synthesizer system prompt says *"Do NOT mention the council, the roles, or the process"*, and the tombstone text is a bare parenthetical with no instruction to flag it. So the synthesizer can easily produce a confidently-degraded answer that silently omits the missing lane — exactly the risk the code comment at `council.py:557-561` was trying to prevent.
- `consultants/engine/council.py:670` — the critic also sees the tombstone (it reads `state.get("research")`).

### 5. Storage artifacts

- `consultants/engine/storage.py:97-114` — YAML front-matter carries `status: failed` and `error: researcher failed: ...` (`storage.py:108-110`).
- `consultants/engine/storage.py:117-120` — the body is still the synthesizer's real answer (`result.final_answer`).
- `consultants/engine/storage.py:129-136` — `transcript.md` renders the failed lane's `RoleTurn` as a `## Researcher` section whose content is `(researcher lane failed: ...)`; with multiple researcher turns the header becomes `## Researcher (round 1)` (`storage.py:130-133`, `_is_multi_round` at `storage.py:161-163`).
- `consultants/engine/storage.py:166-169` — `metadata.json` = `asdict(result)`, so `status`, `error`, and all `turns` are recorded.

### 6. Secondary degradation: the failed lane can never be re-run

- `consultants/engine/council.py:567` (failed lane) and `council.py:660` (successful lane) each add `research_rounds_used += 1`. With 3 lanes → `research_rounds_used == 3`.
- `consultants/engine/council.py:383-389` — at `effort=high` `caps.researcher_rounds_max == 3` (`council.py:72`), so `route_after_critic` logs "round cap hit" and routes straight to the synthesizer; the critic cannot re-route to redo the failed lane.

### 7. What the user perceives

- `consultants/server/app.py:61-75` — the poll payload exposes `status` and `error`; `state.status = terminal_status` at `consultants/server/runner.py:201`, `state.error = node_error` at `runner.py:202`.
- `.claude/skills/consultants/SKILL.md:136-147` — Step 6 ("handle failures") is keyed on `status: failed` and instructs the agent to surface the role error and **not** go to Step 5 (`SKILL.md:106` "Once `status: completed`"). So the degraded-but-real answer that was written to `summary.md` is never fetched or shown.
- `consultants/server/app.py:263-274` — the `/result` endpoint would happily serve the artifacts even in the failed state (it only 409s while `status == "running"`); the answer is on disk, just unreachable through the skill's normal flow.

**Verdict:** the council does **not** fail end-to-end — the graph completes, the synthesizer answers, and artifacts are written. But the session is stamped `status=failed` purely because of the presence of `error` in the merged state (`runner.py:174`), and the skill's failure branch discards the answer. The user's perceived failure mode is: *"the consultation failed — researcher failed: <exc>"*, with no answer at all, even though a partially-grounded answer (built on the surviving lanes) exists on disk.

### 8. Recommended single hardening change

**`consultants/server/runner.py:172-174`** — derive `terminal_status` from whether an answer was *delivered*, not from the presence of any mid-pipeline role error:

```python
node_error = final_state.get("error")
node_failed = final_state.get("_role_failed")
final_answer = (final_state.get("final_answer") or "").strip()
delivered = bool(final_answer) and node_failed != "synthesizer"
terminal_status = "completed" if delivered else "failed"
```

Rationale: a researcher-lane failure is already surfaced (a) inside the transcript via the tombstone turn (`council.py:568-572` → `storage.py:129-136`) and (b) in `summary.md` front-matter as `error:` (`storage.py:109-110`), both of which the skill prints (`SKILL.md:120-121`). Flipping the session to `completed` when an answer exists makes the skill fetch and show that answer (with its `error:` caveat) instead of reporting a bare failure. Keep `status=failed` for the genuinely-terminal cases (synthesizer failure, `council.py:735-747`; or graph crash, `runner.py:155-161`). The separate follow-ups I did **not** recommend as "the one change" are: making `error`/`_role_failed` additive at `consultants/engine/graph.py:74-75` (removes the unverified concurrent-write hazard and allows per-lane attribution), and passing `state.get("error")` into `build_synthesizer_messages` at `council.py:718-723` so the answer itself names the gap instead of hiding it.

---

## Researcher (round 1)

## Findings (sub-research lane 2)

### 1. Where the exception is actually caught

- `run_loop` calls the model at `claude_hooks/agent_loop/runner.py:230` (`final = chat_fn(payload)`) and the tool at `claude_hooks/agent_loop/runner.py:122` (`output = tool_executor(name, args_str, cwd)`). Neither call is wrapped in a try/except inside the loop, so **any** exception from either escapes `run_loop` to its caller.
- The researcher's `chat_fn` is `chat_client.chat` (`consultants/engine/council.py:552`). Retry exhaustion surfaces as `RuntimeError("ollama chat HTTP {code}: …")` at `claude_hooks/get_advice/chat_client.py:285-287` (HTTP) or a re-raised `URLError/OSError` (incl. socket timeout) at `claude_hooks/get_advice/chat_client.py:288-302`; budget is `max_retries = 8` (`chat_client.py:31`, loop at `chat_client.py:212`). `TracedChat.chat` re-raises unchanged (`consultants/engine/trace.py:177-181`).
- Non-string tool output: `runner.py:124-127` evaluates `len(output)` eagerly in the `log.info` call → `TypeError` for `None`/`int`; the tool result never reaches the message list. (In production this is unreachable: `claude_hooks/caliber_proxy/tools.py:439` coerces to `str`, and the runner wires `traced_tool(tool_execute)` at `consultants/server/runner.py:106`.)
- Both origins are swallowed by the **broad** `except Exception` in `researcher_node` (`consultants/engine/council.py:547-555`); the tombstone is returned at `council.py:562-573`. **Neither can escape beyond `researcher_node`** — the node returns normally, so the LangGraph superstep succeeds and the graph continues to critic → synthesizer.

### 2. Reducer / channel behavior of the tombstone

- `research` and `turns` are additive (`consultants/engine/graph.py:64`, `consultants/engine/graph.py:68`); the tombstone contributes `research: [err_text]` (`council.py:566`) and one `RoleTurn` (`council.py:568-572`) → the failed lane is **visible to the synthesizer and to artifact readers**, not silently dropped.
- `error` and `_role_failed` have **no reducer** (`graph.py:74-75`) → `LastValue` channels. A single failing lane writes them once (only the failing lane emits those keys; the success path at `council.py:658-664` does not), so one lane failure is safe. **Two lanes failing in the same superstep would write the same `LastValue` key twice** → LangGraph `InvalidUpdateError` → the whole `compiled.stream` raises. (Unverified: `langgraph` is not in `requirements-dev.txt` and not importable in the main test env, so I could not read its channel source to confirm the exact error text/behavior.)
- Fan-out lanes start with zeroed additive counters (`graph.py:377-381`) and each returns `research_rounds_used: 1` (`council.py:567`) → after 3 lanes the merged value is 3, which is ≥ `caps.researcher_rounds_max` (1 at medium, `council.py:70`) → `route_after_critic` skips the re-route and goes straight to the synthesizer (`council.py:383-389`).

### 3. What the synthesizer actually sees

- `build_synthesizer_messages` renders every entry of `state["research"]` as a numbered "RESEARCHER REPORT (round N)" block (`council.py:253-254`, `council.py:245-266`) → the synthesizer **does** see `(researcher lane failed: <exc>)` as one of the reports.
- But `SYNTHESIZER_SYSTEM` forbids mentioning "the council, the roles, or the process" (`council.py:164-166`) → the synthesizer typically folds the gap into prose or omits it, i.e. the answer is **degraded but not necessarily labeled as degraded** to the user. The synthesizer itself does not fail, so `final_answer` is a normal answer.

### 4. Server status / artifacts

- After the stream drains, `node_error = final_state.get("error")` and `terminal_status = "failed" if node_error else "completed"` (`consultants/server/runner.py:172-174`). **The researcher tombstone's `error` key therefore flips the whole consultation to `status="failed"`** even though the synthesizer produced a full answer.
- Artifacts are then written with `status=terminal_status` and `error=node_error` (`runner.py:186-199`): `summary.md` front matter gets `status: failed` + `error: …` (`consultants/engine/storage.py:108-110`) but the **body is still the synthesizer's real answer** (`storage.py:119`); `transcript.md` shows the tombstone turn (`storage.py:129-136`); `metadata.json` carries `error` and `status` via `asdict` (`storage.py:166-169`).
- `state.status`/`state.error` are set from the same values (`runner.py:201-202`), surfaced by `GET /v1/consult/{sid}` (`consultants/server/app.py:61-75`, `app.py:231-235`) and printed by `claude-consultants status` (`consultants/cli.py:142-145`).
- **Artifact writing can flip the status**: `storage.write_consultation` at `runner.py:199` is unguarded; if it raises, the exception escapes `run_council` and is caught by `app.py:320-324` → `status="failed"`, `error="runner crashed: …"`, with `summary.md`/`transcript.md` possibly written but `metadata.json` missing → `/v1/consult/{sid}/result` then 404s ("artifacts missing", `app.py:263-274`). Contrast `_write_failed_artifacts`, which does wrap the write in try/except (`runner.py:234-237`).
- Total-loss escalation path (graph-level crash, e.g. the double-`LastValue` scenario): `runner.py:155-161` sets `status="failed"`, `error="graph crashed: …"` and calls `_write_failed_artifacts`, which **overwrites** `summary.md` with `final_answer = "(consultation failed: {exc})"` (`runner.py:224-227`) — the council's real work is destroyed.

### 5. The failure mode the user perceives

- The synthesizer's answer exists and is on disk, but the session reports `status: failed` with `error: "researcher failed: <exc>"`. The `/consultants` skill treats `failed` as terminal (`.claude/skills/consultants/SKILL.md:92-93`) and at Step 6 (`.claude/skills/consultants/SKILL.md:136-144`) surfaces only the role + error and suggests `/consultants--config` to change the model — **it never fetches or prints the answer** (Step 5 is gated on `completed`, `SKILL.md:104-127`).
- So the user perceives: "the council failed on a researcher lane, go change the researcher model" — while 2 of 3 lanes succeeded and a good (but quietly incomplete) answer sits in `summary.md`, retrievable only manually via `claude-consultants result <sid>` (`cli.py:148-151`, `app.py:275-279`) or `/consultants--show`.
- Token accounting also under-reports: the failed lane contributes 0 tokens (`council.py:570-571`) and no `total_*_tokens` delta.

### 6. Existing test coverage of this path

- `tests/test_consultants_council.py:514-529` asserts the tombstone sets `error` ("researcher failed"); `tests/test_consultants_council.py:531-561` asserts the additive tombstone (`research`, `turns`, `research_rounds_used`). Any hardening that stops writing the terminal `error` key must update `tests/test_consultants_council.py:529`.

## Recommended single hardening change

**`consultants/engine/council.py:564`** — stop writing the *terminal* `error` channel from a single failed researcher lane. Change the tombstone to record the lane failure in a non-terminal, lane-scoped key (e.g. `"research_failures": [err_text]`) while keeping the existing tombstone text at `council.py:566` and the `RoleTurn` at `council.py:568-572` unchanged.

Why this one:
- It restores the intended semantics: `runner.py:174` computes `terminal_status` from `state["error"]`, and `error` is only legitimately terminal for the **synthesizer** (`council.py:737`) — the one case where no answer exists. A single lane dying is not a council failure.
- The degraded-but-labeled answer then reaches the user: `status="completed"` → the skill proceeds to Step 5 and prints the synthesizer's answer; the missing lane is still visible to the synthesizer (via the additive `research` reducer, `graph.py:64` + `council.py:566`) and in `transcript.md` (`storage.py:129-136`).
- It also removes the multi-lane `LastValue` write collision on `error`/`_role_failed` (`graph.py:74-75`), which is the one path that escalates a lane failure into total loss of the whole consultation (`runner.py:155-161` → `runner.py:224`).
- Optional follow-up (not required by this change): have `runner.py:172-174` copy `research_failures` into a non-fatal warning field on `SessionState` so the user still learns a lane died without the run being marked failed.

---

## Critic (round 2)

DECISION: ready

For the specified single-lane failure, the graph itself does not abort: `researcher_node` catches the exception and returns a tombstone containing `error`, `_role_failed`, one `research` entry, one `research_round`, and one `RoleTurn` (`consultants/engine/council.py:547-573`). The additive reducers concatenate the failed lane’s report and turn with the successful lanes’ deltas (`consultants/engine/graph.py:60-75`; fan-out initial deltas are zeroed at `consultants/engine/graph.py:358-385`). The synthesizer receives the tombstone as an ordinary numbered researcher report, because it passes `state.get("research")` into `build_synthesizer_messages` (`consultants/engine/council.py:718-723`; `consultants/engine/council.py:245-266`); the `error` field itself is not included. Thus synthesis can produce a real, but potentially silently degraded, answer.

The server’s `compiled.stream` loop does not enter its graph-crash handler for this path, because the researcher exception was caught inside the node (`consultants/server/runner.py:136-161`). After the stream completes, however, `node_error = final_state.get("error")` and `terminal_status = "failed" if node_error else "completed"` (`consultants/server/runner.py:171-174`), so the consultation is externally reported as `status=failed` even if the synthesizer returned a final answer. The result still carries that answer and the failed-lane turn into `ConsultationResult` (`consultants/server/runner.py:186-199`); the artifact writer therefore records the real answer while the summary metadata records `status: failed` and the researcher error (`consultants/engine/storage.py:97-120`, `consultants/engine/storage.py:129-169`). The user-facing failure mode is therefore “researcher failed” with no delivered answer through the normal completed-result path, although a partially grounded answer exists in the artifacts.

The single recommended hardening change is at `consultants/server/runner.py:172-174`: derive terminal status from a non-empty final answer and the failing role, rather than from the mere presence of `state["error"]`; retain `failed` for synthesizer failures or graph exceptions. For example, treat a non-empty `final_answer` with `_role_failed != "synthesizer"` as `completed`, while preserving the researcher error in the artifact metadata. This makes the degraded answer reachable without changing the additive tombstone behavior. The concurrent two-failed-lane `LastValue` interaction was not independently verified, but it is outside the single-lane question. The skill file was also unavailable, so the report’s specific claim about the skill’s failure branch remains unverified; the server/API status behavior itself is directly established by `consultants/server/runner.py:171-202` and `consultants/server/app.py:61-75`.

UNVERIFIED: the skill’s exact presentation of failed consultations — `.claude/skills/consultants/SKILL.md:92-147` was cited by the researcher but was not available for spot-checking.

---

## Synthesizer

The graph completes and can produce a partially grounded answer, but the API/artifact status is nevertheless `failed`; for a single researcher-lane exception, the consultation is not aborted at the graph level.

### Exact failure path

1. **The lane exception is converted into a tombstone.**
   - `researcher_node` calls `loop_runner` inside `try`; any `Exception` raised by `agent_loop.runner.run_loop` is caught at `consultants/engine/council.py:547-555 [no run_loop at this line; line is in researcher_node]`.
   - The catch returns:
     - `error="researcher failed: ..."`,
     - `_role_failed="researcher"`,
     - one additive `research` entry containing `(researcher lane failed: ...)`,
     - `research_rounds_used=1`, and
     - one failed `RoleTurn` (`consultants/engine/council.py:562-573`).
   - Thus, the exception does **not** escape the Send-lane node or abort that graph superstep (`consultants/engine/council.py:555-573`).

   The two example origins can reach that catch:
   - An exhausted HTTP retry raises from the chat call, which is invoked at `claude_hooks/agent_loop/runner.py:230`; the broad node catch handles it (`consultants/engine/council.py:547-555 [no run_loop at this line; line is in researcher_node]`).
   - A non-string tool result is returned raw at `claude_hooks/agent_loop/runner.py:122`, after which `len(output)` at `claude_hooks/agent_loop/runner.py:124-127` can raise a `TypeError`; it likewise reaches the node catch (`consultants/engine/council.py:547-555 [no run_loop at this line; line is in researcher_node]`).

2. **Additive state survives the failed lane.**
   - `research`, `turns`, `research_rounds_used`, and token totals use additive reducers (`consultants/engine/graph.py:60-72`).
   - Each Send lane starts with empty research/turn lists and zeroed counters, so the tombstone is concatenated with successful lanes rather than replacing them (`consultants/engine/graph.py:358-385`).
   - `error` and `_role_failed` are ordinary last-writer fields rather than additive fields (`consultants/engine/graph.py:74-75`). For the specified **single** failed lane, successful lanes do not overwrite them.

3. **`compiled.stream` completes normally, but the server interprets the tombstone error as terminal.**
   - The server drains `compiled.stream(initial, stream_mode=["updates", "values"])` inside a graph-level `try` (`consultants/server/runner.py:136-154`).
   - The graph-crash handler, which sets `status="failed"` and writes failure artifacts, is reached only if an exception escapes the stream loop (`consultants/server/runner.py:155-161`). A caught researcher exception does not reach that handler.
   - After the stream completes, the server reads `error` and `_role_failed` and computes `terminal_status = "failed" if node_error else "completed"` (`consultants/server/runner.py:171-174`).
   - Because the researcher tombstone populated `error`, the externally reported status becomes `failed` even if synthesis returned a final answer (`consultants/server/runner.py:172-174`, `consultants/server/runner.py:186-202`).

4. **What the synthesizer actually receives.**
   - The synthesizer is given `state.get("research")`, but not the separate `error` field (`consultants/engine/council.py:718-723 [no error at this line; line is in synthesizer_node]`).
   - Every research-list item is rendered as a numbered `RESEARCHER REPORT (round N)` block (`consultants/engine/council.py:245-266`).
   - Consequently, the synthesizer sees text equivalent to:
     ```text
     RESEARCHER REPORT (round N):
     (researcher lane failed: <exception>)
     ```
     but it does not receive `error="researcher failed: ..."` as a separate field.
   - The synthesis prompt says not to mention the council, roles, or process (`consultants/engine/council.py:164-165`). Therefore, the synthesizer may either acknowledge the missing evidence or silently omit it. The result is potentially degraded and not reliably labeled as incomplete; it is not guaranteed to be a coherent complete answer.

5. **What the artifact writer stores.**
   - The result passed to storage contains the synthesizer’s `final_answer`, all merged turns, `status="failed"`, and the researcher error (`consultants/server/runner.py:186-199 [no md at this line; line is in run_council]`).
   - `summary.md` front matter records `status: failed` and the error (`consultants/engine/storage.py:97-110 [no md at this line; line is in _yaml_front_matter]`), while its body is still the synthesizer’s actual answer (`consultants/engine/storage.py:117-120`).
   - `transcript.md` renders each `RoleTurn`, including the failed researcher tombstone (`consultants/engine/storage.py:123-135 [no RoleTurn at this line; line is in render_transcript]`).
   - `metadata.json` is built from the result object and therefore preserves its status, error, final answer, and turns (`consultants/engine/storage.py:166-169`).

### User-visible outcome

This is a **degraded answer stored behind a failed consultation**, not a total loss of computation:

- The graph reaches synthesis, and a potentially usable answer is written into `summary.md` (`consultants/server/runner.py:186-199 [no md at this line; line is in run_council]`; `consultants/engine/storage.py:117-120`).
- The session nevertheless reports `status=failed` with `error="researcher failed: ..."` (`consultants/server/runner.py:172-202`).
- The normal completed-result path is therefore not entered, so the user experiences “the consultation failed on a researcher lane” rather than receiving the synthesized answer. The exact presentation by the unavailable skill file could not be verified.
- The answer may also be quietly incomplete because the synthesizer receives the failure as one report among others and is instructed not to discuss the internal process (`consultants/engine/council.py:164-165`, `consultants/engine/council.py:245-266`).

### Exactly one hardening change

Change the terminal-status decision at `consultants/server/runner.py:171-174` so a non-empty final answer from a non-synthesizer role failure is considered delivered:

```python
node_error = final_state.get("error")
node_failed = final_state.get("_role_failed")
final_answer = (final_state.get("final_answer") or "").strip()
delivered = bool(final_answer) and node_failed != "synthesizer"
terminal_status = "completed" if delivered else "failed"
```

This preserves `failed` for synthesizer failures and graph exceptions, while allowing a researcher-lane failure to expose the partially grounded answer through the completed-result path. The researcher error remains available in the result and artifact metadata (`consultants/server/runner.py:186-199 [no md at this line; line is in run_council]`; `consultants/engine/storage.py:108-110`).

### Verification limits

- The single-lane path, reducer behavior, synthesizer input, server status calculation, and artifact contents were checked directly from the cited source ranges.
- Concurrent failures from two researcher lanes writing the non-additive `error`/`_role_failed` channels were **not independently verified**; the LangGraph runtime behavior was unavailable. That case is outside the specified single-lane failure.
- The skill’s exact rendering of a failed consultation could not be verified because `.claude/skills/consultants/SKILL.md` was unavailable. The server’s `status=failed` behavior itself is directly established by `consultants/server/runner.py:171-202`.

---
