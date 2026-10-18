---
id: ch-9
title: "Council E — sandbox launcher, lane worktrees/snapshots, shell tool (off by default)"
status: active
priority: M
area: consultants.sandbox
tags: [council, cerebriline]
plan: docs/PLAN-council-cerebriline.md
created: 2026-10-14T13:22:15Z
updated: 2026-10-18T13:41:20Z
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
- 2026-10-18 13:41Z [0a7a5bf4] Run-5 grade (#1641) fixes deployed at f43a445; the caps are in 596cf2b and the glm-5.3-flash defaults in 2fb31d4. Researcher caps high/xhigh 40/32, max 80/72 (shell: 60/52, 120/112); coder workspace 60. Edge-case matrix rows with no run that reached the code are marked "not tested", and cited rows are claim-audited. The critic checks case labels against the runs. CitationLinter resolves a short path by unique suffix. Open, user's call: gap 2 (known findings carried across councils), gap 5 (the critic re-ranks the deliberate read ledger every run). Run 6 requested (#1642).
- 2026-10-18 12:57Z [0a7a5bf4] 2026-10-18 [0a7a5bf4] Run-4 grade (#1636) fixed and deployed at c2c985e (DEPLOY OK). Researcher caps: high/xhigh 30/24, max 60/54; shell bonus max(10, i//2), so xhigh + shell = 45/39. Coder: 12, or 40 with its workspace. Critic ready is held when no run reached the code. Researcher report_check (once). Synthesizer claim audit (synthesizer:audit): not shown / contradicted items move to Unverified. test_reruns follows cp/mv and catches failures hidden by a pipe. glm-5.3-flash inherits glm-5.2's stall thresholds. Dashboard (dfa8454, 2081cb8) is live as well. Ollama mailed; waiting on run 5.
- 2026-10-18 11:50Z [0a7a5bf4] Run-3 grade (mail #1633) fixed + deployed in a7eabe8. COMMANDS numbers each command C<n> and shows the lines a probe printed. A run that never reached the code is not evidence. Synthesizer answer audited in code: a ranked defect cites a run; every run is cited or listed under Dropped; enforce() moves or lists what is still owed. The critic gets the runs. Sandbox changes flags held tests that never ran green. Also deployed: the read-only council dashboard on the engine, LAN via [service] dashboard_host (ae59085, 991e4b2). Waiting on ollama for run 4.
- 2026-10-18 10:47Z [0a7a5bf4] 2026-10-18: LangGraph upgraded to 1.2.16 (f81b760); checkpoint 4.3.0, sqlite 3.1.2, langchain-core 1.6.12. Floors raised in consultants/pyproject.toml. Suite 7,845 passed; deployed. The engine (pid 3754995, :38095) runs 1.2.16 and /v1/health is ok. Rollback freeze: /srv/dev-disk-by-label-opt/dev/lg-pre-upgrade-freeze.txt.
- 2026-10-18 10:25Z [0a7a5bf4] 2026-10-18: run-2 fixes deployed at 1a06863 (DEPLOY OK). - rewind re-enters at the REWIND_NODE (plan + reports + brief, one lane per model), not START; every Send carries additional_context; - evidence bar in code (critic hold, COMMANDS THE LANES RAN, mandatory Unverified section); - jail toolchains: node v26 mirrored to /var/cache/claude-hooks-sandbox/toolchains, bun bound in place, vitest --configLoader runner recipe; - progress follows the tasks stream; ack reply says released / checkpoint_open=false. Run-3 go-ahead mailed (#1629). LangGraph 1.2.16 trial: suite green (7,845); upgrade awaiting owner OK.
- 2026-10-18 09:38Z [0a7a5bf4] 2026-10-18: E3 write-scope clone views built (clone_views.py, --write-scope, registry multi-map, bench_clone_views.py; backup_models scripts/ warm 0.6-1.3 s / 3-4 MiB per lane, cold first lane 14.8 s). Uncommitted, suite running. Run 2 grading (mail #1625/#1626) found: synthesis-phase rewind re-plans and drops the brief (0/226 prompts); critic said ready with no tests run; sandbox node v12 / bun missing from PATH / vite EROFS in ro node_modules; progress reports research done while lanes still run; ack reply says checkpoint_open=true.
- 2026-10-18 09:19Z [0a7a5bf4] E2 launcher built + deployed on solidpc (687e4e1): native/council-sandbox (Rust, no crates, musl, non-PIE static, x86_64+aarch64 via rust-lld, reproducible) replaces prlimit→setpriv→bwrap behind the scope; pinned by sha256 in claude_hooks/sandbox/launcher_pin.json; step_sandbox (before services) + check_sandbox (escape smoke); auto prefers it. Escape suite runs on both backends; 6 per-layer mutants caught. Release asset council-sandbox-v0.1.0 not yet published (docs/RELEASING.md step). Same commit series: 7 fixes from live council csl-2026-10-18-1033-5a67 (checkpoint inject routing, minimax markup lanes, coder worktree view + run_command, critic evidence bar, synthesizer verified/unverified, shell lanes +iters, state previews / SSE role+replay). Remaining E: reflink/overlay views + backup_models bench, Windows/macOS (E7), hardening (E8), net=proxy (E6).
- 2026-10-16 05:41Z [0a7a5bf4] Git-worktree write views built (uncommitted until the full suite passes): --shell write, base snapshot ref, view per lane, freeze-to-patch at council end, crash sweep (startup + hourly), changes/apply/discard/sandbox ls|gc, Sandbox changes section, skill ask-before-apply. kill -9 mid-command leaves nothing (live test). Fixed: session store leaked into views/read mode -> masked. Left: reflink/overlay views + backup_models bench, static launcher + step_sandbox, Windows/macOS, hardening.
- 2026-10-16 05:17Z [0a7a5bf4] E P1 done (43672c4): bwrap backend + seccomp + run_command (read mode) + --shell enablement/follow-up banking/restart note; escape suite 12/12 on solidpc, mutants caught. Left: launcher (static musl) + step_sandbox/check_sandbox, write views (worktree/reflink/overlay) + lifecycle + changes/apply/discard, Windows/macOS (E7), hardening (E8).
- 2026-10-16 04:48Z [0a7a5bf4] pending → active
- 2026-10-14 14:31Z [0a7a5bf4] E1 done (240448d; probe result in 95638ee: EOVERFLOW comes from a host-fs lower layer inside a user namespace, not xino). Left: E2+.
- 2026-10-14 13:22Z [0a7a5bf4] created
