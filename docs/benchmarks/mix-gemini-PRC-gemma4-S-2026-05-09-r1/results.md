# Benchmark — `mix-gemini-PRC-gemma4-S-2026-05-09-r1`

Generated 2026-05-09T10:12:50+02:00 on solidpc.
Engine HEAD (`/shared/dev/claude-hooks`): `5f7d229`
Subject baseline: `bench-baseline-2026-05-07` (commit `83cfd3b`) — frozen worktree at `/tmp/claude-hooks-bench-bench-baseline-2026-05-07-KaZP`
Cloud model snapshot: see `models.json`.

Model pin: per-role config (no override). Snapshot of `claude-consultants config show`:

```json
{
  "ok": true,
  "topology": "council",
  "effort": "xhigh",
  "effort_budget": 5,
  "service": {
    "mode": "always-on",
    "http_port": 38095
  },
  "smart_start": {
    "enabled": false,
    "idle_timeout_seconds": 1800,
    "forwarder_url": "http://127.0.0.1:38096"
  },
  "endpoint": "http://127.0.0.1:38095",
  "roles": {
    "planner": {
      "enabled": true,
      "model": "gemini-3-flash-preview:cloud",
      "ctx_max": null,
      "ctx_max_explicit": false,
      "extra_models": []
    },
    "researcher": {
      "enabled": true,
      "model": "gemini-3-flash-preview:cloud",
      "ctx_max": null,
      "ctx_max_explicit": false,
      "extra_models": [
        "gemma4:31b-cloud",
        "glm-5.1:cloud"
      ]
    },
    "critic": {
      "enabled": true,
      "model": "gemini-3-flash-preview:cloud",
      "ctx_max": null,
      "ctx_max_explicit": false,
      "extra_models": [
        "glm-5.1:cloud"
      ]
    },
    "synthesizer": {
      "enabled": true,
      "model": "gemma4:31b-cloud",
      "ctx_max": null,
      "ctx_max_explicit": false,
      "extra_models": []
    }
  },
  "extras_active": true,
  "mandatory_roles": [
    "synthesizer"
  ],
  "valid_efforts": [
    "high",
    "low",
    "max",
    "medium",
    "xhigh",
    "xmax",
    "xmedium"
  ],
  "valid_service_modes": [
    "always-on",
    "smart-start"
  ]
}
```

## Summary

| Query | Effort | Status | Wall | Prompt tok | Completion tok | LLM calls | Tool calls | sid |
|---|---|---|---|---|---|---|---|---|
| smoke | medium | completed | 30s | 89450 | 2106 | 14 | 12 | `csl-2026-05-09-1009-d160` |
| audit-medium | medium | completed | 61s | 257057 | 8024 | 17 | 30 | `csl-2026-05-09-1009-ef89` |
| audit-high | high | completed | 121s | 142075 | 18644 | 30 | 34 | `csl-2026-05-09-1010-8a10` |

## Per-role breakdown

Wall sums every entry of the role node (researcher in
fan-out fires once per lane; counts accumulate). Token
totals include cloud-model reasoning tokens.

### smoke

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 7.9s | 1 | 237 | 595 | 0 |
| researcher | 29.5s | 12 | 140805 | 2958 | 12 |
| synthesizer | 1.8s | 1 | 1081 | 213 | 0 |

### audit-medium

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 5.2s | 1 | 266 | 797 | 0 |
| researcher | 43.6s | 15 | 381710 | 9206 | 30 |
| synthesizer | 22.1s | 1 | 1737 | 719 | 0 |

### audit-high

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 5.4s | 1 | 410 | 927 | 0 |
| researcher | 3.1m | 27 | 492621 | 27251 | 34 |
| critic | 18.6s | 1 | 2609 | 3585 | 0 |
| synthesizer | 27.1s | 1 | 3010 | 1036 | 0 |

## Files

Per-query artifacts in this directory:

- **smoke**
  - `smoke.summary.md` — synthesizer answer
  - `smoke.transcript.md` — full role transcript
  - `smoke.waterfall.txt` — per-role wall waterfall
  - `smoke.transcript.db` — structured event log (SQLite, v1.1)
  - `smoke.metadata.json` — token totals + retries
- **audit-medium**
  - `audit-medium.summary.md` — synthesizer answer
  - `audit-medium.transcript.md` — full role transcript
  - `audit-medium.waterfall.txt` — per-role wall waterfall
  - `audit-medium.transcript.db` — structured event log (SQLite, v1.1)
  - `audit-medium.metadata.json` — token totals + retries
- **audit-high**
  - `audit-high.summary.md` — synthesizer answer
  - `audit-high.transcript.md` — full role transcript
  - `audit-high.waterfall.txt` — per-role wall waterfall
  - `audit-high.transcript.db` — structured event log (SQLite, v1.1)
  - `audit-high.metadata.json` — token totals + retries

## Per-query grades (manual, per [`EVALUATION.md`](../EVALUATION.md) §3)

| Query | Grade | Notes |
|---|---|---|
| smoke        | PASS | Names all four roles (planner/researcher/critic/synthesizer) with two valid baseline cites; one sentence as required. |
| audit-medium | A    | Six `path:line` import sites with per-site exercisability ruling. Spot-check at HEAD confirms `claude_hooks/providers/pgvector.py:121` (verify) + `bench_recall.py` patterns are real; no fabricated paths. |
| audit-high   | B+   | Q1 trace correct end-to-end (graph reducers → stream loop → synthesizer prompt → storage); Q2 explicit "coherent but degraded answer + `status=failed`" matches reality. Q3 picks a defensive coercion (`"content": output` → `str(output)`) — actionable at HEAD line 172 but doesn't actually plug the traced failure: if `tool_executor` returns a non-string (a scenario the question named), `len(output)` at runner.py:160 crashes first. Compiles, partially addresses → B+ per v1.2 sub-rubric. |

## Per-role grades (manual, per [`EVALUATION.md`](../EVALUATION.md) §3.5)

Aggregated across smoke + audit-medium + audit-high. Per-role models in this mix:
P=gemini-3-flash-preview, R=gemini-3-flash-preview (no fan-out at this effort —
extras only fire at xmedium/xhigh/xmax), C=gemini-3-flash-preview,
S=gemma4:31b-cloud.

| Role | Grade | One-sentence justification |
|---|---|---|
| planner     | A    | 5-8 s wall, clean three-bullet plans across all three queries; gemini-3-flash-preview's strongest role, consistent with prior label grading. |
| researcher  | A    | Per-claim `path:line` in audit-medium (six sites) and audit-high (`graph.py:64,68` / `council.py:555` / `runner.py:137-141` / `council.py:253-254` / `runner.py:174` / `storage.py:108-110`), all valid against the frozen baseline. |
| critic      | A    | Single critic call in audit-high (gemini-3-flash-preview, 18.6 s, 3.6 k completion tokens); the substantive critique enabled the synthesizer to surface the degraded-answer-with-`status=failed` paradox cleanly. |
| synthesizer | A    | gemma4 produced the cleanest audit-high summary of any 2026-05-09 label — explicit "coherent but degraded answer" framing, every reducer + storage cite preserved, no hallucinated symbols. Q3 lands B+ rather than A, but that's a Q3-specific weakness across the whole field, not a synthesizer-quality issue. |

**Mix string:** `P:A R:A C:A S:A`

**Verdict:** PROD-READY (screening — promote to confirmed after N=3)

**Commentary:** This heterogeneous mix (gemini-3-flash-preview for P/R/C, gemma4:31b-cloud for S) is the cheapest A-pick for the synthesizer role observed so far: 3 m 32 s total wall across all three benches, with gemma4 producing the most explicit failure-aware Q1+Q2 framing of any 2026-05-09 label. Synthesis stays in gemma4's documented wheelhouse (the only model that consistently produces actionable Q3 recommendations across all sweeps — though even here Q3 lands B+, not A), while the fast pure-gemini path handles plan/research/critique. Next experiment worth running is N=3 to confirm reproducibility before promoting from `-screening` to a confirmed PROD-READY mix; if the median holds, this becomes the recommended default mix on solidPC + pandorum.
