# caliber-eval results — published

Versioned snapshots of caliber-eval bench reports. The full workbench
(rsynced project workspaces, run logs, fake-HOME dirs, scratch state)
lives outside the repo at `/srv/dev-disk-by-label-opt/dev/caliber-eval/`;
this directory holds **only the lightweight summary artefacts** that
are useful to ship with the code:

- `<label>.json` — `score.py` output for one bench (skill counts,
  `paths:` frontmatter coverage, body chars, per-skill detail).
- `<label>-summary.md` — narrative comparison summary written after
  scoring (run setup, headline metrics vs baseline, where the
  gap comes from, verdict).
- `REFERENCE-claude-cli.md` + `claude-cli-baseline.json` — the
  reference target every other label is graded against.

## Index

| Label | Date | Model | Wall | Score | Skills (proj/total) | Notes |
|---|---|---|---|---|---|---|
| [claude-cli (REFERENCE)](REFERENCE-claude-cli.md) | 2026-04-29 | claude-cli (default) | 37m 14s | **94/100** | 5/8 | Baseline; every other label is graded against this |
| `gemma-tools-optc` | 2026-04-29 | gemma4-98e:tools | ~30m | n/a | 0/3 | Pre-resilience-port; produced 3 skills with empty `paths:` fm. Smoke evidence only |
| [gemma-native-tools-v3](gemma-native-tools-v3-summary.md) | 2026-04-30 | gemma4-98e:native-tools (Q6_K, 256k, 24 GB) | 14m 32s | **87/100** | 1/4 | Highest-scoring local run on record; densest per-skill `paths:` fm (5 entries on `proxy-ops`). Filed upstream as [caliber-ai-org/ai-setup#205](https://github.com/caliber-ai-org/ai-setup/issues/205) |
| [gemma4-31b-cloud](gemma4-31b-cloud-summary.md) | 2026-05-09 | gemma4:31b-cloud | 28m 41s | 85/100 | 2/5 | First **cloud** Ollama A grade; 23% faster than baseline; `paths:` fm 2/2 |
| [gemini-3-flash-preview-cloud](gemini-3-flash-preview-cloud-summary.md) | 2026-05-09 | gemini-3-flash-preview:cloud | **4m 44s** ⚡ | 77/100 | **4/7** | **Fastest run on record by 3×.** Most project skills + densest `paths:` fm of any non-claude-cli label (4/4, 15 entries). **Hallucinated 4 file paths** in skill bodies (`References point to real files: 6/8`) — same off-topic-on-grounding weakness gemini shows on consultants Q3. EVALUATED-ONLY. First bench against the resilience-aware proxy + thought_signature passthrough fix; 0 4xx flaps |
| [deepseek-v4-flash-cloud](deepseek-v4-flash-cloud-summary.md) | 2026-05-09 | deepseek-v4-flash:cloud | 16m 6s | 90/100 | 1/4 | First model to emit `file:line` refs (25 in `get-advice` skill); densest CLAUDE.md at the time (12,839 chars). But ~5/8 file refs hallucinated → `References point to real files: 0/8` is the entire 10-pt gap to 100. Cleanest cloud weather (0 5xx / 0 4xx / 0 empty); 571k input tokens. PROD-screen if score > skill count |
| [glm-5-1-cloud](glm-5-1-cloud-summary.md) | 2026-05-09 | glm-5.1:cloud | 40m 18s | **96/100** ⭐ | **3/6** | **Highest non-claude-cli score in cohort, only 2 pts behind reference.** 9 `file:line` refs across 2 skills, 7/7 verified real on spot-check (vs deepseek's ~3/8). Densest grounded CLAUDE.md ever — 144 bare refs, 11,191 chars. Slowest run in cohort but cleanest output: 0 hallucinated refs, 0 cloud flaps. **PROD-READY**, recommended cloud backend |

## How these get published

After a bench completes and is scored (per
[`PROTOCOL.md` §5](file:///srv/dev-disk-by-label-opt/dev/caliber-eval/PROTOCOL.md)),
the operator copies `<label>.json` and `<label>-summary.md` from the
workbench's `reports/` dir into this directory and commits them. The
heavy artefacts (`claude-cli-artifacts/` snapshot, `logs/`, per-bench
workspaces) stay outside the repo.

Step encoded in
[`caliber-eval/PROTOCOL.md` §7](file:///srv/dev-disk-by-label-opt/dev/caliber-eval/PROTOCOL.md).

## Pointer back to the workbench

- Full reproduce recipe: [`/srv/dev-disk-by-label-opt/dev/caliber-eval/PROTOCOL.md`](file:///srv/dev-disk-by-label-opt/dev/caliber-eval/PROTOCOL.md)
- In-repo overview: [`docs/caliber-eval.md`](../caliber-eval.md)
- Cloud-resilience port: [`docs/PLAN-caliber-proxy-cloud-resilience.md`](../PLAN-caliber-proxy-cloud-resilience.md)
- Proxy runtime docs: [`docs/caliber-proxy.md`](../caliber-proxy.md)
