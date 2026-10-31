# Consultation transcript — csl-2026-10-22-2031-06e2

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. Inspect `consultants/engine/council.py` around `researcher_node`, `synthesizer_node`, `initial_state`, and `run_loop` calls to determine whether exceptions from `agent_loop.runner.run_loop` are caught inside the node and what partial state each node returns; this file was identified but its contents were not read.
2. Verify the reducer semantics in `consultants/engine/graph.py:50-80` and the Send construction in `consultants/engine/graph.py:342-405`, specifically whether a failed lane contributes any `research`, `turns`, token, or error fields and whether sibling lane results can still merge.
3. Trace the `compiled.stream(...)` exception boundary in `consultants/server/runner.py:124-161`, including whether LangGraph propagates a parallel-node exception immediately and whether `final_state` can contain a valid synthesized answer at that point.
4. Inspect the synthesizer prompt/message construction in `consultants/engine/council.py` to establish exactly which successful and missing lane outputs appear in its input when one researcher lane crashes; this symbol location remains unchecked because `council.py` was not read.
5. Inspect `consultants/engine/storage.py` for `ConsultationResult` and `write_consultation`, and compare the normal artifact path at `consultants/server/runner.py:176-203` with the crash path at `consultants/server/runner.py:211-237` to determine whether artifacts expose a failed consultation, a degraded answer, or both; the storage implementation remains unchecked.
6. Inspect `claude_hooks/agent_loop/runner.py` around `run_loop` to identify the exact unhandled-exception boundary for exhausted HTTP retries and non-string tool-executor results, and check whether either condition is caught before propagating into `council.researcher_node`; this file was identified but not read.
7. Review `consultants/server/app.py` and the session response/status serialization around `state.status`, `state.error`, and `final_answer` to determine the user-visible failure mode and select exactly one hardening change at a confirmed `path:line`; `app.py` was identified but not read.

---

## Researcher (round 1)

## Findings (researcher lane failure in the Send fan-out)

**1. The exception is caught inside the node — the graph does not abort.**
- `consultants/engine/council.py:547-554` wraps the `loop_runner(payload, cwd, config=cfg, ...)` call in `try:`; `consultants/engine/council.py:555` catches `Exception` and logs `researcher_node failed`.
- `claude_hooks/agent_loop/runner.py:230` (`final = chat_fn(payload)`) and `claude_hooks/agent_loop/runner.py:300` (`execute_tool_calls(...)` → `claude_hooks/agent_loop/runner.py:122` `output = tool_executor(name, args_str, cwd)`) contain **no** internal try/except, so an exhausted HTTP retry or a `TypeError` from `len(output)` on a non-string tool result propagates out of `run_loop` and lands in that `except`.
- `consultants/engine/trace.py:179-181` (`TracedChat.chat` re-raises) and `consultants/engine/trace.py:220-222` (`traced_tool` re-raises) do not swallow it either.
- Caveat: langgraph internals are not vendored (`consultants/pyproject.toml:17` pins `langgraph>=0.2,<0.4`); I could not read them. But `consultants/engine/graph.py:428-429` compiles with no retry policy, and the tombstone design at `consultants/engine/council.py:557-561` only makes sense if the node catches — consistent.

**2. Reducers: the failed lane contributes a tombstone, siblings still merge.**
- `consultants/engine/graph.py:64` `research: Annotated[list, operator.add]`; `:68` `turns`; `:69-72` `research_rounds_used` / `critic_reroutes_used` / token counters are `operator.add`.
- Failed lane returns `research: [err_text]`, `research_rounds_used: 1`, one `RoleTurn`, **no** token deltas — `consultants/engine/council.py:562-573`.
- Per-lane counters are zero-initialized in the `Send` payload — `consultants/engine/graph.py:377-381` — so additive merging stays correct.
- `error` and `_role_failed` are plain (last-value) channels — `consultants/engine/graph.py:74-75` — so the failing lane's `error` sticks (successful lanes never write that key).

**3. What the synthesizer actually sees.**
- `state["research"]` = surviving lane reports + `"(researcher lane failed: <exc>)"` (tombstone text at `consultants/engine/council.py:562`, injected at `:566`).
- `consultants/engine/council.py:253-254` renders each entry as `RESEARCHER REPORT (round N): <text>` — the tombstone is therefore presented to the LLM **as if it were a research report**, with no lane identity and no topic (the lane's `plan_item` is in state but never echoed; see `consultants/engine/council.py:515-523`).
- The synthesizer prompt explicitly forbids discussing the process — `consultants/engine/council.py:164-165` — so a "silent gap" answer is the likely output. Uncertain (LLM behavior), but the prompt does not ask it to surface unresolved gaps.

**4. `compiled.stream` boundary: no crash, but the run is labelled failed.**
- `consultants/server/runner.py:137-138` streams `stream_mode=["updates","values"]`; `runner.py:139-140` keeps the last `values` payload as `final_state` — which contains the synthesizer's `final_answer` (written at `consultants/engine/council.py:753-754`).
- `consultants/server/runner.py:155-161` (the `except` → `_write_failed_artifacts`) is **not** reached, because the node swallowed the exception.
- `consultants/server/runner.py:172-174`: `node_error = final_state.get("error")` → `"researcher failed: <exc>"` → `terminal_status = "failed"`. Then `runner.py:186-199` writes the artifacts with the **real** `final_answer` and `status="failed"`; `runner.py:201-202` sets `state.status="failed"`, `state.error=node_error`.
- So: the answer exists, the session says failed. `consultants/server/app.py:61-75` (`public_dict`) exposes `status`/`error`; `consultants/server/app.py:275-279` (`/v1/consult/{sid}/result`) returns `summary_markdown` (the answer) + `metadata` (status failed).

**5. Storage / artifact writer.**
- `consultants/engine/storage.py:117-120` — `summary.md` = YAML front-matter + the synthesized answer body. Front-matter carries `status: failed` and `error: researcher failed: ...` — `consultants/engine/storage.py:108-110`.
- `consultants/engine/storage.py:129-136` — the transcript renders each `turn`; the failed lane's turn content is the tombstone string. All three researcher turns have `round=1` (each Send lane starts with `research_rounds_used: 0` at `consultants/engine/graph.py:379` → `consultants/engine/council.py:507-508` sets `this_round=1`), and `_is_multi_round` is true — `consultants/engine/storage.py:130-133`, `:161-163` — so the transcript shows three identical `## Researcher (round 1)` headers with no lane attribution.
- The lane's already-collected tool work is lost: `run_loop` raised, so no return value, and `consultants/engine/council.py:563-573` emits only the tombstone — no partial report.

**6. Side effect worth flagging (not the recommendation).** Each lane adds `research_rounds_used: 1` (`consultants/engine/council.py:567`), so a 3-lane fan-out yields `research_rounds_used=3`; at `high` effort `caps.researcher_rounds_max == 3` (`consultants/engine/council.py:72`) → `consultants/engine/council.py:383-389` blocks any critic re-route. At `low`/`medium` the critic node is dropped anyway (`consultants/server/runner.py:58-63`).

**7. Only the "all lanes died" variant is catastrophic.** If the single (non-fanned) researcher fails, `research` = `[tombstone]` only → the synthesizer answers from the plan alone; the answer is unevidenced but still stored, still `status=failed`. If instead an exception escaped the node body (e.g. `_extract_text(final)` at `consultants/engine/council.py:575` on a non-dict return, which is *outside* the try), then `compiled.stream` raises, `consultants/server/runner.py:155-161` fires, and `_write_failed_artifacts` (`consultants/server/runner.py:211-237`) writes `final_answer="(consultation failed: ...)"`, `turns=[]` — all evidence lost.

## Answer to the question (for the synthesizer)

The council **does not** fail as a whole for a lane crash: the node catches it (`consultants/engine/council.py:555`), the graph continues, the synthesizer runs, and a real (degraded) `final_answer` is written to `summary.md`. But the run still surfaces as `status=failed` with `error="researcher failed: <exc>"` (`consultants/server/runner.py:172-174`, `:201-202`) — the metadata and the body of `summary.md` contradict each other.

Perceived failure mode: the user gets a plausible answer that silently omits the failed lane's topic (the tombstone is presented as a "RESEARCHER REPORT" with no lane identity, `consultants/engine/council.py:253-254`, `:562`), the transcript shows three indistinguishable `## Researcher (round 1)` blocks (`consultants/engine/storage.py:130-133`), and the session/index reports `failed` — so the user cannot tell "one of three sub-investigations never ran" from "the whole council died".

## Recommended single hardening change

**`consultants/engine/council.py:562` — make the researcher tombstone self-identifying instead of anonymous.** Change `err_text = f"(researcher lane failed: {e})"` to include the lane index and the lane's plan items, e.g. `f"(researcher lane {state.get('lane_idx')} failed: {e} — uncovered plan item: {state.get('plan_item')})"`, and mirror that in the `error` string at `:564`. Rationale: the tombstone is already routed into the additive `research` channel and rendered as a report at `consultants/engine/council.py:253-254`, so making it name the missing scope is the single change that (a) lets the synthesizer name the gap it must not paper over, (b) makes the transcript attributable to a lane, and (c) keeps the run's `status=failed`/`error` string (`consultants/server/runner.py:172`) actionable instead of generic.

(Evidence-sufficient but deliberately not recommended as the single change: `consultants/server/runner.py:174` currently maps any node `error` to `status=failed`, conflating "degraded answer produced" with "no answer produced" — a separate semantics decision.)

---

## Researcher (round 1)

## Findings — sub-research lane 2 (storage / run_loop boundary / user-visible status)

### 1. Where the exception actually dies (not where the plan assumed)

- `claude_hooks/agent_loop/runner.py:230` — `final = chat_fn(payload)` is **not** wrapped in any try/except. `ChatClient.chat` raises on exhausted retry budget: `RuntimeError(f"ollama chat HTTP {e.code}: {err_body}")` at `claude_hooks/get_advice/chat_client.py:285-287`, or re-raises the last `URLError`/`OSError` at `claude_hooks/get_advice/chat_client.py:288-302` (retry budget `DEFAULT_MAX_RETRIES = 8`, `chat_client.py:31`, loop `chat_client.py:212`). Socket timeout is an `OSError` subclass → retried → re-raised after the budget.
- `claude_hooks/agent_loop/runner.py:122` — `output = tool_executor(name, args_str, cwd)` is likewise unguarded; `claude_hooks/agent_loop/runner.py:126` does `len(output)`, so a `None`/int result raises `TypeError` there, and `claude_hooks/agent_loop/runner.py:128` stores it as a tool message `content` (`claude_hooks/agent_loop/runner.py:129-134`). `claude_hooks/agent_loop/runner.py:299-301` calls `execute_tool_calls` with no guard.
- Production caveat: the wired executor is `claude_hooks/caliber_proxy/tools.py:420-439` (`execute`), which **never raises and always returns a str** (`tools.py:439`: `return result if isinstance(result, str) else str(result)`), and `consultants/engine/trace.py:217-222` re-raises anything the executor raises. So in the shipped stack the realistic trigger is the HTTP/retry exhaustion, not a non-string tool result; the non-string case is only reachable via an injected executor.
- **The exception is caught one level up, inside the node**: `consultants/engine/council.py:547-554` wraps `loop_runner(...)` in `try`, and `consultants/engine/council.py:555-573` catches `Exception` and returns a **tombstone** dict (`error`, `_role_failed`, `research: [err_text]`, `research_rounds_used: 1`, one `turns` entry) instead of re-raising. `err_text = f"(researcher lane failed: {e})"` at `consultants/engine/council.py:562`.

### 2. Reducers / Send fan-out (`consultants/engine/graph.py`)

- Fan-out: `consultants/engine/graph.py:358-385` builds one `Send("researcher", {...})` per lane, each carrying empty `research`/`turns` and zeroed counters (`graph.py:376-381`); `FANOUT_MIN_ITEMS = 2` / `FANOUT_MAX_LANES = 3` at `consultants/engine/council.py:297` and `council.py:307`.
- Additive channels: `research: Annotated[list, operator.add]` (`consultants/engine/graph.py:64`), `turns: Annotated[list, operator.add]` (`graph.py:68`), `research_rounds_used` / `critic_reroutes_used` / `total_*_tokens` as `operator.add` ints (`graph.py:69-72`).
- Single-value channels: `error: Optional[str]` and `_role_failed: Optional[str]` have **no reducer** (`consultants/engine/graph.py:74-75`).
- Consequence A (verified by code, not by execution): a single failed lane writes one value to `error` → no conflict → the graph proceeds normally to critic/synthesizer.
- Consequence B (predicted LangGraph semantics, **not verifiable here** — langgraph is not installed in this env, `consultants/pyproject.toml:17` pins it to the separate `claude-hooks-consultants` env; `tests/test_consultants_council.py:5-7` confirms the main env has no langgraph): two lanes failing in the same superstep write `error`/`_role_failed` twice → LangGraph's default last-value channel raises `InvalidUpdateError`, which escapes `compiled.stream` and lands in the runner's crash handler. Flagging as medium-confidence inference, not verified behavior.
- Side effect of the tombstone: it adds `research_rounds_used: 1` (`council.py:567`) exactly like a successful lane (`council.py:660`), so N lanes always inflate the round counter by N, which feeds the critic re-route cap check at `consultants/engine/council.py:373-378`.

### 3. `consultants/server/runner.py` — `compiled.stream` boundary

- Loop: `consultants/server/runner.py:137-138` (`compiled.stream(initial, stream_mode=["updates", "values"])`); `final_state` is taken from the last `"values"` payload (`runner.py:139-140`).
- The **only** crash boundary is `consultants/server/runner.py:155-161`: any exception escaping the graph → `state.status = "failed"`, `state.error = f"graph crashed: {e}"`, `_write_failed_artifacts(...)`, `return`. A node-level exception (our lane crash) never reaches this line, because `council.py:555` already converted it to a tombstone.
- After the stream drains, `node_error = final_state.get("error")` and `terminal_status = "failed" if node_error else "completed"` at `consultants/server/runner.py:172-174`. **This is where a degraded-but-answered council is mislabeled as failed**: the tombstone's `error` string is present in `final_state`, so `terminal_status == "failed"` even though the synthesizer ran and produced `final_answer`.
- Artifacts on this path (`consultants/server/runner.py:177-199`): `final_answer=final_state.get("final_answer", "")` (`runner.py:186`), `turns=list(final_state.get("turns") or [])` (`runner.py:187`), `status=terminal_status` (`runner.py:189`), `error=node_error` (`runner.py:190`) → so `summary.md` contains **both** the real degraded answer *and* `status: failed` + `error: researcher failed: …` in YAML front matter (`consultants/engine/storage.py:97-114`, body at `storage.py:117-120`). The failed lane's tombstone also appears in `transcript.md` as a researcher turn (`storage.py:123-158`).
- True graph-crash path for contrast: `consultants/server/runner.py:211-237` writes `final_answer=f"(consultation failed: {exc})"`, `turns=[]`, `models={}` (`runner.py:221-225`) — no answer at all, and the write itself is guarded (`runner.py:234-237`).

### 4. What the synthesizer actually sees

- `synthesizer_node` builds messages from `state.get("research") or []` (`consultants/engine/council.py:718-723`) via `build_synthesizer_messages` (`council.py:245-266`).
- Each merged research entry is rendered **as an ordinary report**: `f"\nRESEARCHER REPORT (round {i}):\n{r.strip()}"` (`council.py:253-254`). The failed lane's entry therefore appears as e.g. `RESEARCHER REPORT (round 2):\n(researcher lane failed: RuntimeError('ollama chat HTTP 500: ...'))` — the synthesizer is told nothing about *which* sub-plan/lane is missing, and the lane label is the arbitrary merge order of the fan-out lanes (no `plan_item`/`lane_idx` survives into `research`).
- The synthesizer prompt forbids mentioning the council/roles (`SYNTHESIZER_SYSTEM`, `council.py:156-168`), so it typically folds the raw error string into prose or drops it → the user gets a **confidently-worded answer with a silent coverage gap**, or a pasted raw exception string.
- The critic, when enabled, sees the same tombstone as a research report (`build_critic_messages`, `council.py:231-242`) and may re-route on it.

### 5. User-visible failure mode (the decisive part)

- `SessionState.public_dict()` returns `status` + `error` (`consultants/server/app.py:61-75`, field defaults `app.py:53-56`); CLI `status` prints it verbatim (`consultants/cli.py:142-145`).
- The `/consultants` skill only fetches the result when `status: completed` (`/consultants/skills/consultants/SKILL.md:104-118`) and on `status: failed` instructs the agent to surface only the role + error and suggest `/consultants--config` (`SKILL.md:136-147`). So on a single-lane crash the user is told **"the council failed (researcher failed: …)"** and is never shown the degraded answer that is sitting in `summary.md` — even though `GET /v1/consult/{sid}/result` would happily return it (`consultants/server/app.py:246-279`).
- Net: the consultation does **not** fail end-to-end (synthesizer runs, artifacts contain a usable answer), but the API surfaces it as `status=failed`, and the user-perceived outcome is "the council broke and gave me nothing" instead of "the council answered, but one of three sub-investigations is missing".

### 6. One hardening change (recommended)

**`consultants/server/runner.py:174`** — stop deriving terminal status from "any node recorded an error"; derive it from "did we get an answer".

Current: `terminal_status = "failed" if node_error else "completed"` (`consultants/server/runner.py:174`).

Recommended shape (one site): compute `has_answer = bool((final_state.get("final_answer") or "").strip())` and set `terminal_status = "failed" if (node_error and not has_answer) else "completed"`, while keeping `state.error = node_error` (`runner.py:202`) and `error=node_error` in the artifact (`runner.py:190`) so the lane failure is still visible in `metadata.json` / the status payload.

Why this one: it makes the single-lane-crash path report the *actual* outcome the code already produced (a degraded answer) instead of a blanket `failed`, which is what makes the skill skip the result (`SKILL.md:104-147`). It is also a strictly smaller blast radius than changing the reducer semantics in `consultants/engine/graph.py:74-75`, and it does not touch the normal path (`node_error is None` → `completed`).

Deliberately **not** recommended (noted for the synthesizer): the concurrent-write hazard on the reducer-less `error` / `_role_failed` channels (`consultants/engine/graph.py:74-75`) would need a separate fix (accumulating error list + reader update in `consultants/server/runner.py:172-174`) and is the fix for the 2+-lane-failure crash variant, not the single-lane case in the question.

---

## Critic (round 2)

DECISION: ready

The evidence is sufficient for the single-lane case: `researcher_node` catches the `run_loop` exception and returns a tombstone with `error`, `_role_failed`, one research entry, one turn, and no token usage (`consultants/engine/council.py:547-573`). The additive reducers merge that tombstone with successful sibling lanes (`consultants/engine/graph.py:60-72`), while `error` and `_role_failed` are single-value channels (`consultants/engine/graph.py:74-75`). The synthesizer receives the merged list and formats every entry as a “RESEARCHER REPORT,” so it sees the failure text as an ordinary report without the failed lane’s scope (`consultants/engine/council.py:245-266`, `consultants/engine/council.py:718-723`). The graph therefore normally reaches the synthesizer and produces a `final_answer` (`consultants/engine/council.py:748-758`); `compiled.stream` only enters its whole-graph crash path if an exception escapes the graph (`consultants/server/runner.py:136-161`). On the single-lane tombstone path, the server writes both the degraded answer and `status="failed"` (`consultants/server/runner.py:172-202`), and storage renders the answer body plus failed/error metadata (`consultants/engine/storage.py:97-120`). Thus the consultation is not wholly lost, but it is not reliably coherent: it is a degraded answer with a missing research scope, while the user-facing status says the council failed. Exactly one hardening is warranted: change `consultants/engine/council.py:562` so the tombstone includes the zero-based `lane_idx` plus one and the lane’s `plan_item`; that makes the missing scope visible to the synthesizer and attributable in the transcript without changing status semantics.

---

## Synthesizer

**The graph continues and normally produces a degraded `final_answer`; it does not enter the whole-graph crash handler. However, the server labels the consultation `status="failed"`, so the persisted result contains both a plausible answer and contradictory failure metadata. The answer is not reliably coherent because the synthesizer cannot tell which research scope was lost.**

### 1. Exception and additive reducers

An HTTP failure from `chat_fn(payload)` is unguarded in `run_loop` (`claude_hooks/agent_loop/runner.py:230`), while an executor result such as `None` or an integer can fail at `len(output)` (`claude_hooks/agent_loop/runner.py:122`, `claude_hooks/agent_loop/runner.py:126`). Such an exception propagates to `researcher_node`, whose `try/except` catches it (`consultants/engine/council.py:547-555`). The shipped executor normally converts non-string results to strings, so that variant is primarily reachable through an injected executor (`claude_hooks/caliber_proxy/tools.py:420-439`).

The failed lane returns:

- `error="researcher failed: …"` and `_role_failed="researcher"`;
- one `research` entry containing `(researcher lane failed: …)`;
- one researcher turn containing that tombstone;
- `research_rounds_used=1`; and
- no token deltas.

That return is constructed at `consultants/engine/council.py:562-573`.

The reducer semantics then determine the merge:

- `research` and `turns` concatenate, so successful sibling reports and the failed lane’s tombstone coexist (`consultants/engine/graph.py:64`, `consultants/engine/graph.py:68`).
- Round and token counters sum; therefore the failed lane contributes one round but zero tokens (`consultants/engine/graph.py:69-72`, `consultants/engine/council.py:567-573`). With three fan-out lanes, for example, `research_rounds_used` becomes three rather than one.
- `error` and `_role_failed` are single-value channels, not accumulating channels (`consultants/engine/graph.py:74-75`). The failed lane’s error therefore remains present after successful siblings merge, because those siblings do not write `error`.

### 2. `compiled.stream` path

The server streams both updates and values and retains the last values payload as `final_state` (`consultants/server/runner.py:135-140`). Since `researcher_node` catches the `run_loop` exception and returns a tombstone update, the stated lane failure does **not** escape the graph and does not reach the whole-graph exception handler at `consultants/server/runner.py:155-161`.

Instead, the graph proceeds to the synthesizer, whose normal return supplies `final_answer` and its turn (`consultants/engine/council.py:748-758`). After the stream drains, the server sees the tombstone’s `error` and unconditionally derives:

```python
terminal_status = "failed" if node_error else "completed"
```

at `consultants/server/runner.py:171-174`. It then stores the real `final_answer` alongside `status="failed"` and the researcher error (`consultants/server/runner.py:186-202`).

Thus, this is **not** the catastrophic graph-crash path. The catastrophic path writes the placeholder `(consultation failed: …)` with no turns (`consultants/server/runner.py:211-237`). One boundary qualification: `_extract_text(final)` is outside the researcher’s `try` block (`consultants/engine/council.py:555 [no _extract_text at this line; line is in researcher_node]`, `consultants/engine/council.py:575`), so a different exception occurring after `run_loop` returns could still escape and trigger the whole-graph failure path; that is not the exception described in the question.

### 3. What the synthesizer actually sees

The merged `research` list contains the successful sibling reports plus the generic tombstone. `synthesizer_node` passes that list directly into `build_synthesizer_messages` (`consultants/engine/council.py:718-723`).

Every entry is rendered identically as a report:

```text
RESEARCHER REPORT (round N):
<entry text>
```

(`consultants/engine/council.py:245-255`). Consequently, the failed lane appears approximately as:

```text
RESEARCHER REPORT (round 2):
(researcher lane failed: RuntimeError(...))
```

The failed lane’s `plan_item` is not included. Although the node knows the lane’s `lane_idx` and `plan_item` (`consultants/engine/council.py:515-523`), the tombstone contains only the exception text (`consultants/engine/council.py:562`). The synthesizer therefore sees **that some report failed, but not which sub-question or research scope disappeared**. It may omit the gap, mention the raw exception, or otherwise produce a plausible-looking answer; the LLM’s handling of the tombstone is not deterministic.

### 4. Storage and the user-visible failure mode

The normal artifact path persists:

- the synthesized `final_answer`;
- all merged turns, including the failed researcher tombstone;
- `status="failed"`; and
- `error="researcher failed: …"`.

Those values are assembled at `consultants/server/runner.py:176-199`. `summary.md` consequently contains failed/error metadata in YAML front matter (`consultants/engine/storage.py:97-110 [no md at this line; line is in _yaml_front_matter]`) and the degraded synthesized answer as its body (`consultants/engine/storage.py:117-120`). The transcript also contains the tombstone as an ordinary researcher turn (`consultants/engine/storage.py:123-136`); because the failed turn does not identify its lane, the transcript does not show which sub-investigation was lost.

At the API level, the session exposes the failed status and error (`consultants/server/app.py:61-75`), while the result endpoint can still return the stored summary together with that metadata (`consultants/server/app.py:275-279`). The practical user-visible inconsistency is therefore:

> “The council failed with a researcher error,” while an answer synthesized without the missing lane quietly exists in the result artifact.

The user is told the whole consultation failed, rather than being told, “the answer is degraded because one scoped research lane was lost.”

### Exactly one hardening change

**Change the tombstone construction at `consultants/engine/council.py:562` to include the one-based lane number and its `plan_item`**, for example:

```python
err_text = (
    f"(researcher lane {int(state.get('lane_idx') or 0) + 1} failed: {e}; "
    f"missing scope: {state.get('plan_item')})"
)
```

The required lane metadata is already available at `consultants/engine/council.py:515-520`. Because that same `err_text` becomes both the synthesizer’s `research` entry and the failed researcher turn (`consultants/engine/council.py:566-570`), this single change makes the missing scope visible to the synthesizer and attributable in the persisted transcript.

---
