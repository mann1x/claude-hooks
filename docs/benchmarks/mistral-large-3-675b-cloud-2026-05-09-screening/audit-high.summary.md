---
session_id: csl-2026-05-09-0646-44ea
created: "2026-05-09T06:46:23"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "mistral-large-3:675b-cloud"
  planner: "mistral-large-3:675b-cloud"
  researcher: "mistral-large-3:675b-cloud"
  synthesizer: "mistral-large-3:675b-cloud"
topology: council
effort: high
duration_seconds: 66.09
status: completed
cwd: /tmp/claude-hooks-bench-bench-baseline-2026-05-07-5PKY
---

**The council produces a coherent but degraded answer when a single researcher lane fails; the entire consultation only fails if the graph itself crashes.**

- **CouncilState additive reducers** (`consultants/engine/graph.py:64,68-72`): Failed lanes append a tombstone string to `research`/`turns` lists via `operator.add`, so the synthesizer sees them as regular entries.
- **`compiled.stream` loop** (`consultants/server/runner.py:137`): Exceptions in parallel lanes crash the stream unless caught; top-level `try/except` at `155` sets `status="failed"` and writes a tombstone answer.
- **Synthesizer input messages** (`consultants/engine/council.py:254`): Sees the merged `research` list (including tombstones), so it emits a degraded answer citing the failure.
- **Storage artifact writer** (`consultants/engine/storage.py:200-208`): Always writes `summary.md`/`transcript.md`; failed consultations get `final_answer="(consultation failed: {exc})"`.

**User-perceived failure mode:**
- Single-lane exception → degraded answer with error details inline (`status=completed`).
- Graph crash → `status=failed`, user sees a tombstone answer.

**Hardening change:** Add `return_exceptions=True` to `compiled.stream` at `consultants/server/runner.py:137` to prevent lane exceptions from crashing the entire consultation.
