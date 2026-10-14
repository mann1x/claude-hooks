---
id: ch-8
title: "Council D — lane robustness: always-on loop guards, strict nudge profile, named terminal actions"
status: pending
priority: M
area: consultants.guards
tags: [council, cerebriline]
plan: docs/PLAN-council-cerebriline.md
created: 2026-10-14T13:22:11Z
updated: 2026-10-14T13:22:11Z
sessions: [0a7a5bf4]
---

## Description
Milestone D (D1-D15) scoped for unattended runs per report 08: loop guards always on on every backend (reasoning loop, repetition, non-convergence, cycle/dedup, mistake limit), each trip recording the realized sampler; [guards] profile standard|strict (nudges only, default standard); every terminal action is a forced summary with stop_reason/failure_class or a tombstone, never a wait on a human.

## Acceptance
- [ ] thresholds fitted by replaying transcript.db
- [ ] no guard path pauses or asks
- [ ] strict profile configurable from menu and CLI, per project and per run

## Log
- 2026-10-14 13:22Z [0a7a5bf4] created
