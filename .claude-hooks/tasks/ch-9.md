---
id: ch-9
title: "Council E — sandbox launcher, lane worktrees/snapshots, shell tool (off by default)"
status: active
priority: M
area: consultants.sandbox
tags: [council, cerebriline]
plan: docs/PLAN-council-cerebriline.md
created: 2026-10-14T13:22:15Z
updated: 2026-10-18T09:38:43Z
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
- 2026-10-18 09:38Z [0a7a5bf4] 2026-10-18: E3 write-scope clone views built (clone_views.py, --write-scope, registry multi-map, bench_clone_views.py; backup_models scripts/ warm 0.6-1.3 s / 3-4 MiB per lane, cold first lane 14.8 s). Uncommitted, suite running. Run 2 grading (mail #1625/#1626) found: synthesis-phase rewind re-plans and drops the brief (0/226 prompts); critic said ready with no tests run; sandbox node v12 / bun missing from PATH / vite EROFS in ro node_modules; progress reports research done while lanes still run; ack reply says checkpoint_open=true.
- 2026-10-18 09:19Z [0a7a5bf4] E2 launcher built + deployed on solidpc (687e4e1): native/council-sandbox (Rust, no crates, musl, non-PIE static, x86_64+aarch64 via rust-lld, reproducible) replaces prlimit→setpriv→bwrap behind the scope; pinned by sha256 in claude_hooks/sandbox/launcher_pin.json; step_sandbox (before services) + check_sandbox (escape smoke); auto prefers it. Escape suite runs on both backends; 6 per-layer mutants caught. Release asset council-sandbox-v0.1.0 not yet published (docs/RELEASING.md step). Same commit series: 7 fixes from live council csl-2026-10-18-1033-5a67 (checkpoint inject routing, minimax markup lanes, coder worktree view + run_command, critic evidence bar, synthesizer verified/unverified, shell lanes +iters, state previews / SSE role+replay). Remaining E: reflink/overlay views + backup_models bench, Windows/macOS (E7), hardening (E8), net=proxy (E6).
- 2026-10-16 05:41Z [0a7a5bf4] Git-worktree write views built (uncommitted until the full suite passes): --shell write, base snapshot ref, view per lane, freeze-to-patch at council end, crash sweep (startup + hourly), changes/apply/discard/sandbox ls|gc, Sandbox changes section, skill ask-before-apply. kill -9 mid-command leaves nothing (live test). Fixed: session store leaked into views/read mode -> masked. Left: reflink/overlay views + backup_models bench, static launcher + step_sandbox, Windows/macOS, hardening.
- 2026-10-16 05:17Z [0a7a5bf4] E P1 done (43672c4): bwrap backend + seccomp + run_command (read mode) + --shell enablement/follow-up banking/restart note; escape suite 12/12 on solidpc, mutants caught. Left: launcher (static musl) + step_sandbox/check_sandbox, write views (worktree/reflink/overlay) + lifecycle + changes/apply/discard, Windows/macOS (E7), hardening (E8).
- 2026-10-16 04:48Z [0a7a5bf4] pending → active
- 2026-10-14 14:31Z [0a7a5bf4] E1 done (240448d; probe result in 95638ee: EOVERFLOW comes from a host-fs lower layer inside a user namespace, not xino). Left: E2+.
- 2026-10-14 13:22Z [0a7a5bf4] created
