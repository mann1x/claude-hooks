---
session_id: csl-2026-10-22-1818-a218
created: "2026-10-22T18:18:56"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "minimax-m3.1-flash:cloud"
  planner: "minimax-m3.1-flash:cloud"
  researcher: "minimax-m3.1-flash:cloud"
  synthesizer: "minimax-m3.1-flash:cloud"
topology: council
effort: high
duration_seconds: 561.02
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-6kcB
---

A single exception inside `run_loop` does **not** abort the Send superstep: it is converted into a researcher tombstone, the graph can still synthesize a potentially useful degraded answer, but the runner subsequently marks the consultation `status="failed"`—so the normal user flow reports a hard failure and withholds the answer that was actually written.

### 1. Exact failure path

1. `researcher_node` calls `loop_runner(...)` inside a `try` and catches `Exception`; the `run_loop` invocation itself is therefore contained (`consultants/engine/council.py:547 [no run_loop at this line; line is in researcher_node]`, `consultants/engine/council.py:555`). The catch returns:
   - `error="researcher failed: ..."`,
   - `_role_failed="researcher"`,
   - one `research` entry containing `(researcher lane failed: ...)`,
   - and a matching transcript turn (`consultants/engine/council.py:562-572`).

2. `research` and `turns` use additive reducers, so the failed lane’s tombstone and turn coexist with surviving lanes’ reports and turns (`consultants/engine/graph.py:64`, `consultants/engine/graph.py:68`). `error` and `_role_failed` have no reducer (`consultants/engine/graph.py:74-75`); the repository therefore does not establish deterministic concurrent-write behavior if multiple lanes fail simultaneously. That external LangGraph detail is unverified, but it does not affect the single-failure path.

3. The runner drains `compiled.stream(..., stream_mode=["updates", "values"])` and refreshes `final_state` from each `values` payload (`consultants/server/runner.py:136-140`). Because the researcher returned a normal state update instead of raising, execution does not enter the graph-crash handler at `consultants/server/runner.py:155-161`; the critic and synthesizer can still run.

4. The synthesizer receives the merged `research` entries and constructs one `RESEARCHER REPORT (round N)` section for each, inserting the entry text verbatim (`consultants/engine/council.py:245-266`, `consultants/engine/council.py:253-254`). Thus it sees:
   - the surviving lanes’ reports,
   - the literal `(researcher lane failed: ...)` tombstone,
   - the planner’s plan,
   - and the critic’s verdict (`consultants/engine/council.py:249-256`).

   The prompt does not require disclosure of missing coverage and instead says not to mention roles or the process (`consultants/engine/council.py:156-168`). Consequently, evidence establishes that a final answer is produced, but **not** that it will be coherent or disclose the failed lane.

5. After the stream drains, the runner finds the researcher’s `error` and unconditionally chooses `terminal_status="failed"` whenever it is present (`consultants/server/runner.py:171-174`). It nevertheless stores the synthesizer’s `final_answer` in the result, records `status="failed"` and the researcher error, and writes the artifacts (`consultants/server/runner.py:176-199`).

6. The normal artifact writer writes `summary.md`, `transcript.md`, and `metadata.json` (`consultants/engine/storage.py:200-208`). The summary front matter contains `status` and `error`, while its body is `result.final_answer` (`consultants/engine/storage.py:97-120`). The resulting `summary.md` therefore contains the synthesized answer body under `status: failed` and the researcher error.

### 2. What the user perceives

The API status response exposes both `status` and `error` (`consultants/server/app.py:61-75`), and the result endpoint will serve the written summary even for a failed session (`consultants/server/app.py:246-279`). However, the documented client flow fetches the result only after `status: completed` (`.claude/skills/consultants/SKILL.md:104-120 [unverified — file not found]`); for `status: failed`, it tells the agent to surface the role error rather than print the synthesized answer (`.claude/skills/consultants/SKILL.md:134-147 [unverified — file not found]`).

So the user-facing failure mode is:

> “The consultation failed because the researcher failed.”

The surviving research and synthesized answer remain in the session artifacts, but the normal workflow does not retrieve or display them.

This differs from a genuine graph crash: if an exception escapes outside the protected `loop_runner` call—for example, during import, message assembly, or post-loop extraction—it reaches the runner’s graph-crash handler, which writes a placeholder `(consultation failed: ...)` with no turns (`consultants/engine/council.py:497-499`, `consultants/engine/council.py:521-527`, `consultants/engine/council.py:575-576`, `consultants/server/runner.py:155-160`, `consultants/server/runner.py:224-235`).

### 3. Non-string tool-executor sub-case

- A `None` or integer result raises `TypeError` at `len(output)` inside `run_loop` (`claude_hooks/agent_loop/runner.py:122-127 [no run_loop at this line; line is in execute_tool_calls]`). It is caught by the same researcher boundary and therefore follows the tombstone → synthesized answer → `status="failed"` path (`consultants/engine/council.py:547 [no run_loop at this line; line is in researcher_node]-566`).
- A sized non-string such as a dictionary or list passes `len()` and is placed directly into the tool-message content without a string validation or coercion at this boundary (`claude_hooks/agent_loop/runner.py:122-133`). Whether a later client rejects it is not established by the reviewed code.
- The production executor catches tool failures and coerces results with `str(result)`, so this non-string case is unreachable through that implementation (`claude_hooks/caliber_proxy/tools.py:420-439`).
- Separately, if the loop returns an empty final text rather than raising, the forced-summary path can emit the “no findings” or “failed to produce a written report” stub (`consultants/engine/council.py:585-639`). That path does not set `error`, so the runner classifies it as `completed` (`consultants/server/runner.py:171-174`).

### Exactly one hardening change

Change the terminal-status decision at `consultants/server/runner.py:174` so a recoverable researcher failure with a substantive synthesized answer is a completed, degraded consultation; retain failure when the synthesizer failed or no substantive answer exists:

```python
terminal_status = (
    "failed"
    if node_error and (
        node_failed == "synthesizer"
        or not final_state.get("final_answer", "").strip()
    )
    else "completed"
)
```

This preserves `node_error` in the session and artifacts (`consultants/server/runner.py:190`, `consultants/server/runner.py:202`) while allowing the normal result-fetch path to deliver the degraded answer (`.claude/skills/consultants/SKILL.md:104-120 [unverified — file not found]`).
