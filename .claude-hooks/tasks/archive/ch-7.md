---
id: ch-7
title: "Council C — compaction: reduced replay default, full 5-step per role, condensation, overflow"
status: done
priority: H
area: consultants.compaction
tags: [council, cerebriline]
plan: docs/PLAN-council-cerebriline.md
created: 2026-10-14T13:22:08Z
updated: 2026-10-15T04:59:19Z
sessions: [0a7a5bf4]
---

## Description
Milestone C: same model as the agent, one serial connection (parallel only under PolyKV); reduced replay flow by default, full 5-step opt-in per role; 25% target, 0.9 trigger, no-tail rescue, verbatim user requests; thinking condensation; overflow compaction (tool-result truncation, stale reads, provider overflow, starved cap); duplicate-stub fix; A/B user vs system message role.

## Acceptance
- [ ] small local model (<=8B, 32k) bench through >=3 compactions in each flow vs the fork's 85/63/50/25% baseline
- [ ] same-model and single-connection tests pass
- [ ] parity with compaction off

## Log
- 2026-10-15 04:59Z [0a7a5bf4] pending → done: Exit C met, fb8772e. Bench v2 20/20 on qwen3.6 27B @ eleven2go, all arms ≥3 compactions: reduced/system 3/4, full 2/4 each, reduced/user 1/4, basic 0/4 (fork 25% at 3+). Defaults unchanged (reduced + system); n=4 is not significant (p=0.49), so a placement verdict needs ≥20 trials an arm. Run split across two code versions / xOllama builds (paused 05:29-06:25), recorded in the report's Provenance. Open gap: an oversized last turn can't be folded.
- 2026-10-14 13:22Z [0a7a5bf4] created
