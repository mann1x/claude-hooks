# 05 — Agents & orchestration in Cerebriline, mapped to the consultants council

Slice: commit scopes `agents` (108), `escalation` (20),
`teams` (19), `prompts`/`prompt`/`prompt-templates` (16), `skills`/`sdd` (7),
`swarm` (3), `struggle` (3), `complexity` (4), `timings` (3), `events` (2),
`queue` (2), `resume` (1), `team`/`teammates` (2), in
`/shared/dev/cline` (main, 2026-08-12 → 2026-10-10). Scope `atomic` (45
commits) is excluded by owner (early-phase, not working yet). I also read the PR-merge
commits outside the scope prefix that carry the same work: `cea4a1247` (#104
swarm resilience), `60b60ebf6` (#106 liveness), `c8ef7aff4` (#92 worker
supervisor), `d6ffd8b1f`, `3d819eb9a`, `5fc435627`, `0fa66bef2`, `c9c3ffb41`.
Docs read: `STATE_SUMMARY.md`, `PLANS.md` (§7b, §7p, §9f–9k, §12, §13),
`CLAUDE.md`, `sdk/ARCHITECTURE.md`.

There are no commits in this window with the scopes `check`, `loop`, `turn`,
`task`, `lead` or `restore`. Their subjects sit under `agents:` and are
covered below.

Paths in the fork are relative to `/shared/dev/cline/sdk/packages/` unless they
start with `apps/` or `/`. Our paths are relative to the claude-hooks repo root.

---

## 0. The shape of the two systems

| | Cerebriline | consultants council |
|---|---|---|
| Who orchestrates | an LLM **lead** (the chat model) calls `spawn_agent` / `spawn_swarm` / `subagent_<name>` / `team_*` tools | a fixed LangGraph: planner → researcher fan-out → critic(s) → synthesizer (+ coder, tool_executor, refuter) |
| Worker shape | a full agent loop per sub-agent (its own `SessionRuntime`, own engine session, own compaction, CoW overlay sandbox) | a researcher lane = one `run_loop` over read-only tools, one model, one plan-item slice |
| Fan-out sizing | live admission: queue + placement over up to 10 heterogeneous "agent nodes", priority tiers, live occupancy | static `FANOUT_MAX_LANES` (default 2) × `[primary]+extra_models` (`consultants/engine/council.py:879`, `consultants/engine/graph.py:963-1019`) |
| Failure stance | **"agents retry and never fail on infrastructure"** (user ruling, `cea4a1247`) | bounded retries (15 attempts ≈ 15 min, `claude_hooks/get_advice/chat_client.py:39-42`), then the lane is tombstoned (`consultants/engine/council.py:2275`, `:2541`) |
| Escalation | per-session **expert model** handed the task when a struggle detector fires, or once when a terminal guard would end the run | xauto **topology** escalator (more rounds/reroutes/multi-critic) on low confidence, critic dissent, time pressure or a named gap (`consultants/engine/escalation.py:76-158`) |
| Durability | per-session round registry `<session>.rounds.json`, interrupted rounds recorded, restart-safe PolyKV pools | LangGraph checkpoints + `~/.claude/consultants-inflight.json`, suspend/resume across engine restarts (`consultants/server/app.py:1392`, `:1477`) |

The difference that matters most: the fork's worker is an autonomous agent. It
runs for tens to hundreds of turns against a slow, flaky self-hosted engine.
Almost every feature below answers one failure that showed up in a measured
50–75-agent swarm on pandorum. Our lanes are short (4–10 iterations,
`consultants/engine/council.py:201-229`) and go to Ollama Cloud. So the
resilience and supervision lessons port well. The placement and PolyKV
machinery mostly does not.

---

## 1. Feature map (the fork side)

### 1.1 Resilience: "agents retry and never fail on infra" (#104, `cea4a1247`, 2026-09-25)

**What.**
- `classifyTurnFault` / `classifyTurnFaultError` (`shared/src/agents/turn-faults.ts:228`, `:267`) sort a failed turn into one of two classes:
  - `transport`: refused or reset connections, 502/503/504, "server is shutting down", "loading model" (`:39`, `:89`).
  - `refusal`: admission refusals and 429 (`:167`).
  - KV eviction is a refusal too (`:95`, `:112`).
- `AgentConfig.recoverTurnFault` hands the fault to `createTurnFaultRecovery` (`core/src/extensions/tools/team/turn-fault-recovery.ts:168`). It waits on the node's `GET /health` with exponential backoff capped at 30 s and **no deadline**. It re-sends the turn *before the failed message enters the transcript*, with no limit, and Stop always wins.
- Refusal backoff runs 2 s → 60 s (`turn-fault-recovery.ts:47`, `:53`, `:56`).
- A refusal before admission always goes back to the front of the queue. The node is held, growing from 5 s up to `REFUSED_HOLD_MAX_MS` = 60 s (`core/src/extensions/tools/team/placed-run.ts:67`, `:70`). `MAX_REFUSED_REQUEUES` and `MAX_STALLED_DURABLE_REFUSALS` were deleted.
- Classified retries were added later:
  - engine partial eviction and the speculative-batch throw (`8e284da5b`);
  - "Invalid input batch" as transport (`403b1aa04`);
  - OpenCoti window refusal as a refusal (`677d5d7b3`).
- The error *class* also reaches the lead, as `failureClass: "infra" | "task"` per agent (`core/src/extensions/tools/team/batch-report.ts:190`, `:222`).

**Why (measured).** Run 1tmrl (build .191, 75 agents): the server restarted twice. Every agent with an open stream ended on `server is shutting down`. Thirteen were never admitted because a durable "tps below floor" refusal became their final result, and 18 more were refused later and ended at once. Swarm 0926: 29/75 agents ended. 11 were eviction while holding 20–31k of a 64k window, 4 speculative-batch, 9 loop-guard misreported as cancelled (`8e284da5b`).

**Map.** Our `ChatClient` retries `{408,429,500,502,503,504}` with outage markers (`claude_hooks/get_advice/chat_client.py:39-67`). When the budget runs out, the lane raises and becomes a tombstone (`consultants/engine/council.py:2541`). We do not separate "the cloud was down" from "the model failed the task", and neither does the synthesizer prompt (`council.py:484-503` only tells it a lane failed). The council has a deadline (`consultants/engine/control.py:56-69`), and the fork's "no deadline" stance does not transfer. What does transfer:
- make infra waits deadline-bounded rather than attempt-bounded;
- tag tombstones `infra` vs `task`.

### 1.2 Lead nudges and stuck-agent reports (`cea4a1247`, `2c177bb04`, `5f677aa01`, `a0ca2159f`)

**What.** `createAgentTroubleWatch` (`core/src/extensions/tools/team/agent-trouble.ts:256`) tracks how long each agent has been waiting continuously: a refusal, an unreachable node, a PolyKV room wait. Engine output for the agent ends the stretch.
- After `LEAD_NUDGE_AFTER_MS` = 10 min (`:35`) the lead is told once per agent, batched over `LEAD_NUDGE_BATCH_MS` = 30 s (`:49`). The report lists who is stuck, why and since when, says nothing was stopped, and asks the lead to consider `stop_agents` and doing the task itself.
- After `NO_NODE_AFTER_MS` = 2 h (`:41`) a second report asks for a decision. Session wlafh: the server died and ten agents waited 6 h while the lead sat in `await_agents`.
- Nothing is ever auto-stopped.
- A message for the lead ends a blocking `await_agents` or spawn wait (`rounds.wakeAwaits`; `b00ff0a05` covers a steer that was queued before the wait began).

**Map.** Our "lead" is Claude driving the review loop through the skill. The engine never tells Claude that a council is wedged on infra. `DeadlineWarning` exists (`consultants/engine/events.py:155`), but nothing says "lane 3 has been waiting 10 min on a 503 storm". The fit is a cheap `InfraWait` event plus a field in `show`/`--wait` output. The mailbox could carry it too.

### 1.3 Restart-safe pools and liveness (#104, #106 `60b60ebf6`, `d6ffd8b1f`)

**What.**
- Every PolyKV server root has a *generation*. A restart (`X-OpenCoti-Boot-Id` changes, a pooled turn comes back without `X-Context-Window`, or `pool_unknown`) bumps it, and every agent re-attaches by a stable logical key.
- A heartbeat watchdog treats 3 × ping + 5 s = 35 s with no byte as a dead server (`OpencotiServerSilentError` → transport → `/health` wait).
- The `/health` probe returns a cooled-off node as soon as it answers (`NODE_PROBE_INTERVAL_MS` = 5 s, `core/src/extensions/tools/team/agent-placement-queue.ts:124`), with the 30 s cool-off as the upper bound (`:113`).
- Lesson `d6ffd8b1f`: the same boot id with a check that did not answer is a missed check, not a restart. A false "restart" abandoned every owner and cost ~10 min.

**Map.** This is mostly OpenCoti-specific. The transferable rule: **classify a fault by the server's identity, not by one failed probe.** Our llamafile daemon has a PID file. Ollama has no boot id. Low port value.

### 1.4 Batch report: always fits, names everyone (`cea4a1247`, `a14dde050`, `e0afd0557`, `ddd206303`)

**What.**
- A finished agent whose report is over `AGENT_SUMMARY_MAX_CHARS` = 1,000 is asked for a summary of at most 1,000 chars (`core/src/extensions/tools/team/agent-reports.ts:18-30`). The full report is filed for `read_agent_report(name, start_line)`, which pages it 200 lines / 12k chars at a time (`:20-23`).
- `buildSpawnBatchReport` (`core/src/extensions/tools/team/batch-report.ts:402`) fills a 28,000-char budget (`:49`), measured on the JSON the model actually receives, in this order:
  1. `summary` (counts by status, type and failure class; iterations; tokens);
  2. a compact `agents` index with one ≤120-char line per agent (`:52`), shrinking to `name|status|class` strings;
  3. whole `reports`, as many as fit;
  4. `notShown`, by name.
- Nothing is left to middle-truncation.
- Every delegated answer is bounded the same way: async team runs (B-7), sync `team_run_task`.

**Why.** Tool results are capped at 32k chars and middle-cut. 75 reports (209k chars) reached the lead as 26 reports. Agents were dropped *by name*.

**Map.** Our synthesizer gets the lanes' reports through `budget.plan`/`compact_messages` (`consultants/engine/budget.py:288`, `:388`). With 2 lanes × 2–3 models this is fine. At xmax with critic fan-out, compaction can drop a whole lane silently, the same failure the tombstone fix (`council.py:2536-2541`) was written for. The fork's "index of every lane first, whole reports while they fit, the rest named + pageable" order is the right shape for the synthesizer and critic inputs.

### 1.5 Sampling diversity for identical models (`9078bd9f2`, `6222ce903`, 2026-09-25)

**What.** Every spawn path accepts `temperature`, `seed`, `seed:"random"`, `temperature:"random"` and `temperature_range`.
- **Defaults** (`core/src/extensions/tools/team/spawn-sampling.ts`):
  - `DEFAULT_TEMPERATURE_RANGE_PERCENT = 2` (`:54`);
  - `MAX_TEMPERATURE_RANGE_PERCENT = 10` (`:57`);
  - random seed in `[0, 2^31)` (`:60`).
- **Seed offset.** A seed covering N agents is offset by index: count 3 with seed 7 runs 7, 8, 9. A call-level seed gives worker i `seed + i`, and an entry's own seed wins (`9078bd9f2`).
- **Random temperature** is drawn uniformly within ±range% of the model's own temperature, rounded to 3 decimals and floored at 0 (`drawSpawnSampling`, `:272-306`).
- **Base temperature** comes from the first source that states one: the connection's `temperature`, then `providerConfig.sampling.temperature`, then the Ollama Modelfile via `/api/show`, then OpenCoti `/props`. If none states one, the temperature is left unset, with an info line ("model temperature unknown; kept the model's sampler") (`:323-334`).
- **Draw lifetime.** The draw is made once per agent at the spawn leaf, so re-placement keeps it. `restart_agent` redraws a random seed (`608a03e6d`).
- **Recording.** The realized values are put on the row, the result and the persisted teammate spec (restored as drawn).
- **Tool-text guidance:** "a draw within 2% … is the intended variation. Set `temperature_range` only when the user states a number" (`:145`).

**Map.** `claude_hooks/model_sampling.py` resolves one template per model (shipped → user → env, `:53-55`, `:174`, `:179`). It can set `seed`, but every lane of the same model gets the **identical** sampler. The x-tier Send loop (`consultants/engine/graph.py:980-1005`) iterates `models_per_lane = [primary] + researcher_extras`. Two plan-item lanes on the same primary, and any duplicated `extra_models` entry, therefore sample identically. A synthesizer fallback chain or critic fan-out that repeats a model does the same. The fork's numbers are a ready-made default: `seed = base + global_lane_idx` when a model repeats, and an optional ±2% temperature jitter. Record the realized sampler in the `llm_call` row.

### 1.6 Struggle and complexity detection (escalation phases 4–6, 2026-09-13/14)

**Lead struggle detector** (`core/src/runtime/safety/struggle-detector.ts`). It was built from 360 harness run digests replayed against iteration timelines (`cd6cbf73f`).
- **Constants:** `STRUGGLE_WINDOW = 10` (`:34`), `STRUGGLE_FAILED_CALLS = 6` (`:64`, moved from 4 in `29f113533`), `STRUGGLE_DISTRESS_HITS = 2` (`:67`), `STRUGGLE_MIN_ITERATION = 20` (`:76`), `STRUGGLE_MAX_PER_TASK = 2` (`:79`), `STRUGGLE_EDIT_STREAK = 3` (`:94`), `STRUGGLE_FAILED_TRANSACTIONS = 2` (`:123`).
- **Rule** (`inspect`, `:660-720`):
  - an edit streak of 3 refused edits, or ≥2 thrown-away transactions, fires regardless of iteration;
  - otherwise it needs **failed calls ≥ 6 in 10 turns AND (distress ≥ 2 OR hedging no longer decaying)** at iteration ≥ 20;
  - at most once per transaction and twice per task;
  - never twice for the same diagnosis over an unchanged file set;
  - a quieter `nudge` fires one failure short of the trigger.
- **Measured findings:**
  - Absolute hedging separates nothing: "wait" runs 5.4–11.9 per 1k words with no relation to outcome. Its *decay* against the run's own opening does separate.
  - Distress words alone catch 86% of failures and fire on 46% of good runs.
  - The conjunction reaches 56% recall at a 4% false alarm, firing 42% of the way into the run.
  - The old threshold of 4 failed calls was "unreachable", not conservative. The fix to count refusals hidden inside successful results (`refusalIn`) saw 50 failures where 3 had been counted.
  - At 6: 13% false alarm, 57% recall.
  - ≥2 thrown-away transactions: 0/236 false alarms (`5fc435627`).
- **Lexicon is corpus-gated** (`5de2e87f7`, `f647143c1`, `be4c71735`): "`regressing` 86 occurrences in 19/198 sessions; `going circles` 0 → removed".
- **Delegation rides the nudge.** It is gated on `spawn_agent` being on and the endpoint serving more than one request at a time (`5fc435627`).

**Worker struggle supervisor** (`core/src/runtime/safety/worker-struggle.ts`, #92 `c8ef7aff4`, extended to every delegated agent in `8c26d9d11`):
- **Thresholds:**
  - `WORKER_STRUGGLE_MIN_ITERATION = 8` (`:64`);
  - `WORKER_NUDGE_AFTER_ITERATIONS = 20` (`:67`);
  - budget window 6 (`:70`), 3 budget-exhausted turns to nudge (`:73`), 2 more to stop (`:76`) after a 2-turn grace (`:79`).
- **Phases:** `watching → nudged → stopped` (`createWorkerStruggleSupervisor`, `:238`).
- **What it stops on: only sustained thinking-budget exhaustion** (`reasoningHitBudget`). The turn count and the detector verdicts only nudge.
- **The first design was wrong.** Replay of 150 real workers: it caught 0 real loops and would have stopped 3 workers that answered. The "grinders" were errors: 16 llama.cpp `Invalid diff`, 8 admission refusals, 1 HTTP parse error.
- **The retuned design**, on the same replay: the one real loop (k76ar4) is nudged at turn 6 and stopped at 13. Of the 121 workers that answered, 0 are stopped and 5 are nudged (PLANS.md §7p, lines 613-635).
- **The nudge is worker-shaped:** "commit your best finding now as a SUMMARY" (`describeWorkerNudge`, `:224`).
- **A stop suspends** the agent as `awaiting_lead` with reason "struggling"; it does not end it (`8c26d9d11`). The reasoning tail is recovered as the digest (`digestOf`/`recoverReasoning` in `spawn-swarm-tool.ts`).

**Cognitive complexity** (`core/src/extensions/complexity/walker.ts`, `36e9d7be8`, `9f0a84083`, `59513de84`):
- tree-sitter, 36 grammars, scored per file as its *hardest function*;
- the score travels with its ladder: 0-4 simple, 5-14 moderate, 15-24 high, 25-59 very high, 60+ extreme;
- calibrated over 5,695 functions, so 15+ is the worst 6.8%;
- any failure means "not measured", never "simple";
- used only as a tiebreaker in the escalation assessment and appended to `check_file`. "Nothing here lets it be evidence that escalation is warranted."

**Map.** We have no struggle signal per lane. `capped_thinking` condenses a budget-cut turn into a note (`claude_hooks/agent_loop/runner.py:552-563`), but nothing counts how often that happens. The worker supervisor's "3 capped turns in 6 → nudge; 2 more after a 2-turn grace → force the final report" fits on top of the existing note path and the empty-final fallback (`consultants/engine/council.py:2557-2590`). The hedging-decay feature could also feed the xauto `low_confidence` signal. Complexity is low value for a read-only council.

### 1.7 Escalation ladder: the expert model (`314f3fe72` … `62b7d39ce`)

**What.** A fifth model scope, "Escalation", is a deliberately costlier expert.
- **The `escalate` tool** (`core/src/runtime/escalation/escalate-tool.ts:26-40`) is decorated onto the session only when an expert is configured. Its description puts the cost first and the licence to use it anyway second: warning only about cost "produces a model that never escalates and stops anyway".
- **The expert gets a *brief*, not the transcript** (`core/src/runtime/escalation/brief.ts`): task, goal, standard, the open transaction, and *what earlier transactions tried*. A discarded transaction leaves no trace on disk.
- **Budgets:** `DEFAULT_MAX_ESCALATIONS = 3` per task, `DEFAULT_MAX_FOLLOW_UPS = 20` per escalation (`core/src/runtime/escalation/escalation-session.ts:59`, `:61`). Held vs closed expert conversation is a setting: warm cache vs freeing a local slot. A failed hand-over is refunded (`5ba3dcd22`).
- **The harness's own assessment** (`core/src/runtime/escalation/assessment.ts`, `270c9eca6`) goes beside the model's self-report, to the user and to the expert. Every line carries its replay error rate, and none is a verdict.
- **Forced escalation** (`core/src/runtime/escalation/forced-escalation.ts`, `7edbfab26`): when a terminal guard (loop guard, mistake limit, reasoning-loop streak) would end the run, it is spent *once* on the expert.
  - Evidence: the loop guard ended 40 of the 88 failures and 3 of the 243 successes.
  - The offer is "deferred, never removed": the run must call `escalate` on the very next turn or it ends.
- **Non-blocking hand-over** (`5a6c1d742`, gated `alternateWithBase`, off by default):
  - the base model stands down: write tools refused, `run_commands` warned (`core/src/runtime/escalation/stand-down.ts:49-58`);
  - `wait_for_expert` never returns "nothing yet";
  - batched expert notes every 30 s (`core/src/runtime/escalation/expert-notes.ts:102`), never its reasoning;
  - every write is logged as a revision;
  - the loop and struggle guards run on the expert, but only as notes;
  - a mailbox runs base → expert.
- **On failure** (`62b7d39ce`): the files the expert changed are named, its transcript is saved, and a usage limit (402) is surfaced. The motivating case: glm-5.3 worked 70 min, hit the plan limit, and the base was told "nothing has changed".

**Map.** Our xauto escalator (`consultants/engine/escalation.py:250` `next_escalation`, tiers at `:132`) escalates **breadth** (rounds 1→3→8, reroutes 1→2→5, confidence target 0.60→0.70→0.75, multi-critic at xmax). It never escalates the **model**. The fork's ladder suggests a per-lane model rung: a lane that trips a struggle or terminal signal (tombstone-bound, capped-thinking streak, loop) is re-run once on a stronger `escalation_model` with a brief (plan item + what the lane tried). Budget: once per lane, ≤N per council. The fork's other lessons apply directly:
- "brief, not transcript";
- "refund on failed hand-over";
- "name what the expert changed on failure";
- "harness assessment beside the self-report". Our critic/refuter is the model's assessment. A counted assessment (tool failures, stalls, capped turns, citation-lint rejects) would be the harness half.

### 1.8 Iteration caps, awaiting_lead, resume (`fdee39e61`, `d8dcee6a3`, `6dd71aad2`, `93e4c7013`, `308ded6b3`, `8d0fd97ef`)

**What.**
- **Out of turns is a state, not an error.** A run that runs out of turns ends with `limit: "max_iterations"`, not as an error (`fdee39e61`). Before this, a side turn's own cap was reported to the lead as "fixer agents truncated at 4 iterations".
- **A capped agent is suspended** in `awaiting_lead` with its transcript, overlay and round slot kept (`runDelegatedWithCap`, `core/src/extensions/tools/team/agent-iteration-cap.ts:759`). The same path is used for the loop guard (`stoppedByLoopGuard`, `:633`) and the struggle supervisor (`stoppedBySupervisor`, `:621`).
- **Resume:** `resume_agent` adds `DEFAULT_RESUME_ITERATIONS = 30` (`:66`), or the spawn cap again. Notices are batched over 3 s (`:69`).
- **The lead no longer sets iteration caps** (`8d0fd97ef`: the lead in h0o2o had set 25 and 4 agents died on it). Every 60 iterations the lead gets a batched "long-running agents" note, and nothing is stopped.
- **Cap warning** (`capWarningMargin`/`createCapWarning`, `:708-718`): a capped agent is told to wrap up a few turns before the cap. One the lead stops at the cap writes its report in one last tool-less turn (`FINAL_REPORT_NOTE`, `:654`).
- **Engine session kept while the lead decides** (`93e4c7013`): releasing it made 32 agents re-admit 64 times as "new" sessions behind a tps floor, for up to 30 min.

**Map.** Our researcher cap (`caps.researcher_loop_iters`, `consultants/engine/council.py:2407`) ends the loop. The empty-final fallback then forces a tool-less summary (`:2557-2590`). That covers the "final report" half. We do not have the "wrap up in K turns" warning before the cap. It is a cheap prompt injection in `run_loop` (`claude_hooks/agent_loop/runner.py:452`) and turns a forced summary into a planned one.

### 1.9 Loop guards on the agent loop (agents package)

The rule behind all of these: **a nudge is cheap, a kill is not.** Thresholds come from replayed corpora, and each nudge is rationed.
- **`ReasoningLoopGuard`** (`agents/src/reasoning-loop-guard.ts:43`, `823f20dc6`) watches the *streaming* reasoning channel for collapse and cuts the request.
  - Signals: low-uniqueness (≤4 distinct lines in a 60-line window after 4,000 chars), line cycles (period ≤6, ≥20 cycles), phrase cycles inside one long line (≥2,000-char line, period ≤120, ≥12 cycles).
  - `maxConsecutiveTrips = 3` ends the run.
  - "Length is never the signal": healthy turns run 150–260k chars.
  - On by default, because "this one guards spend".
- **Reasoning repetition** (`agents/src/reasoning-repetition.ts:50`, `eaf80ef5d`) catches a model restating the same conclusion while still calling tools.
  - Paragraph duplicate fraction ≥0.25 AND a repeat count ≥4. Only paragraphs that carry a sentence count; bare code quotes do not.
  - 1.2% fire rate out of sample, all hand-verified loops.
  - One nudge per run, earned back after a cooldown.
- **Non-convergence** (`agents/src/agent-runtime.ts:468`, `c5d421167`): 3 consecutive turns with no tool call, each ≥2,000 chars of reasoning, earn a nudge, never a kill. No text property separates those turns; the streak does.
- **Thinking-only turn** (`4c3aa0ec7`, `agents/src/agent-runtime.ts:316-331`): a reply with reasoning but no answer and no call is asked again, up to 3 times. A separate reminder covers a tool call *written inside the thinking* (`TOOL_CALL_MARKUP`). Measured: 13/15 agents in k3ba1 ended "without an answer of its own"; in 11 of 23 such turns the call sat inside the thinking.
- **Tool call unparsable** (`79bda2162`, `6b7a21ca2`, `c8ef7aff4`): Ollama's Go XML parser errors, llama.cpp's `Invalid diff: now finding less tool calls!` and OpenCoti's `tool_call_rejected` are classified `tool_call_unparsable` and asked again (twice max). The retry *names the argument the value swallowed*. The payload is never repaired: "a call cut mid-argument, patched up and executed, writes the fragment over the file".
- **Repeated-call loop guard** (`core/src/runtime/safety/loop-detection.ts`):
  - strike limit 6 (`:43`), futile strike 4 (`:63`), result-cycle repeat 3 / hard 6 (`:427`, `:441`);
  - polling tools are exempt (`POLLING_TOOL_NAMES`, `:278`, `25f19467c`), and so are browser scrolls (`6c3c3fa58`);
  - the abort names the repeated call `name {arg:"value…"}` (`8d0fd97ef`);
  - revision ordinals are stripped before results are compared, because "the number that makes the message useful made the loop invisible".
- **Many nudges in one turn are all delivered** (`b47831f8d`); before, only one handler spoke per turn. The budget went from 3 nudges to 1, because "the second nudge was answered with the same sentence … and has never changed an outcome".
- **Every nudge emits a `kind:"nudge"` notice** with the quantity it fired on (`0fa66bef2`). Without that there is no evidence to tune thresholds.

**Map.**
- `run_loop` stubs exact `name|args` duplicates (`claude_hooks/agent_loop/runner.py:309-336`). That is the fork's oldest guard and nothing more.
- We have **no reasoning-collapse guard**. This matters more for us than for the fork. `chat_streamed` counts every reasoning chunk as liveness (`.claude/rules/consultants-runtime.md`), so a collapsed thinking stream keeps a call "alive" past the stall monitor until the per-call hard cap (5–60 min, `consultants/engine/stall_defaults.py:256`). That is billed cloud time.
- We have no unparsable-tool-call classification (grep finds none in `claude_hooks/` or `consultants/`).
- We have no thinking-only re-ask; the loop simply ends on that turn.

### 1.10 Rounds registry, agents_status and the lead's controls (§13, `459f43cc5`, `539ffc450`, `608a03e6d`, `659837c80`, `134eb25d7`, `4cbe228dc`)

**What.**
- **Rounds.** Every delegation opens a *round* in a per-session registry (`core/src/extensions/tools/team/agent-rounds.ts`), persisted to `<session>.rounds.json` (`:22`) with a 1 s debounce (`:539`). Each agent's record keeps:
  - its original task, role, sampler and cap, and where it ran;
  - its state: queued, running, waiting_infra, awaiting_lead, done, failed or cancelled;
  - why it stopped: iteration cap, overflow, mistake limit or loop guard, engine error, task error, cancelled by lead, user or session, or interrupted.
  - Why: after compaction the lead "no longer had the tasks it had given".
- **`agents_status`** (`core/src/extensions/tools/team/agent-status.ts`, cap 12k chars `:62`) is read-only, bounded, and does **no new polling**. It reads the records, the queue and the last `/kv`. It includes the retry policy in words (`RETRY_POLICY_TEXT`, `:74`).
- **Controls:**
  - `requeue_agent`: next boundary, transcript kept, a "slow" or "malfunction" reason avoids the current node;
  - `restart_agent`: fresh overlay, seed redrawn;
  - `resume_agent`;
  - `retry_failed(round_id)`: reruns failed and cancelled agents from their stored tasks;
  - `message_agents`;
  - `stop_agents`, graceful by default. A graceful stop runs one tool-less report turn, so a stopped agent's work is not lost (`659837c80`: the lead stopped 13/15 agents in czbnh and got their file changes with no word of what they were).
- **Interrupted rounds.** A round the session's end interrupted is written as interrupted *before* anything is stopped. The lead is told on reload with `retry_failed` instructions (`4cbe228dc`).
- **Spend ledger** across requeues, restarts and resumes (`9396f6b96`): nothing lowers a total. Cached vs fresh input is split (`dc06c27ec`): "600k in for a 30k prefix sent twenty times" was read as cost.

**Map.** We have M9's control surface (7 routes, 8 CLI verbs), `transcript.db` `llm_call` rows and `NodeFinished{ok,error}` events (`consultants/engine/events.py:87`).
- **Missing: a per-lane stop reason** with an infra/task class and a **`retry_failed` for tombstoned lanes**. Today a follow-up reruns the whole council warm. Re-running one failed lane from its stored plan item, inside the same session, is cheaper.
- **Missing: graceful cancel with a report turn.** Our cancel stops (docs/consultants.md "Cancel and pause actually stop the run").

### 1.11 Swarm scheduling, placement, concurrency per provider/account (§7b, §9, 2026-09-22/23)

**What.**
- **Static widths are wrong.** `DEFAULT_MAX_PARALLEL_TOOL_CALLS = 8` (`agents/src/agent-runtime.ts:956`) is right for reads and wrong for delegation, which already owns its concurrency. So a tool may declare `boundsOwnConcurrency` (`:3632`, `shared/src/agent.ts:307`, `81155b618`: "forty requested agents ran eight wide behind the tool pool").
- **The swarm grows while it runs** (`aba0efdf6`): a work queue under a supervisor replaces `Promise.all` over a fixed array.
  - It launches while the engine says yes and stops at the first no.
  - Then it waits for a worker to finish, or for the 5 s tick (`core/src/extensions/tools/team/spawn-swarm-tool.ts:391`).
  - `SWARM_RUNAWAY_MAX = 64` is a spin guard, not a size (`:385`). The user ruled "8 is miserable".
- **The probe must not take the slot** (`canAdmitMore`, `core/src/extensions/tools/team/agent-admission.ts:111`). A fresh capacity read is "bought by an agent finishing and by nothing else", because each read folds the engine's learner (`8b10ddf0e`).
- **Agent nodes:** up to 10 heterogeneous provider configs with priority 1–10 and an opt-in priority 0 (the lead's own PolyKV session, cap 8) (`core/src/extensions/tools/team/agent-placement.ts:58`, `:64`).
  - Round-robin within a tier, on **live occupancy** rather than a spawn counter: a freed tier-1 slot pulls the next agent back up (`placeAgent`, `:113`).
  - Every tier full means queue, drained FIFO (`agent-placement-queue.ts:226`).
  - Capacity 0 means off, not unbounded. `Infinity` means the endpoint decides (`88dc254eb`).
- **Node health** (`agent-placement-queue.ts:104-152`):
  - an unreachable node cools off for 30 s, probed every 5 s;
  - "model not found" re-places the agent and cools the node for 10 min, bounded at 3 nodes, and only if the attempt spent nothing (`dc3cd2248`);
  - a 502/504 "Bad Gateway" with zero tokens marks the node down (`addb27abc`).
- **Per-node pacing:** one agent per node may be started-but-not-admitted at a time (`413a6b680`). This is applied only on PolyKV-paced nodes (`7bb9d692c`); elsewhere agents start together.
- **Relocation:** an admitted agent a node refuses is moved to a node with room, transcript kept (`1996ad19d`). The lead in h0o2o had done this by hand 21 times.
- **Per-endpoint slot counts:** "one server's slot count is not another server's" (`0082e5bfe`). The slot gate is re-entrant along the spawn chain (`8654cbab1`: a one-slot endpoint deadlocked for 2 h when a sub-agent spawned).
- **Delegation is withheld when the endpoint can run only one request at a time** (`a3e069423`). A withheld `spawn_agent` says why (`6c3c3fa58`).

**Map.**
- `claude_hooks/ollama_slots.py` is our connection-slot accounting. It has a cloud scope for the whole account, sized as plan connections minus `reserve_for_hooks` (`:1-60`, `:172`), a scope per local server (`:186`), cross-process file locks, FIFO and fail-open (`:272`). Slot-queue time does not count as a stall (`claude_hooks/get_advice/chat_client.py:647-655`).
- That covers the fork's "per-provider/account limits" more simply. We do not need placement tiers.
- **The transferable pieces:**
  - **a work queue instead of folding.** `FANOUT_MAX_LANES = 2` folds extra plan items into two lanes (`consultants/engine/council.py:879-905`, docs/consultants.md:382-401). With the slot limiter already queuing every HTTP attempt, more lanes could simply queue, so plan items stay separate. "A full window is a wait, not a lost worker" (`17d3f63bf`). Caveat: every queued lane still holds a stall clock, deadline time and tokens.
  - **"model missing" → next model in the role chain**, without spending a retry budget. Today only the synthesizer walks `extra_models` (docs/consultants.md:1068-1088).

### 1.12 Event streams (`3375d3632`, `9725ec24e`, `bdb2ab2b4`, `27353c047`, `bcbd0823a`, `9e0358ee5`)

**What.**
- **Sub-agent events are stamped** with `agentId`, `conversationId` and `parentAgentId` in the adapter and in `emitLegacyEvent`. Before this, 50 agents' reasoning streamed into the lead's chat interleaved.
- **Row phases:** waiting for the model, queued on the server, prefilling n/N, generating, writing an editor call, compacting. `tool-input-progress` makes a tool call being written count as work.
- **A silence says what it is.** Without a phase, a prefill or server queue read as "idle".
- **Inspect** streams the live output only to a subscribed row, batched per 100 ms, capped at 64 KB.
- **Events no longer deep-copy the conversation:** 165k events against a 554k-char conversation.
- **Harness notes are tagged `[SYSTEM MESSAGE]`** (`core/src/runtime/turn-queue/harness-notes.ts:11`), merged by kind (`mergeSideTurnRecaps`, `:37`), labelled as the harness's rather than the user's, and **dropped on a user Cancel** (`7e94d386b`). A queued recap once started a 58k compaction.

**Map.** Our `CouncilEvent` family (`consultants/engine/events.py:43-272`) already carries `role`, `round` and `lane_idx`, plus `model` on `NodeStarted`. SSE polls the recorder. Gaps:
- a **phase** on in-flight calls (queued-for-slot / prefill / generating / tool), which `chat_streamed` already knows;
- `model` on `ToolCall` and `NodeFinished`.

### 1.13 Timings (`a3ce3283d`, `e22cdc0ea`, `162292870`, `ec965b122`, `1a7f36a58`, `93d3843b0`)

**What.** Every request carries `RequestTimings` from two clocks: Cline's own (wall, TTFT) and the engine's (prompt vs eval time). The rules:
- **Engine total minus wall is queueing.** "A request whose measured total far exceeds the engine's spent the difference queueing."
- **Prefill rate only from the evaluated tokens.** `prompt_eval_count` includes the cached prefix, so dividing by `prompt_eval_duration` reported 1.44M tok/s. A fully cached prompt has *no* prefill rate.
- **Generation rate:** derived only if the engine does not report one, and labelled `gen*`.
- **Per-turn/per-tool boundary trace** behind `CLINE_TIMING`, never per token: one session put 260,774 of its 261,885 lines in debug and took the host down. It found a flat ~650 ms per-tool floor, and listeners were timed too.

**Map.** `ChatClient` keeps `prompt_eval_duration`, `eval_duration` and `load_duration` from Ollama's final frame (`claude_hooks/get_advice/chat_client.py:834-840`). The stall rules say to recalibrate thresholds from `transcript.db` `llm_call` rows (`.claude/rules/consultants-runtime.md`). Recording **TTFT**, **slot-queue time**, **engine prompt/eval durations** and **cached prompt tokens** per row would make that calibration exact. The startup-stall threshold is fundamentally a TTFT question (`consultants/engine/stall.py:70-115`).

### 1.14 Teams vs swarm (§7b R3, B-1…B-13, G-6, 2026-09-26)

**What.**
- **The ruling** (PLANS.md:1052-1058): "teammates are fixed load, fixed worker; swarm is … allocated to as many agents as possible." The tool descriptions rule each other out by *shape*, not by tool name.
- **The 13-bug audit of teams:**
  - dedupe keyed on teammate only (B-1);
  - races at the between-task close (B-2, B-3);
  - cancel that did not stop (B-4);
  - shutdown leaking queued runs (B-5, B-6);
  - unbounded answers (B-7);
  - a teammate awaiting its own run (B-8);
  - a stale window grant (B-9);
  - date revivers ×4 (B-10, `4113cd1ec`);
  - persisting on every streamed chunk (B-11: 101 writes for 100 chunks → 0);
  - deleted teammates resurrected (B-12).
- **Teammates got 4 tools instead of 17:** 10,706 → 3,033 schema chars, −72% (`577ed0df8`).
- **Teams are off by default everywhere** (~2.8k tokens a request) (`aa7307225`, `f45163373`).
- **`AgentTool.timeoutMs` has no reader** (`095c81d81`): no agent gets a wall-clock timeout, by design.

**Map.** Our roles are teammates in the fork's sense: fixed load, durable per council. The schema-cost lesson applies to our role tool surface ("why `all_roles` is on", docs/consultants.md:1374). The rule "persist on state change, not on every chunk" is worth checking against `recorder.py`.

### 1.15 Atomic change protocol: excluded by owner

Scope `atomic` (45 commits) is excluded by owner: it is early-phase and not working properly yet, so it is not mapped for inheritance.

### 1.15a Per-agent check (§13 E, `agents` scope, `d8dcee6a3`, `d6c062ffc`, `659837c80`)

**What / map.** A delegated agent can carry `check:{command,expect,must}` (`core/src/extensions/tools/team/agent-check.ts:98-134`, `createDelegatedAgentCheck` `:305`). It applies only to our opt-in `coder` role.
- Three identical failing verdicts end the run as failed.
- A check is **not run on the host when there is no sandbox**.
- A shell's exit 127 / not-found is the environment's fault, not the agent's (`d6c062ffc`: 5 of 6 fixers spent 11–18 iterations on the environment, and 2 changed the host's PATH).
- A check given to review-only agents is warned about (`659837c80`).

These are good rules for any future coder "verify" command.

### 1.16 SDD and skills (`e9ecd5b91`, `689be2628`, `0ff8523d8`)

**What.** A spec-driven skill set (after GSD 2). The plan moved from markdown into **SQLite behind one turn-checked tool**: `sdd`, whose `next` answers the one step that is due, and every out-of-order action is refused with the reason and that step. Live, glm-5.3 via Ollama:
- 3-requirement CLI in **82 s / 37 iterations / 0 refusals**, against **8 m 18 s / 121 iterations** on files;
- a small model's malformed JSON list is named as unreadable rather than read as empty.

**Map.** Indirect. It is evidence that a typed state machine beats prose state for small models, which supports keeping our planner's plan as structured JSON plus `consultancy.json` status. Our tasks system already follows this "file is truth, index is derived" pattern (`.claude/rules/tasks.md`).

### 1.17 Prompt-engineering lessons (all measured)

| lesson | evidence | commit |
|---|---|---|
| Don't argue for a tool; state which is the measurement | "it skips strings" became the model's reason to doubt the scan; 2-for-2 the scan was right | `e825ae5f5` |
| "Verify by running it again", not by reasoning | tok/turn 655 → 3,140 once "run last" replaced "validate by running"; the long turns are bracket counting | `cea985baa` |
| A batching rule caused restore loops | 13.6 restores/run for templates carrying it vs 0.23 | `bce283980`, `204059f3d` |
| One nudge, not three | the second nudge got the identical sentence and never changed an outcome | `b47831f8d` |
| Tell the model its reasoning is not in front of it | gemma4 doubted a repetition notice it could not see | `a16e35478` |
| Pacing: "the harness queues; put every call in one message" | the 75-agent lead argued 5× about whether 75 calls was too many | `599ca8a50`, `addb27abc` |
| "Settle a question with a tool when one can" | three agents spent ~30k thinking tokens each counting braces | `599ca8a50` |
| Mark the recommended option by default | 0/19 `ask_question` calls carried one while it was opt-in | `ed176c801` |
| Per-family templates use `{{DEFAULT}}`; near-copies (≥0.98 word-LCS) are left out | otherwise a copy is a snapshot that drifts | `d64f44b50`, `30907ed79` |
| Generator temperature 0.2 makes models copy; 0.6–0.9 rewrites | kimi-k3 returned the input byte-identical six times | `f6da183d5`, `c434bbfb8` |
| `match:` list = OR, mapping = AND; `/api/show` `family:""` for cloud tags | six templates silently fell through to default.md | `5a6c1d742` |
| Delegated models get their family template too | the expert read upstream's frontier-model prompt | `5a6c1d742` |
| A missing feature must say so | "standing down silently is indistinguishable from being broken" | `fc3801a91`, `a755a58f9`, `6c3c3fa58` |

For us, the lessons that apply directly to `council.py`'s role prompts (`:233-240` preamble and the role tails): state measurements instead of arguing for a tool, ration nudges to one, tell the model what it cannot see, and end mode-branched prompts with an explicit "do X, not Y" (our memory `feedback_report_mode_appendix_needs_explicit_instruction`). A related "rules in the user message" finding comes from the excluded `atomic` scope and is not mapped here.

### 1.18 Resume semantics

**Fork.**
- Rounds are persisted and running ones reload as interrupted (`459f43cc5`).
- The interrupted record is written *before* stopping, and notices queued before session registration are delivered after (`4cbe228dc`).
- Background-round rows keep updating after the lead's run ends (`f61cb1eeb`).
- A resumed session keeps its saved compaction across transcript reshaping (`8de6864e2`). The tool-pairing sanitizer folded parallel results, the prefix hash stopped matching, and the compaction was dropped silently: 58k → 134.5k of a 128k window.
- Pools carry a generation across server restarts (1.3).
- Teammate specs persist with their drawn sampler (`6222ce903`); a shutdown without a reason means "the lead's intent", not "lifecycle" (B-12).

**Ours.**
- Durable checkpoints, an in-flight index, suspend on SIGTERM and resume at startup (`consultants/server/app.py:1392`, `:1477`).
- `MAX_RESUMES = 3` (`consultants/server/inflight.py:34`), plus a 64-iteration stream-rewind bound (`consultants/server/runner.py:1596`).

We are at parity or ahead for graph state. The fork lesson that still applies: **anything keyed on a hash of a message prefix must be re-derived after any message-normalizing pass**, or it is dropped silently. That applies to our tool-loop compaction and to M8 store keys if either ever keys on transcript hashes.

---

## 2. Timing and stall budgets side by side

| knob | fork | ours |
|---|---|---|
| infra wait | unlimited, `/health` backoff ≤30 s; refusal 2 s→60 s | 15 attempts, backoff ≤90 s ≈ 15 min (`chat_client.py:39-41`) |
| silence = dead | 35 s (3×10 s ping + 5 s), only with server keepalive | stall threshold 120 s floor / 300 s default, 1 retry (`stall_defaults.py:238-241`, `control.py:80-81`) |
| per-call hard cap | none (`AgentTool.timeoutMs` unenforced by design) | 3 × p99, clamped 5–60 min (`stall_defaults.py:256-268`) |
| run deadline | none; the lead decides | 20/60/90 min + per extra model (`control.py:56-69`) |
| lead/user nudge | 10 min once per agent, 2 h again; batched 30 s | none |
| node cool-off | 30 s probed every 5 s; model-missing 10 min | n/a (slot limiter fail-open after `max_wait_s` 1800 s, `ollama_slots.py`) |
| iteration milestones | note every 60 iterations, no cap set by the lead | `researcher_loop_iters` 4/4/10/… (`council.py:201-229`) |
| side turn | 10 iterations (`core/src/runtime/host/steer-side-turn.ts:46`) | n/a |

---

## 3. Gap table

Effort: S ≤ 1 day, M 2–4 days, L ≥ a week. Value: what it buys the council given
our short lanes on Ollama Cloud.

| # | fork has | we have | port how | effort | value |
|---|---|---|---|---|---|
| 1 | **Reasoning-collapse guard** cutting the stream (`agents/src/reasoning-loop-guard.ts:43`), on by default "because it guards spend" | stall monitor counts reasoning as liveness, so a collapsed thinking stream runs to the 5–60 min hard cap | port the three detectors (low-uniqueness / line-cycle / phrase-cycle, same thresholds) into `chat_streamed`; on a trip, abort the request, keep the text so far, condense via `capped_thinking` and continue; end the lane after 3 trips | M | **H** |
| 2 | **Turn-fault classes** transport/refusal/task + **failureClass infra/task** in reports (`shared/src/agents/turn-faults.ts:228`, `batch-report.ts:222`) | one retry budget; tombstone `(researcher lane failed: …)` with no class (`council.py:2541`) | classify in `ChatClient`; outage waits bounded by the **run deadline** instead of the attempt count; tombstone carries `class=infra/task`; synthesizer prompt says "infra-failed lane = coverage gap, not evidence" | M | **H** |
| 3 | **Worker struggle supervisor**: 3 budget-exhausted turns in 6 → nudge; +2 after 2-turn grace → stop and recover the reasoning tail (`runtime/safety/worker-struggle.ts:64-87`); replay 0/121 false stops | `capped_thinking` note per turn, nothing counts them | count capped turns in `run_loop`; at 3/6 inject "commit your best finding now"; at +2 jump to the existing forced-summary path (`council.py:2557`); emit a `nudge` event with the count | S | **H** |
| 4 | **Per-lane model escalation**: struggle/terminal-guard → expert with a brief, once (`runtime/escalation/forced-escalation.ts`, `escalation-session.ts:59-61`) | xauto escalates breadth only (`escalation.py:132`) | `researcher.escalation_model`; a lane that would tombstone or that trips #1/#3 reruns its plan item once on it with a brief (item + tools tried + failure); budget ≤ 2 per council; refund on failure; record as `RuntimeMutation` | M–L | **H** |
| 5 | **Sampling diversity**: seed+index for copies; `temperature:"random"` ±2% (max 10%) around the model's own; realized values recorded (`spawn-sampling.ts:54-60`, `:272`) | identical sampler for every lane of a model (`model_sampling.py:174`) | in the Send loop give lane i `seed = base + global_idx` when the model repeats in the fan-out (critic fan-out and fallback chain too); optional `jitter_pct` (default 0, 2 suggested) on the role; write the realized sampler into `llm_call` | S | M |
| 6 | **Unparsable tool-call recovery** naming the swallowed key; ≤2 re-asks; never repair the payload (`agents/src/agent-runtime.ts:361-383`) | none | detect parser errors (Ollama XML, llama.cpp `Invalid diff`, `tool_call_rejected`) in `ChatClient`/`run_loop`; re-ask once with the cause; count separately from HTTP retries | S | M |
| 7 | **Thinking-only turn re-ask** (3), with a distinct "your call was inside your thinking" reminder (`agent-runtime.ts:316-331`) | loop ends on that turn; empty-final fallback only at loop end | in `run_loop`, a turn with reasoning but no content and no tool call → re-ask (≤3) before ending; detect tool-call markup inside the reasoning | S | M |
| 8 | **Batch report** within a 28k budget: summary → index of every agent → whole reports → `notShown` by name + `read_agent_report` pager (`batch-report.ts:49`, `agent-reports.ts:18-23`) | lanes compacted by `budget.plan` (`budget.py:288`) | build the synthesizer/critic input as an index of every lane (lane, model, status, class, one line) first, then whole reports while they fit, then named-only; optional `read_lane_report` tool for the synthesizer | M | M |
| 9 | **Wrap-up warning before the cap** + final tool-less report turn (`agent-iteration-cap.ts:654`, `:708-718`) | forced summary only after the cap | inject "you have K turns left; plan your report" at `max_iterations - K` in `run_loop` (K = 2 at low/medium) | S | M |
| 10 | **Lead nudge on prolonged infra wait**: 10 min once per agent, 2 h again; batched (`agent-trouble.ts:35-49`) | `DeadlineWarning` only | an `InfraWait` event + `status.waiting_infra` in `show`/`--wait`; optional mailbox note to the requesting session after 10 min | S | M |
| 11 | **retry_failed / graceful stop with report turn** (`608a03e6d`, `659837c80`) | follow-up reruns the council warm; cancel stops hard | `POST /v1/consult/{sid}/retry-lanes` reruns tombstoned lanes from their stored plan items inside the session, then re-synthesizes; graceful cancel = each running lane gets one tool-less report turn | M | M |
| 12 | **Lane facts / stop reasons** (iteration_cap, infra, loop_guard, struggle, task_error, cancelled_by) on every result (`agent-rounds.ts:559`, `:2243`) | `NodeFinished{ok,error}` | add `stop_reason` + `failure_class` to `NodeFinished` and the `llm_call`/lane rows; surface them in `show` | S | M |
| 13 | **Cycle-aware loop guard** with polling exemptions, naming the repeated call (`runtime/safety/loop-detection.ts:43-63`, `:278`, `:427-441`) | exact-duplicate stub (`agent_loop/runner.py:309-336`) | add A→B→A result-cycle detection (repeat 3 → warn, 6 → stop the loop into the summary path); strip volatile counters before comparing | S | L–M |
| 14 | **Reasoning repetition + non-convergence nudges** (`agents/src/reasoning-repetition.ts:50`, `agent-runtime.ts:468`), rationed to 1 | none | port only if #1/#3 leave a gap; lanes are short | S | L |
| 15 | **Work queue instead of folding** + non-consuming probe (`spawn-swarm-tool.ts:385-391`, `agent-admission.ts:111`) | `FANOUT_MAX_LANES=2`, plan items folded (`council.py:879-905`) | let lanes exceed the cap and queue on `ollama_slots` (the queue does not count against the stall clock, `chat_client.py:647-655`); keep a runaway max; make sure the deadline accounts for queued lanes | M | M |
| 16 | **Model-missing → re-place**, no retry spent (`dc3cd2248`) | synthesizer-only fallback chain | for any role, a 404 "model not found" moves to the next `extra_models`/fallback without consuming the retry budget | S | M |
| 17 | **RequestTimings**: TTFT, queue = wall − engine, prefill rate from evaluated tokens only (`a3ce3283d`, `162292870`) | engine fields kept in memory (`chat_client.py:834-840`) | persist ttft, slot_wait_s, prompt_eval_s, eval_s, cached_prompt_tokens in `llm_call`; recalibrate startup-stall from the TTFT p99 | S | M |
| 18 | **Harness notes tagged `[SYSTEM MESSAGE]`, merged, dropped on cancel** (`harness-notes.ts:11-37`, `7e94d386b`) | engine-authored injections (follow-up, inject, adversary) are untagged | prefix engine-authored messages with one tag; terse; never shown as the user's | S | L |
| 19 | **Role tool surface minimization** (17 → 4 tools, −72% schema) (`577ed0df8`) | `all_roles` tool surface on (docs/consultants.md:1374) | measure per-role schema chars; drop tools a role never calls (recorder data) | S | L–M |
| 20 | **Prompt rules from measurement**: state the measurement, don't argue for a tool (`e825ae5f5`); one nudge (`b47831f8d`); say what the model cannot see (`a16e35478`) | role prompts in `council.py:233-240` | review the role prompts against these three rules; bench any change | S | M (needs bench) |
| 21 | **Per-agent check** with 3-identical-fails stop, no host run without sandbox, exit-127 = environment (`agent-check.ts:98-134`) | coder has sandboxed write_file (M10), no verify command | when the coder gains a verify step, copy these four rules verbatim | M | L (coder default off) |
| 22 | **Agent nodes / priority tiers / PolyKV / boot-id generations** (§9, #106) | `ollama_slots` cloud/local scopes | do not port; the slot limiter covers account-level limits. Keep "identity, not one failed probe" as a review rule | — | L |
| 23 | **Struggle detector (lead) + hedging decay** (`struggle-detector.ts:660-720`) | xauto `low_confidence` from the synthesizer's self-rating | optional: compute hedging-ratio decay over a lane's reasoning as a counted confidence input next to the self-rating ("harness assessment beside the model's", `270c9eca6`) | M | L–M |

---

## 4. Defaults worth recording verbatim

- Sampling: `DEFAULT_TEMPERATURE_RANGE_PERCENT = 2`, `MAX_TEMPERATURE_RANGE_PERCENT = 10`, `RANDOM_SEED_LIMIT = 2^31`; seed for copy i = seed + i; temperature draw = base × (1 + u·range%), u ∈ [−1, 1], 3 decimals, ≥0; base unknown → leave unset (`core/src/extensions/tools/team/spawn-sampling.ts:54-60`, `:272-306`).
- Worker supervisor: min iteration 8, nudge after 20 tool turns, budget window 6, 3 to nudge, 2-turn grace, 2 to stop (`core/src/runtime/safety/worker-struggle.ts:64-79`).
- Lead struggle: window 10, failed calls 6, distress 2, min iteration 20, ≤2 per task, edit streak 3, thrown-away transactions 2 (`core/src/runtime/safety/struggle-detector.ts:34-123`).
- Reasoning loop guard: minChars 4000, window 60, maxPeriod 6, minCycles 20, maxUniqueInWindow 4, minLineChars 2000, phraseMaxPeriod 120, phraseMinCycles 12, maxConsecutiveTrips 3 (`agents/src/reasoning-loop-guard.ts:43-53`).
- Reasoning repetition: minChars 4000, minParagraphChars 60, minStopwords 3, maxRepeatTrip 4, duplicateFractionTrip 0.25, cooldownTurns 3, maxNudges 1 (`agents/src/reasoning-repetition.ts:50-60`).
- Non-convergence: streak 3 silent turns, each ≥2,000 reasoning chars (`agents/src/agent-runtime.ts:468`); no-tool-call nudges 1 (`:425`); thinking-only re-ask 3 (`:331`).
- Resilience: refusal backoff 2 s → 60 s; node refused hold 5 s → 60 s; cool-off 30 s / probe 5 s; model-missing 10 min, ≤3 nodes; lead nudge 10 min, batch 30 s, second at 2 h (`turn-fault-recovery.ts:47-56`, `placed-run.ts:67-70`, `agent-placement-queue.ts:104-152`, `agent-trouble.ts:35-49`).
- Reports: summary ≤1,000 chars; pager 200 lines / 12k chars; batch budget 28,000 chars, index line ≤120 (`agent-reports.ts:18-21`, `batch-report.ts:49-52`); `agents_status` ≤12k (`agent-status.ts:62`).
- Escalation: 3 escalations per task, 20 follow-ups per escalation, notes every 30 s; terminal-guard escalation once per task (`escalation-session.ts:59-61`, `expert-notes.ts:102`, `forced-escalation.ts`).
- Swarm: tick 5 s, runaway max 64, old default 8 retired as a sizing number (`spawn-swarm-tool.ts:385-391`).

---

## 5. Recommended order

1. **#1 reasoning-collapse guard** and **#3 capped-turn supervisor**. Both close a real money leak. Both are small, and each has a published threshold set and replay evidence.
2. **#2 fault classes** with deadline-bounded infra waits, and the failure class in tombstones and the synthesizer prompt.
3. **#5 seed offset for repeated models**, together with **#17 timing columns**. Both are cheap, and they make later calibration possible.
4. **#4 per-lane model escalation**. This is the one structural feature. Design it after 1–3 are emitting the signals it would key on: the fork's own lesson is that its first thresholds were guesses, and replay overturned them (`c8ef7aff4`, `29f113533`).
5. **#8, #9, #11, #12** as the lane-report and control polish.

The fork's method is worth adopting along with its code:
- every guard emits a `nudge` event with the number it fired on (`0fa66bef2`);
- thresholds are fit by replaying archived transcripts, not by reasoning about them;
- a guard nudges before it kills, and kills only on the narrowest signal the corpus supports.
