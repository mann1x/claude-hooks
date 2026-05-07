# Benchmark — `kimi-k2.6-cloud-2026-05-07`

Generated 2026-05-07T09:23:48+02:00 on solidpc.
Engine HEAD (`/srv/dev-disk-by-label-opt/dev/claude-hooks`): `7f5e08e`
Subject baseline: `bench-baseline-2026-05-07` (commit `f309436`) — frozen worktree at `/tmp/claude-hooks-bench-bench-baseline-2026-05-07-ce1r`
Cloud model snapshot: see `models.json`.

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
| smoke | medium | completed | 394s | 24040 | 11260 | 14 | 22 | `csl-2026-05-07-0900-18fc` |
| audit-medium | medium | completed | 213s | 61745 | 12257 | 17 | 27 | `csl-2026-05-07-0907-5644` |
| audit-high | high | completed | 787s | 112656 | 34320 | 23 | 37 | `csl-2026-05-07-0910-c184` |

## Per-role breakdown

Wall sums every entry of the role node (researcher in
fan-out fires once per lane; counts accumulate). Token
totals include cloud-model reasoning tokens.

### smoke

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 1.8m | 1 | 237 | 2315 | 0 |
| researcher | 5.2m | 12 | 44265 | 7930 | 22 |
| synthesizer | 1.8m | 1 | 1076 | 3212 | 0 |

### audit-medium

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 25.7s | 1 | 270 | 1145 | 0 |
| researcher | 3.1m | 15 | 92452 | 9171 | 27 |
| synthesizer | 1.3m | 1 | 1511 | 3655 | 0 |

### audit-high

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 1.8m | 1 | 400 | 5509 | 0 |
| researcher | 18.6m | 20 | 436536 | 60904 | 37 |
| critic | 1.6m | 1 | 4810 | 4903 | 0 |
| synthesizer | 1.2m | 1 | 5103 | 5282 | 0 |

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

## Per-query grades (manual, per [`EVALUATION.md`](../EVALUATION.md) §3)

| Query | Grade | Notes |
|---|---|---|
| smoke        | (pending re-run with code-only smoke) | original liveness-check smoke broke under worktree pinning — re-running with the 2026-05-07 baseline's code-only variant |
| audit-medium | A    | All 6 ground-truth unprotected sites cited with correct verdicts; correctly omitted install.py protected/inline-script sites; no false positives |
| audit-high   | A    | All 4 required claims present and cited with `path:line`; recommendation cites the new tombstone code (council.py:562-566) and notes the remaining `status=failed` mismatch as a degraded-answer footgun |

## Per-role grades (manual, per [`EVALUATION.md`](../EVALUATION.md) §3.5)

Aggregated across audit-medium + audit-high transcripts (smoke
re-run pending; per-role grades stable enough to lock in now and
re-confirm after the smoke re-run).

| Role | Grade | One-sentence justification |
|---|---|---|
| planner     | A | 6-7 numbered items per query, each pointing at a concrete file/path or verification step ("Show the diff of commit `4e67dc2`", "Run `git grep -n -E 'import psycopg\\|from psycopg'`") — no vague items |
| researcher  | A | Path:line citations on every finding; tool calls batched (audit-medium iter 2 had 5 parallel tools); audit-high lane was sharp enough to read its own hardening fix at council.py:562-566 |
| critic      | A | Audit-high critic produced parseable `DECISION:` line in concise reasoning; one re-route used productively |
| synthesizer | A | Audit-medium nailed all 6 ground-truth sites; audit-high produced a working trace with explicit hardening recommendation including a code-shaped diff; bottom-line-first format throughout |

**Mix string:** `P:A R:A C:A S:A`

**Mix string:** `P:_ R:_ C:_ S:_`

**Verdict:** _PROD-READY / EVALUATED-ONLY / UNSTABLE_

**Commentary:** _one paragraph — what role(s) this model wins at vs prior labels, which role(s) it should NOT be used for, whether you'd build a heterogeneous mix around it_
