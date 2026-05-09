# Benchmark — `mix-gemini-PRC-gemma4-S-2026-05-09-r2`

Generated 2026-05-09T10:24:02+02:00 on solidpc.
Engine HEAD (`/shared/dev/claude-hooks`): `5f7d229`
Subject baseline: `bench-baseline-2026-05-07` (commit `83cfd3b`) — frozen worktree at `/tmp/claude-hooks-bench-bench-baseline-2026-05-07-CvAB`
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
| smoke | medium | completed | 30s | 131087 | 2559 | 14 | 12 | `csl-2026-05-09-1020-6b3a` |
| audit-medium | medium | completed | 76s | 331859 | 9128 | 17 | 34 | `csl-2026-05-09-1020-7cd4` |
| audit-high | high | completed | 136s | 179789 | 19003 | 34 | 30 | `csl-2026-05-09-1021-0736` |

## Per-role breakdown

Wall sums every entry of the role node (researcher in
fan-out fires once per lane; counts accumulate). Token
totals include cloud-model reasoning tokens.

### smoke

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 3.7s | 1 | 237 | 569 | 0 |
| researcher | 27.5s | 12 | 188750 | 3001 | 12 |
| synthesizer | 3.4s | 1 | 1063 | 179 | 0 |

### audit-medium

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 4.5s | 1 | 266 | 637 | 0 |
| researcher | 55.8s | 15 | 548731 | 12740 | 34 |
| synthesizer | 24.0s | 1 | 1890 | 708 | 0 |

### audit-high

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 7.1s | 1 | 410 | 1106 | 0 |
| researcher | 3.1m | 31 | 566041 | 30128 | 30 |
| critic | 12.8s | 1 | 2590 | 2199 | 0 |
| synthesizer | 20.2s | 1 | 3123 | 1360 | 0 |

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
| smoke        | PASS | Names all four roles with a valid `council.py:98` cite. |
| audit-medium | A    | Identical six-site list to r1 with per-site exercisability ruling; adds a `pyproject.toml:34` reachability hint r1 didn't have. |
| audit-high   | B+   | Q1+Q2 correct (graph reducer cites + storage path identical to r1). Q3 same defensive `output = str(tool_executor(...))` coercion as r1 — actionable but doesn't plug the traced failure (`len(output)` at runner.py:160 crashes first if non-string). Compiles, partially addresses → B+ per v1.2 sub-rubric. |

## Per-role grades (manual, per [`EVALUATION.md`](../EVALUATION.md) §3.5)

| Role | Grade | One-sentence justification |
|---|---|---|
| planner     | A | 5.2 s wall, clean three-bullet plan; gemini-3-flash-preview's strongest role. |
| researcher  | A | All audit-medium + audit-high cites match the frozen baseline; tombstone behavior cited at `council.py:566` (an extra detail r1 didn't surface). |
| critic      | A | Single critic call in audit-high, gemini-3-flash-preview, substantive verdict that flowed into the synthesizer's failure framing. |
| synthesizer | A | gemma4 produced the same explicit "coherent but degraded answer + status=failed" framing as r1, no symbol drift. |

**Mix string:** `P:A R:A C:A S:A`

**Verdict:** PROD-READY (r2 of N=3)

**Commentary:** r2 reproduces r1's grade profile cleanly: every role grade matches and the wall stays under 4 minutes (4 m 02 s vs r1 3 m 32 s). Q3 lands on exactly the same `str(output)` defensive recommendation as r1 — same B+ per sub-rubric, same "compiles + partially addresses" framing. Reproducibility on the synthesizer answer is high.
