---
id: ch-6
title: "Council B — output cap, context-window knob, thinking per backend, sampling diversity"
status: pending
priority: H
area: consultants.budgets
tags: [council, cerebriline]
plan: docs/PLAN-council-cerebriline.md
created: 2026-10-14T13:22:05Z
updated: 2026-10-14T13:22:05Z
sessions: [0a7a5bf4]
---

## Description
Milestone B: automatic output cap min(0.75×ctx, 96k, model max, user max) with per-backend wire names; num_ctx knob (model default; assured/dynamic under PolyKV); thinking level fixed per (role, model), mapped per backend; random seed + temperature margin (2%, max 10%) on every role.

## Acceptance
- [ ] budget bench shows caps, granted windows and think budgets per call
- [ ] deadlines re-validated from transcript.db
- [ ] parity cohort green

## Log
- 2026-10-14 13:22Z [0a7a5bf4] created
