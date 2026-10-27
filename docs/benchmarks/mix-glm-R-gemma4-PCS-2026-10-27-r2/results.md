# Benchmark — `mix-glm-R-gemma4-PCS-2026-10-27-r2`

Generated 2026-10-27T15:33:08+01:00 on solidpc.
Engine HEAD (`/srv/dev-disk-by-label-opt/dev/claude-hooks`): `2dbbe40`
Subject baseline: `bench-baseline-2026-05-07` (commit `83cfd3b`) — frozen worktree at `/srv/dev-disk-by-label-opt/dev/bench-home-2026-10-27/tmp/claude-hooks-bench-bench-baseline-2026-05-07-BlRv`
Answer key removed from the worktree: `docs/benchmarks docs/consultants-benchmarks.md`
Cloud model snapshot: see `models.json`.

Model pin: per-role config (no override). Snapshot of `claude-consultants config show --user`:

```json
{
  "ok": true,
  "topology": "council",
  "effort": "xhigh",
  "effort_budget": 5,
  "max_followups": 4,
  "allow_extra": 1,
  "verify_budget": "bounded",
  "adversary_strictness": "strict",
  "adversary_checkpoint": false,
  "adversary_checkpoint_timeout_s": 1800,
  "service": {
    "mode": "always-on",
    "http_port": 38195
  },
  "protocol": {
    "typed": false
  },
  "guards": {
    "profile": "standard",
    "roles": {}
  },
  "decision": {
    "enabled": false,
    "url": null,
    "model": "jev-latest",
    "key_file": null,
    "floor": 0.6,
    "high_stakes_floor": 0.85,
    "timeout_s": 15.0,
    "seams": [
      "review",
      "confidence",
      "struggle",
      "citations"
    ]
  },
  "budget": {},
  "sampling": {},
  "compaction": {},
  "endpoints": {},
  "cloud": {
    "route": "direct",
    "url": "https://ollama.com",
    "key_file": null
  },
  "smart_start": {
    "enabled": false,
    "idle_timeout_seconds": 1800,
    "forwarder_url": "http://127.0.0.1:38096"
  },
  "endpoint": "http://127.0.0.1:38195",
  "roles": {
    "planner": {
      "enabled": true,
      "model": "gemma4:31b-cloud",
      "ctx_max": null,
      "ctx_max_explicit": false,
      "extra_models": [],
      "think": "medium",
      "budget": {},
      "sampling": {},
      "compaction": {},
      "models": {}
    },
    "researcher": {
      "enabled": true,
      "model": "glm-5.3-flash:cloud",
      "ctx_max": null,
      "ctx_max_explicit": false,
      "extra_models": [],
      "think": "high",
      "budget": {},
      "sampling": {},
      "compaction": {},
      "models": {}
    },
    "tool_executor": {
      "enabled": false,
      "model": "gemma4:31b-cloud",
      "ctx_max": null,
      "ctx_max_explicit": false,
      "extra_models": [],
      "think": false,
      "budget": {},
      "sampling": {},
      "compaction": {},
      "models": {}
    },
    "critic": {
      "enabled": true,
      "model": "gemma4:31b-cloud",
      "ctx_max": null,
      "ctx_max_explicit": false,
      "extra_models": [],
      "think": "medium",
      "budget": {},
      "sampling": {},
      "compaction": {},
      "models": {}
    },
    "coder": {
      "enabled": false,
      "model": "glm-5.2:cloud",
      "ctx_max": null,
      "ctx_max_explicit": false,
      "extra_models": [],
      "think": "high",
      "budget": {},
      "sampling": {},
      "compaction": {},
      "models": {},
      "default_route": {
        "primary": "glm-5.2:cloud",
        "fallback": "kimi-k2.6:cloud"
      },
      "routes_by_language": {
        "c": {
          "primary": "kimi-k2.6:cloud",
          "fallback": "deepseek-v4-pro:cloud"
        },
        "cpp": {
          "primary": "deepseek-v4-pro:cloud",
          "fallback": "deepseek-v4-flash:cloud"
        },
        "csharp": {
          "primary": "kimi-k2.6:cloud",
          "fallback": "minimax-m3:cloud"
        },
        "go": {
          "primary": "kimi-k2.6:cloud",
          "fallback": "deepseek-v4-pro:cloud"
        },
        "python": {
          "primary": "kimi-k2.6:cloud",
          "fallback": "deepseek-v4-flash:cloud"
        },
        "rust": {
          "primary": "kimi-k2.6:cloud",
          "fallback": "deepseek-v4-pro:cloud"
        }
      }
    },
    "synthesizer": {
      "enabled": true,
      "model": "gemma4:31b-cloud",
      "ctx_max": null,
      "ctx_max_explicit": false,
      "extra_models": [],
      "think": "high",
      "budget": {},
      "sampling": {},
      "compaction": {},
      "models": {}
    },
    "adversary": {
      "enabled": false,
      "model": "kimi-k2.6:cloud",
      "ctx_max": null,
      "ctx_max_explicit": false,
      "extra_models": [],
      "think": true,
      "budget": {},
      "sampling": {},
      "compaction": {},
      "models": {}
    }
  },
  "tools": {
    "enabled": true,
    "git": false,
    "all_roles": true,
    "default_level": "auto",
    "permissions": {},
    "shell": {
      "default": "off",
      "mode": "read",
      "net": "none",
      "backend": "auto",
      "ro_binds": [],
      "timeout_s": 120,
      "max_timeout_s": 600,
      "memory_mib": 4096,
      "pids": 256,
      "cpu_pct": 400,
      "lane_disk_gib": 2.0,
      "free_floor_gib": 20.0,
      "free_floor_pct": 10.0,
      "held_ttl_days": 7.0
    }
  },
  "store": {
    "enabled": false,
    "backend": "pgvector",
    "enable_at_efforts": [
      "high",
      "max",
      "xmedium",
      "xhigh",
      "xmax",
      "xauto"
    ],
    "recall_limit": 5,
    "pgvector_dsn": "postgresql://claude:***@127.0.0.1:5432/memory",
    "pgvector_table": "consultants_store",
    "sqlite_vec_path": "~/.claude/consultants-store.db",
    "embedder": "llamafile",
    "ttl": {
      "enabled": true,
      "research_days": 30.0,
      "tool_results_hours": 24.0,
      "project_days": null,
      "user_days": null,
      "refresh_on_read": true,
      "jitter_pct": 0.1
    },
    "distillation": {
      "enabled": true,
      "model": "gemma4:31b-cloud",
      "fallback_models": [
        "glm-5.2:cloud"
      ],
      "sweep_interval_seconds": 3600.0,
      "min_entries_per_distillation": 3,
      "max_session_entries": 50,
      "max_groups_per_sweep": 5,
      "pace_seconds_between_distillations": 5.0
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
    "xauto",
    "xhigh",
    "xmax",
    "xmedium"
  ],
  "valid_service_modes": [
    "always-on",
    "smart-start"
  ],
  "valid_store_backends": [
    "memory",
    "pgvector",
    "sqlite_vec"
  ],
  "valid_verify_budgets": [
    "minimal",
    "bounded",
    "generous"
  ],
  "valid_adversary_strictness": [
    "soft",
    "normal",
    "strict"
  ],
  "active_config": {
    "scope": "user",
    "override_user_global": true,
    "project_config_path": "/srv/dev-disk-by-label-opt/dev/claude-hooks/.claude-hooks/consultants.toml",
    "project_file_exists": true,
    "user_config_path": "/srv/dev-disk-by-label-opt/dev/bench-home-2026-10-27/.claude/consultants-config.toml"
  }
}
```

## Summary

| Query | Effort | Status | Wall | Prompt tok | Completion tok | LLM calls | Tool calls | sid |
|---|---|---|---|---|---|---|---|---|
| smoke | medium | completed | 243s | 57624 | 1201 | 9 | 7 | `csl-2026-10-27-1500-2eb1` |
| audit-medium | medium | completed | 637s | 85621 | 3924 | 17 | 27 | `csl-2026-10-27-1504-5b80` |
| audit-high | high | failed | 1107s | 70013 | 5569 | 16 | 25 | `csl-2026-10-27-1514-18af` |

## Per-role breakdown

Wall sums every entry of the role node (researcher in
fan-out fires once per lane; counts accumulate). Token
totals include cloud-model reasoning tokens.

### smoke

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 3.6m | 3 | 19753 | 979 | 2 |
| researcher | 23.6s | 5 | 89885 | 580 | 5 |
| synthesizer | 0.9s | 1 | 2521 | 83 | 0 |

### audit-medium

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 1.8m | 5 | 20939 | 1460 | 3 |
| researcher | 15.0m | 11 | 331088 | 4654 | 24 |
| synthesizer | 1.1m | 1 | 4292 | 1067 | 0 |

### audit-high

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 1.1m | 4 | 9892 | 1715 | 3 |
| researcher | 13.5m | 8 | 240875 | 5208 | 18 |
| critic | 7.8m | 3 | 15479 | 1764 | 4 |
| synthesizer | 3.5s | 1 | 4604 | 696 | 0 |

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
| smoke        | _PASS / WEAK / FAIL_   | _one-line note_ |
| audit-medium | _A / B / C / F_        | _one-line note_ |
| audit-high   | _A / B / C / F_        | _one-line note_ |

## Per-role grades (manual, per [`EVALUATION.md`](../EVALUATION.md) §3.5)

Read each role's output in the per-query `transcript.md`
files and assign one grade per role aggregated across all
three queries. Critic grade is `n/a` unless audit-high ran.

| Role | Grade | One-sentence justification |
|---|---|---|
| planner     | _A / B / C / F_      | _why_ |
| researcher  | _A / B / C / F_      | _why_ |
| critic      | _A / B / C / F / n/a_| _why_ |
| synthesizer | _A / B / C / F_      | _why_ |

**Mix string:** `P:_ R:_ C:_ S:_`

**Verdict:** _PROD-READY / EVALUATED-ONLY / UNSTABLE_

**Commentary:** _one paragraph — what role(s) this model wins at vs prior labels, which role(s) it should NOT be used for, whether you'd build a heterogeneous mix around it_
