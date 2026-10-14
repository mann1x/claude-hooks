---
id: ch-7
title: "Council C — compaction: reduced replay default, full 5-step per role, condensation, overflow"
status: pending
priority: H
area: consultants.compaction
tags: [council, cerebriline]
plan: docs/PLAN-council-cerebriline.md
created: 2026-10-14T13:22:08Z
updated: 2026-10-14T13:22:08Z
sessions: [0a7a5bf4]
---

## Description
Milestone C: same model as the agent, one serial connection (parallel only under PolyKV); reduced replay flow by default, full 5-step opt-in per role; 25% target, 0.9 trigger, no-tail rescue, verbatim user requests; thinking condensation; overflow compaction (tool-result truncation, stale reads, provider overflow, starved cap); duplicate-stub fix; A/B user vs system message role.

## Acceptance
- [ ] small local model (<=8B, 32k) bench through >=3 compactions in each flow vs the fork's 85/63/50/25% baseline
- [ ] same-model and single-connection tests pass
- [ ] parity with compaction off

## Log
- 2026-10-14 13:22Z [0a7a5bf4] created
