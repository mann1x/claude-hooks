---
session_id: csl-2026-05-07-0958-04c1
created: "2026-05-07T09:58:39"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "glm-5.1:cloud"
  planner: "glm-5.1:cloud"
  researcher: "glm-5.1:cloud"
  synthesizer: "glm-5.1:cloud"
topology: council
effort: high
duration_seconds: 392.70
status: completed
cwd: /tmp/claude-hooks-bench-bench-baseline-2026-05-07-e4FI
---

The council produces a degraded answer that the user perceives as `status="failed"`, but a partial synthesizer answer still exists in the artifact body — creating a confusing failure mode where the user sees both a failure flag and real (incomplete) content.

**1. CouncilState additive reducers** (`consultants/engine/graph.py:64-75`)
- `research: Annotated[list, operator.add]` (:64) — the failed lane's tombstone `["(researcher lane failed: …)"]` is **concatenated** with surviving lanes' research. The failure string becomes indistinguishable from a real report in the merged list.
- `turns: Annotated[list, operator.add]` (:68), `research_rounds_used` (:69), `total_*_tokens` (:71-72) — all additive; the failed lane's values merge normally (inflating round counts by +1).
- `error: Optional[str]` (:74) and `_role_failed: Optional[str]` (:75) — **non-additive**, default dict-merge (last-write-wins). A lane that succeeds doesn't return `error` in its dict, so it **doesn't overwrite** the failed lane's `error` — that key persists. However, if *multiple* lanes fail, only the last-failing-lane's error survives; earlier ones are silently clobbered.

**2. `runner.py`'s `compiled.stream` loop** (`consultants/server/runner.py:136-161`)
- The researcher exception is caught **inside** `researcher_node` (`council.py:555`), so `run_loop`'s exception never reaches `compiled.stream`. The graph continues through remaining nodes normally; the `except Exception` at `runner.py:155` is only hit by graph-level crashes, not per-node tombstones.

**3. Synthesizer's input messages** (`consultants/engine/council.py:715-723`)
- `synthesizer_node` reads `state.get("research") or []` (:720) — the additive-merged list containing both real research and the tombstone string. `build_synthesizer_messages` (:253) renders every entry as `"RESEARCHER REPORT (round N): …"` with no structural check for failure markers. The synthesizer has **no signal** that one "report" is actually an error; it produces an answer based on partial evidence without acknowledging the gap.

**4. Storage artifact writer** (`consultants/engine/storage.py:117-120, 200-207`)
- After the stream, `runner.py:171-174` evaluates: `terminal_status = "failed" if node_error else "completed"` → `"failed"` because the failed lane's `error` key persisted.
- `write_consultation` is called with `ConsultationResult(status="failed", final_answer=<synthesizer text>)`. `render_summary` (:117-120) writes `final_answer` as the body with `status: "failed"` in YAML front-matter. No guard against non-None `final_answer` exists, and in practice the synthesizer always produces text.

**User-perceived failure mode:** The user sees `status="failed"` in the API/metadata, but `summary.md` still contains a real (incomplete) synthesizer answer. This is confusing — is it failed or not? Additionally, if two lanes fail in a 3-lane fan-out, only the last one's error survives due to the non-additive `error` field, hiding the true extent of degradation.

**Recommended hardening change** — Make `error` and `_role_failed` additive reducers at `consultants/engine/graph.py:74-75`:

```python
error: Annotated[list[str], operator.add]       # was Optional[str]
_role_failed: Annotated[list[str], operator.add]  # was Optional[str]
```

Then update the tombstone returns (`council.py:564`) to wrap values in lists (`"error": ["researcher failed: …"]` instead of a scalar), and update the runner's detection at `runner.py:172` to `if final_state.get("error")` (non-empty list). This ensures all parallel-lane failures accumulate rather than clobber each other, and the synthesizer can later be enhanced to count how many lanes failed vs. succeeded.
