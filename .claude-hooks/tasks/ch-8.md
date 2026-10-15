---
id: ch-8
title: "Council D — lane robustness: always-on loop guards, strict nudge profile, named terminal actions"
status: active
priority: M
area: consultants.guards
tags: [council, cerebriline]
plan: docs/PLAN-council-cerebriline.md
created: 2026-10-14T13:22:11Z
updated: 2026-10-15T03:56:39Z
sessions: [0a7a5bf4]
---

## Description
Milestone D (D1-D15) scoped for unattended runs per report 08: loop guards always on on every backend (reasoning loop, repetition, non-convergence, cycle/dedup, mistake limit), each trip recording the realized sampler; [guards] profile standard|strict (nudges only, default standard); every terminal action is a forced summary with stop_reason/failure_class or a tombstone, never a wait on a human.

## Acceptance
- [ ] thresholds fitted by replaying transcript.db
- [ ] no guard path pauses or asks
- [ ] strict profile configurable from menu and CLI, per project and per run

## Log
- 2026-10-15 03:56Z [0a7a5bf4] 3c21e29 D7 for tool_executor and coder (per failover attempt: class / no_artifacts / empty_message as task_error); single-call roles not wired by design. d9976a0 rules bullet for D9 infra-wait. Suite 7522 passed / 33 skipped. Only D9 retry-lanes + graceful stop remain, waiting on the owner. Bench 15/20 held for opencoti's CPU timing run on eleven2go (~1 h from 05:52, they mail DONE).
- 2026-10-15 03:40Z [0a7a5bf4] 06b25dc: mangled tool-name recovery (40 researcher calls in 104 transcripts answered "unknown tool"). 0de00fc D12: tool_use_by_role.py; tool_executor drops survey_project/recall_memory (0/215). Suite 7517. Open D: D9 (retry-lanes, graceful cancel, infra-wait event), D7 for non-researcher roles. Bench paused at 15/20 awaiting xollama AFTER mail.
- 2026-10-15 03:30Z [0a7a5bf4] D3-strict committed 8bf43ed: [guards] profile standard|strict + roles; set-guards CLI, menu item 14, per-run --guards, seed only when non-default. Suite 7513. Bench paused 05:29 at 15/20 (results/2026-10-15/compaction-v2) for xollama v0.40.4-xollama.1 check; first kill hit the wrapper pid (bug-1020), corrected. Resume on xollama AFTER mail. Open D: D9, D12, D7 for other roles.
- 2026-10-15 03:19Z [0a7a5bf4] D11 committed 31d02c7: peer_findings gate (cosine>=0.5 from provider vector_distance=cosine, else >=2 shared stemmed words), query clamp, given once per (sid,lane). Suite 7501. Bench 11/20 at 05:19. Next: D3-strict + [guards] profile.
- 2026-10-15 03:10Z [0a7a5bf4] D8 committed ef0f156: lane_digest — under 28k chars byte-identical; past it index of every report + whole reports while they fit + named rest + read_lane_report pager when tools. Suite 7495. Bench v2 9/20 at 05:09 (ETA ~06:25); xollama wants 22434 for v0.40.4-xollama.1 (build ready ~05:45-06:15) — pause at a trial boundary on their mail.
- 2026-10-15 03:03Z [0a7a5bf4] D15 committed e4e3a18: empty-reply retry adds EMPTY_TURN_REMINDER once (was resent unchanged); overflow terminal covered by C5 + D7. Suite 7491. Done so far: D1 D2 D3-std D4 D5 D6 D7(researcher) D10 D13 D14 D15. Open: D3-strict + [guards] profile, D8, D9, D11, D12; D7 for other roles.
- 2026-10-15 02:58Z [0a7a5bf4] D13 committed 772f7b2: harness_note() tags every harness-written turn once ([SYSTEM MESSAGE], _harness); 5 untagged sites fixed; source-scan guard. Suite 7490. Bench v2 8/20 at 04:58.
- 2026-10-15 02:52Z [0a7a5bf4] D7 committed 26a3fe6 (researcher lanes): stop_reason/failure_class on NodeFinished, RoleTurn, node_exit runtime_events payload; show `lanes`. Failed lanes now write node_exit (wrote none). Suite 7487. Other roles' exits not wired yet.
- 2026-10-15 02:40Z [0a7a5bf4] D6 committed dc1f2c7: cap warning (fork capWarningMargin, once, on last tool result) + tool-less report at the cap (_stop_reason iteration_cap) for every run_loop caller. Suite 7481 passed / 33 skipped. Remaining D: D3-strict + [guards] profile, D7 (stop_reason on NodeFinished/lane rows/show — now 6 stop reasons exist), D8, D9, D11, D12, D13, D15.
- 2026-10-15 02:33Z [0a7a5bf4] D4 committed b263f9e (suite 7468/33 skip). D5 committed 8ea5328: read_file cuts on whole lines, header names lines shown + next start_line (old header claimed 10-40 while 3 lines shown); grep context_lines 0-50 + 48k char cap. Suite 7476 passed. Next: D6 wrap-up warning before the cap.
- 2026-10-15 02:23Z [0a7a5bf4] D4 built (uncommitted, suite running): tool_call_unparsable fault class (never retried; was resent as transport every retry), stream {"error"} lines raise (Ollama reader used to return the partial as a reply), run_loop re-asks <=2 naming the swallowed key, cut-at-cap args answered not run, claude_hooks/tool_args.py coercion (bool("false") made grep case-insensitive). tests/test_tool_call_recovery.py 17 pass; fail-first shown behaviourally. bugs 1016-1018.
- 2026-10-14 19:35Z [0a7a5bf4] 2026-10-14: D2 struggle supervisor built (d6296c2) and D3 standard silent-turn re-asks + non-convergence built (004dc98). 7451 passed. Done so far: D1, D2, D3-standard, D10, D14. Open: D3 strict (with [guards] profile), D4–D9, D11–D13, D15. Not deployed.
- 2026-10-14 19:20Z [0a7a5bf4] pending → active: 2026-10-14: D1 built — reasoning-loop guard (8311777: stream cut in both readers, condense+continue, 3 trips → forced report _stop_reason=reasoning_loop) + repetition nudge (5234289). D10 loop guard + D14 mistake limit built (165adb8). Non-convergence moved to D3. Full suite 7435 passed (consultants env). Not deployed. Next: D2 struggle supervisor, then D3–D6.
- 2026-10-14 13:22Z [0a7a5bf4] created
