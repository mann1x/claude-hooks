# Benchmark — `mix-gemini-PRC-gemma4-S-2026-05-09-r3`

Generated 2026-05-09T10:38:32+02:00 on solidpc.
Engine HEAD (`/shared/dev/claude-hooks`): `5f7d229`
Subject baseline: `bench-baseline-2026-05-07` (commit `83cfd3b`) — frozen worktree at `/tmp/claude-hooks-bench-bench-baseline-2026-05-07-yfI2`
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
| smoke | medium | completed | 30s | 90062 | 2769 | 15 | 10 | `csl-2026-05-09-1035-dd20` |
| audit-medium | medium | completed | 46s | 147054 | 7715 | 16 | 24 | `csl-2026-05-09-1035-b617` |
| audit-high | high | completed | 136s | 185234 | 18376 | 33 | 27 | `csl-2026-05-09-1036-b147` |

## Per-role breakdown

Wall sums every entry of the role node (researcher in
fan-out fires once per lane; counts accumulate). Token
totals include cloud-model reasoning tokens.

### smoke

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 4.1s | 1 | 237 | 509 | 0 |
| researcher | 29.0s | 13 | 151597 | 3597 | 10 |
| synthesizer | 2.1s | 1 | 964 | 192 | 0 |

### audit-medium

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 3.9s | 1 | 266 | 592 | 0 |
| researcher | 43.4s | 14 | 268620 | 9113 | 24 |
| synthesizer | 4.3s | 1 | 2126 | 660 | 0 |

### audit-high

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 5.8s | 1 | 410 | 1000 | 0 |
| researcher | 3.5m | 30 | 587330 | 31376 | 27 |
| critic | 9.8s | 1 | 2905 | 1645 | 0 |
| synthesizer | 18.6s | 1 | 3271 | 1246 | 0 |

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
| smoke        | PASS | Names all four roles with `council.py:98-99` cite. |
| audit-medium | A    | Same six sites as r1/r2; terser per-site format ("Exercisable: Yes" only) but no fabrications. |
| audit-high   | B    | Q1+Q2 correct, with extra detail r1/r2 didn't surface (cites `graph.py:74` last-write-wins on `error`, `storage.py:119-120` for body write). Q3 takes a different tack than r1/r2: short-circuit synthesis when `state.get("error")` is set, returning `"(Incomplete answer: ...)"` from `synthesizer_node`. **Overcorrects** — replaces any synthesized answer with an error string whenever any researcher lane fails, throwing away potentially valid synthesis from surviving lanes. Compiles + addresses the misleading-answer mode but at material UX cost (compare gemma4-r2 prompt augmentation, A — keeps synthesis + adds awareness). B per v1.2 sub-rubric: addresses the failure mode but with significant UX regression. |

## Per-role grades (manual, per [`EVALUATION.md`](../EVALUATION.md) §3.5)

| Role | Grade | One-sentence justification |
|---|---|---|
| planner     | A | 5-7 s walls; same clean three-bullet plans across all three queries. |
| researcher  | A | Strongest path:line coverage of the three runs — adds `graph.py:74`, `council.py:562-573`, `storage.py:119-120`, `runner.py:137-154` to the cite set. |
| critic      | A | Substantive critic verdict in audit-high (gemini-3-flash-preview); enabled the synthesizer's "hallucination of omission" framing. |
| synthesizer | B | gemma4 reproduced the failure-aware framing but its Q3 recommendation overcorrects; this is the run where the synthesizer trades quality for excess strictness. |

**Mix string:** `P:A R:A C:A S:B`

**Verdict:** PROD-READY (r3 of N=3) — with note that Q3 variance across runs is the failure mode to watch

**Commentary:** r3 confirms the mix's reproducibility on Q1+Q2 (identical structure to r1/r2) but exposes Q3 variance: r1+r2 produce the same defensive `str(output)` recommendation while r3 produces a strict short-circuit. Both are actionable; both compile; neither is the gemma4-r2 prompt-augmentation answer (which lands A on its own bench). The variance is small enough that the median Q3 grade across the sweep is **B+**, not A — a real ceiling for this mix. Verdict for the mix as a whole: PROD-READY for plan/research/critique/synthesis quality; for code-diff hardening recommendations specifically, prefer single-shot pure gemma4:31b-cloud (the documented A-only label).
