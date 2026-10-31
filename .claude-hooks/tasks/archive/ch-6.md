---
id: ch-6
title: "Council B — output cap, context-window knob, thinking per backend, sampling diversity"
status: done
priority: H
area: consultants.budgets
tags: [council, cerebriline]
plan: docs/PLAN-council-cerebriline.md
created: 2026-10-14T13:22:05Z
updated: 2026-10-14T16:36:58Z
sessions: [0a7a5bf4]
---

## Description
Milestone B: automatic output cap min(0.75×ctx, 96k, model max, user max) with per-backend wire names; num_ctx knob (model default; assured/dynamic under PolyKV); thinking level fixed per (role, model), mapped per backend; random seed + temperature margin (2%, max 10%) on every role.

## Acceptance
- [ ] budget bench shows caps, granted windows and think budgets per call
- [ ] deadlines re-validated from transcript.db
- [ ] parity cohort green

## Log
- 2026-10-14 16:36Z [0a7a5bf4] pending → done: Closed c952d8f. Live council on opencoti @bs2 (DCA, 2M session) is the exit evidence. Also fixed: llama.cpp single system turn, error reducers for concurrent lane failures, Send lanes missing runtime_control.
- 2026-10-14 15:51Z [0a7a5bf4] B built on dev: B1/B2 3798d9c (cap min(0.75·window, 96k, model max); window per backend, local never trained ctx; stated # Output Budget; [budget] knobs + set-budget; hard cap scales with num_predict/32768 after measuring decode in 140 transcript.db; PolyKV [endpoints.X] window = dynamic default, assured refused until F), B3 209ec28 (think levels/ints per (role, model), opencoti reasoning_budget_tokens, cloud thinking_values gate, refused level -> true, allowance-based capped detection, set-think; fixed role.think dropped on save), B4 82c7bf5 (per-instance seed unique per model per run, ±2% temperature jitter, set-sampling, show samplers). Open: model-template think layer (no measured values yet), /api/show think-budget probe for local builds, xOllama read-back live test.
- 2026-10-14 13:22Z [0a7a5bf4] created
