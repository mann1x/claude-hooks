# Benchmark — `kimi-k2.6-cloud-2026-05-07`

Generated 2026-05-07T08:34:34+02:00 on solidpc.
Repo HEAD: `639596e`

Model pin: per-role config (no override). Snapshot of `claude-consultants config show`:

```json
{
  "ok": true,
  "topology": "council",
  "effort": "medium",
  "effort_budget": 3,
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
      "model": "kimi-k2.6:cloud",
      "ctx_max": null,
      "ctx_max_explicit": false
    },
    "researcher": {
      "enabled": true,
      "model": "kimi-k2.6:cloud",
      "ctx_max": null,
      "ctx_max_explicit": false
    },
    "critic": {
      "enabled": true,
      "model": "kimi-k2.6:cloud",
      "ctx_max": null,
      "ctx_max_explicit": false
    },
    "synthesizer": {
      "enabled": true,
      "model": "kimi-k2.6:cloud",
      "ctx_max": null,
      "ctx_max_explicit": false
    }
  },
  "mandatory_roles": [
    "synthesizer"
  ],
  "valid_efforts": [
    "high",
    "low",
    "max",
    "medium"
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
| smoke | medium | completed | 212s | 49304 | 10917 | 15 | 23 | `csl-2026-05-07-0807-e7c2` |
| audit-medium | medium | completed | 167s | 136178 | 12590 | 17 | 38 | `csl-2026-05-07-0810-5c85` |
| audit-high | high | completed | 1030s | 154071 | 38942 | 27 | 46 | `csl-2026-05-07-0813-faa2` |

## Per-role breakdown

Wall sums every entry of the role node (researcher in
fan-out fires once per lane; counts accumulate). Token
totals include cloud-model reasoning tokens.

### smoke

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 42.1s | 1 | 237 | 2700 | 0 |
| researcher | 3.4m | 13 | 100522 | 8490 | 23 |
| synthesizer | 44.5s | 1 | 1021 | 2907 | 0 |

### audit-medium

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 18.6s | 1 | 270 | 1267 | 0 |
| researcher | 4.1m | 15 | 228846 | 11204 | 38 |
| synthesizer | 42.4s | 1 | 1529 | 2849 | 0 |

### audit-high

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 51.4s | 1 | 400 | 3113 | 0 |
| researcher | 21.3m | 24 | 569241 | 57829 | 46 |
| critic | 1.1m | 1 | 3675 | 4611 | 0 |
| synthesizer | 59.8s | 1 | 3932 | 4442 | 0 |

## Files

Per-query artifacts in this directory:

- **smoke**
  - `smoke.summary.md` — synthesizer answer
  - `smoke.transcript.md` — full role transcript
  - `smoke.waterfall.txt` — per-role wall waterfall
  - `smoke.trace.jsonl` — raw JSONL trace
  - `smoke.metadata.json` — token totals + retries
- **audit-medium**
  - `audit-medium.summary.md` — synthesizer answer
  - `audit-medium.transcript.md` — full role transcript
  - `audit-medium.waterfall.txt` — per-role wall waterfall
  - `audit-medium.trace.jsonl` — raw JSONL trace
  - `audit-medium.metadata.json` — token totals + retries
- **audit-high**
  - `audit-high.summary.md` — synthesizer answer
  - `audit-high.transcript.md` — full role transcript
  - `audit-high.waterfall.txt` — per-role wall waterfall
  - `audit-high.trace.jsonl` — raw JSONL trace
  - `audit-high.metadata.json` — token totals + retries

## Grades (manual, per [`EVALUATION.md`](../EVALUATION.md) §3)

Single-run grades — not yet aggregated to N=3. Treat as
provisional. Re-grade after the 3-run aggregate exists.

| Query | Grade | Notes |
|---|---|---|
| smoke        | PASS | "live as a local-only systemd user service on 127.0.0.1:38095" with two `path:line` cites; concise, decisive |
| audit-medium | A    | All 6 ground-truth unprotected sites cited with correct verdicts; correctly omitted install.py protected/inline-script sites; no false positives |
| audit-high   | A    | All 4 required claims present with `path:line`; recommendation is a working code-shaped diff, not prose |

**Verdict:** PROD-READY (single-run; pending N=3 confirmation per §5)

**Commentary:** Q3 produced an answer that correctly diagnosed the synthesizer-doesn't-see-`error` gap and proposed a working hardening patch — the bar this query was designed to test. The audit-medium being FASTER than the smoke is counter-intuitive but consistent with kimi-k2.6's reasoning-token bias on under-constrained inputs (the planner over-elaborates on a one-line "is the pipeline live?" prompt). Cloud variance not yet measured; may not reproduce. Would ship at every role pending the 3-run check.
