---
session_id: csl-2026-10-22-2031-06e2
created: "2026-10-22T20:31:55"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "minimax-m3.1-flash:cloud"
  planner: "minimax-m3.1-flash:cloud"
  researcher: "minimax-m3.1-flash:cloud"
  synthesizer: "minimax-m3.1-flash:cloud"
topology: council
effort: high
duration_seconds: 468.69
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-zEA5
---

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
