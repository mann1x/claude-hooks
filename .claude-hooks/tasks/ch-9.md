---
id: ch-9
title: "Council E — sandbox launcher, lane worktrees/snapshots, shell tool (off by default)"
status: active
priority: M
area: consultants.sandbox
tags: [council, cerebriline]
plan: docs/PLAN-council-cerebriline.md
created: 2026-10-14T13:22:15Z
updated: 2026-10-16T04:48:31Z
sessions: [0a7a5bf4]
---

## Description
Milestone E: E1 prerequisite fixes (runtime_permissions, control.py enum, per-lane cache invalidation, lane-bound executor, GC); static council-sandbox launcher as a deploy artifact (Landlock optional, try without the boot parameter); git worktrees / reflink / overlay views with a lifecycle and no stale views; shell off by default, enabled on request, by knob, or in a follow-up; Windows/macOS backends.

## Acceptance
- [ ] escape suite green on Linux
- [ ] kill -9 at every lifecycle state leaves nothing
- [ ] backup_models view in <2 s with no data-proportional disk use
- [ ] follow-up adds shell to a running consultancy
- [ ] Windows smoke passes

## Log
- 2026-10-16 04:48Z [0a7a5bf4] pending → active
- 2026-10-14 14:31Z [0a7a5bf4] E1 done (240448d; probe result in 95638ee: EOVERFLOW comes from a host-fs lower layer inside a user namespace, not xino). Left: E2+.
- 2026-10-14 13:22Z [0a7a5bf4] created
