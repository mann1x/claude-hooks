# 08 — Cerebriline's guards, scoped for an unattended council

Scope: every guard, safety and supervision mechanism in `/shared/dev/cline`
(`main`, HEAD `d5b94fa90`, 2026-10-10), read at the source rather than from
commit subjects. For each one: what trips it, what it does then, whether a
human is involved, which setting moves it, and whether it runs on the lead, on
delegated workers, or both. Then the mapping onto the consultants council,
which runs **unattended** from submission to answer (plan §0.7).

This extends report 05, which already records the thresholds and the replay
evidence behind most of these guards (05 §1.6–§1.9, §4). They are not repeated
here. This report adds four things:
- the **trip actions**, and who acts on them;
- the **lead/worker split**;
- the **settings** that switch the guards on or make them stricter, with
  "Strong coding nudges" as the anchor (owner, 2026-10-10);
- a **`[guards] profile`** design and a review of the plan's §0.4 and §4 (D1–D13).

Fork paths are relative to `/shared/dev/cline/sdk/packages/` unless they start
with `apps/`. Ours are relative to the claude-hooks repo root.

---

## 0. The finding that shapes everything else

**In the fork, the lead and the workers are guarded differently, and the
difference is the one that matters for us.**

| | lead (the VS Code chat session) | delegated worker (`spawn_agent` / swarm / team) |
|---|---|---|
| Config built by | `core/src/runtime/orchestration/runtime-builder.ts:1690-1704` | `core/src/extensions/tools/team/delegated-agent.ts:440-512` |
| `completionPolicy.maxNoToolCallNudges` | always `DEFAULT_MAX_NO_TOOL_CALL_NUDGES = 1` (`runtime-builder.ts:1702`) | **unset → 0**. Only `onCompletionAttempt` is set, and only when the agent has a `check` (`delegated-agent.ts:502-511`) |
| `strongNudges` | the user setting, forwarded only when `false` (`runtime-builder.ts:1703`) | never read: the nudge budget is 0 |
| No-tool-call / announced-intent / unparsed-text / non-convergence nudges | on | **off**. All four are gated on `nudgesEnabled()` (`agents/src/agent-runtime.ts:1471-1473`, `:1483-1491`, `:2043`) |
| Thinking-only re-ask, unparsable-call re-ask, truncated-turn retry, reasoning-loop cut, repetition nudge on tool-calling turns | on | on (not gated on the nudge budget) |
| Loop guard + mistake limit | on; terminal → `onConsecutiveMistakeLimitReached` → **the user** (`apps/vscode/src/sdk/sdk-interaction-coordinator.ts:72-98`) | on; terminal → no callback → stop (`core/src/runtime/safety/mistake-tracker.ts:230-238`) → **suspended `awaiting_lead`** (`core/src/extensions/tools/team/agent-iteration-cap.ts:621-640`) |
| Struggle supervision | lead struggle detector → nudge / escalation offer (`core/src/runtime/host/local-runtime-host.ts:2120-2200`) | worker struggle supervisor → nudge, then stop → `awaiting_lead` (`core/src/runtime/host/local/spawn-tool.ts:740`) |
| Edit verification, checklist close-out | on (`local-runtime-host.ts:1289-1345`) | not composed |
| Cap warning + final report turn | — | on (`delegated-agent.ts:520-530`, `agent-iteration-cap.ts:654`, `:708-750`) |

**Consequence.** In the fork, *a worker's turn with no tool calls ends its run,
and that turn is its report*. This is the contract a researcher lane has with
us. The "Strong coding nudges" setting, which the owner named as the anchor for
our strict profile, does not touch fork workers at all: it is a lead-only,
interactive-coding setting. Porting it to lanes means **designing** a
lane-shaped version of it (§3), not copying it.

The fork's terminal paths all end at a decision-maker:
- for the lead, that is the **user** ("Send a message to give Cerebriline guidance and continue the task", `sdk-interaction-coordinator.ts:85-87`);
- for a worker, it is the **lead LLM** (`awaiting_lead`, `resume_agent`, `stop_agents`).

The council has neither during a run. Claude, through the skill, reads the
council only after it answers. So every fork terminal action that means
"suspend and let someone decide" has to become a **harness decision with a
named stop reason**. The usual form is a forced tool-less report followed by
`stop_reason` + `failure_class` on the lane (plan D7).

### 0.1 Owner ruling (2026-10-10): loop guards are not a profile matter

**Every loop guard is always on and always enforced, on every backend, cloud
included.** No profile switches them off or tunes them. The reason: cloud models
loop too, especially when the sampling parameters are wrong. That covers:
- reasoning collapse (§2.1);
- reasoning repetition (§2.2);
- non-convergence (§2.3);
- duplicate calls, cycles and A→B→A (§2.9);
- the worker supervisor's thinking-budget loop (§2.12);
- any loop detector added later.

The mistake limit (§2.10) is a stop rather than a nudge, so it is fixed too.

**Every loop trip records the realized sampler** in its `nudge` / `stop_reason` event and on the lane row: seed, temperature, top_p/top_k/min_p, the penalties, `think` level, and the model template it came from (`claude_hooks/model_sampling.py`; plan A5/B4). Two things follow:
- a loop that correlates with a bad template is visible;
- the replay fitting of §0.4 can separate "model loops" from "sampler made it loop".

**The `[guards]` profile covers nudges only:**
- the strong no-tool-call nudges (§3);
- the related re-ask budgets;
- nudge timing.

**Compaction is not linked to the profile.** The reduced flow is the default, and the full 5-step flow is a per-role user opt-in (plan C2, as ruled).

---

## 1. Fork settings that switch a guard on or make it stricter

All of these are global settings in `apps/vscode/src/shared/storage/state-keys.ts`.
The `task*` fields can override some of them per task.

| setting (state key) | values / default | what it controls | where it is read |
|---|---|---|---|
| **`strongNudgesEnabled`** — "Strong coding nudges" | bool, **true** (`state-keys.ts:492`) | Whether a no-tool-call turn on the lead is nudged *without evidence* of unfinished work (§2.4) | UI row `apps/vscode/webview-ui/src/components/settings/sections/FeatureSettingsSection.tsx:106-112`; `apps/vscode/src/sdk/cline-session-factory.ts:2298-2299`, `:2650`; `runtime-builder.ts:1703`; enforced at `agents/src/agent-runtime.ts:1419-1426` |
| `cappedThinkingEnabled` (+ `cappedThinkingPrompt`) | bool, **true** (`state-keys.ts:428`) | The capped-thinking condenser (§2.8) | `cline-session-factory.ts:1984`; `local-runtime-host.ts:1068-1148` |
| `editVerificationSettings.mode` | `"off" \| "nudge" \| "require"`, **`"nudge"`** (`apps/vscode/src/shared/EditVerificationSettings.ts:20-40`) | The unchecked-edits completion guard: nudge = 2 hold-backs, require = 4 (`local-runtime-host.ts:1303`) | `apps/vscode/src/sdk/vscode-session-host.ts:249-252`; `local-runtime-host.ts:1289-1345` |
| `escalationModelEnabled` (+ `escalationModeApiConfiguration`) | bool, **false** (`state-keys.ts:297`) | Whether an expert exists at all, which arms the `escalate` tool and the forced-escalation offer | session factory |
| `escalationSettings` | `requireApproval` **false**, `closeAfterEscalation` false, `maxEscalations` **3**, `maxFollowUps` **20**, `struggleFailedCalls` **6**, `struggleDistressHits` **2**, `struggleWindow` **10**, `struggleMinIteration` **20**, `struggleMaxPerTask` **2**, `struggleEditStreak` **3**, `struggleFailedTransactions` **2**, `alternateWithBase` false, `relayNothing` false (`apps/vscode/src/shared/EscalationSettings.ts:111-128`; proto `apps/vscode/proto/cline/state.proto:616-659`) | Escalation budgets and the struggle detector's operating point (`local-runtime-host.ts:2134`) | `apps/vscode/src/core/controller/state/updateSettings.ts` |
| `focusChainSettings` | `{enabled: true, remindClineInterval: 6}` (`apps/vscode/src/shared/FocusChainSettings.ts:8-11`) | The checklist and its close-out guard (2 hold-backs, `core/src/extensions/tools/task-progress.ts:407`) | session factory |
| `subagentSandboxConfine` / `leadSandboxConfine` | **true** / **false** (`state-keys.ts:456`, `:459`) | Write confinement for commands ("agents run unattended") | report 04 |
| `subagentCommandsEnabled` | false | Whether workers get a shell at all | report 04 |
| `autoApprovalSettings` | per-action | Tool approval asks (user) | `sdk-interaction-coordinator.ts:100-112` |
| `useAutoCondense`, `thinkingCompactionEnabled`, `councilCompactionEnabled` | all true | Compaction (report 02), not guards | — |
| *(SDK only, no UI)* `execution.loopDetection` | on unless `false`; `{softThreshold: 2, hardThreshold: 5}` (`core/src/runtime/safety/loop-detection.ts:408-411`; `session-runtime-orchestrator.ts:731-737`) | Repeated-call loop guard | — |
| *(SDK only)* `execution.maxConsecutiveMistakes` | **6** (`session-runtime-orchestrator.ts:698`). The UI field was **removed** (proto `reserved 139 "max_consecutive_mistakes"`, `state.proto:131-132`) | Mistake limit | — |
| *(SDK only)* `execution.reasoningLoopDetection` | on unless `false` (`agent-runtime.ts:2999-3005`); thresholds `agents/src/reasoning-loop-guard.ts:43-53` | Streaming reasoning-collapse cut | — |
| *(SDK only)* `execution.reasoningRepetition` | on unless `false` (`agent-runtime.ts:1449-1468`); `agents/src/reasoning-repetition.ts:50-82` | Self-repetition nudge | — |
| *(SDK only)* `completionPolicy.maxNoToolCallNudges` / `noToolCallTurnStreakLimit` / `maxTruncatedTurnRetries` / `strongNudges` / `requireCompletionTool` / `completionGuard` / `onCompletionAttempt` | 1 (lead) or 0 (workers) / 3 / 2 / per setting / — | The silent-turn policy | `shared/src/agents/types.ts:1223`, `shared/src/agent.ts:890` |

**Per-model / small-model behaviour.** I searched `core`, `agents` and `shared`
for anything that tightens a guard by model size or family. There is none. The
fork has no "small model" switch, and every threshold is model-independent.
The nearest thing is the user-facing description of Strong coding nudges,
which frames it by *usage* ("coding" vs "mostly to ask questions"). The
reasoning-repetition nudger's rationing is justified by "A nudge costs a small
model attention" (`reasoning-repetition.ts:216`), but that is not a switch.
**Our strict profile is new design, not a port.** Its thresholds need the D-bench
before they ship (plan §0.4).

**Coupling.** No fork guard is tied to `strongNudgesEnabled`. It changes exactly
one branch (`agent-runtime.ts:1419-1426`). The guards that share the
**no-tool-call nudge budget** are coupled to each other: with
`maxNoToolCallNudges = 0`, the generic nudge, the announced-intent nudge, the
unparsed-text nudge, the non-convergence nudge and the silent-turn repetition
nudge are all off together (`:1471`, `:1483`, `:2043`, `:2096`). That budget,
not `strongNudges`, is the real "workers vs lead" switch.

---

## 2. Guard by guard

Each entry gives:
- **Detects** — the trigger, with thresholds and file:line;
- **Trip** — what the guard does, and whether a human is involved;
- **Knob / default**;
- **Scope** — lead (L), workers (W), or both;
- **Ours** — what we have today;
- **Council** — the unattended adaptation, or DROP;
- **Profile** — `always` (loop guards and stops, outside any profile; §0.1), or the standard/strict value (nudges only).

### 2.1 Reasoning-loop guard (streaming collapse cut)

- **Detects:** in the *streaming* reasoning channel, after `minChars: 4000`:
  - `low-uniqueness`: ≤ `maxUniqueInWindow: 4` distinct lines in a `window: 60`;
  - `line-cycle`: period ≤ `maxPeriod: 6`, ≥ `minCycles: 20`;
  - `phrase-cycle`: a single line ≥ `minLineChars: 2000`, period ≤ `phraseMaxPeriod: 120`, ≥ `phraseMinCycles: 12` (`agents/src/reasoning-loop-guard.ts:43-53`, `:110-200`).
- **Trip:** breaks out of the stream, which cancels the provider request (`agent-runtime.ts:3239-3249`). The turn keeps what it produced and the run continues with a status notice (`:4037-4083`). The streak resets on any uncut turn (`:3249`). At `maxConsecutiveTrips: 3` it calls `onReasoningLoopLimitReached`:
  - the lead has an expert → **forced-escalation offer** (`local-runtime-host.ts:2402-2410`);
  - otherwise, and for workers → abort.
  - No user involvement at any point.
- **Knob:** `execution.reasoningLoopDetection` (object or `false`). On by default, "this one guards spend" (`agent-runtime.ts:3000-3001`).
- **Scope:** L + W.
- **Ours:** none. `chat_streamed` counts reasoning deltas as liveness (`.claude/rules/consultants-runtime.md`), so a collapsed think runs until the per-call hard cap (`consultants/engine/stall_defaults.py:256-268`).
- **Council (D1):**
  - port the three detectors into `chat_streamed` and cancel through the existing cooperative abort (`consultants/engine/stall.py:221-245`);
  - keep the produced text, condense it as `capped_thinking` does, and continue;
  - at the trip limit, **don't abort the lane**: run the existing forced-summary path (`consultants/engine/council.py:2557-2625`) from the pre-cut transcript, with `stop_reason=reasoning_loop`;
  - critic and synthesizer get the cut and one retry, then the normal role fallback (synthesizer `extra_models`, `council.py:3001-3010`).
- **Profile:** **always**, for every role, on every backend, cloud included (owner ruling 2026-10-10; §0.1). The fork's thresholds are used unchanged, `maxConsecutiveTrips` 3 included. Every trip records the realized sampler (§0.1).

### 2.2 Reasoning-repetition nudge

- **Detects:** in one completed reasoning block, ≥ `minChars: 4000`, among prose-bearing paragraphs (≥ `minParagraphChars: 60`, ≥ `minStopwords: 3`): duplicate fraction ≥ `0.25` **and** a max repeat ≥ `4`. The rationing is `cooldownTurns: 3`, `maxNudges: 1`, and `recoveryTokens: 4000` earns the nudge back (`reasoning-repetition.ts:50-82`).
- **Trip:** appends one reminder, never cuts.
  - On a tool-calling turn it is always on (`agent-runtime.ts:2283-2293`).
  - On a silent turn it is gated on the nudge budget (`:2093-2100`).
  - No human.
- **Knob:** `execution.reasoningRepetition`, on by default.
- **Scope:** L + W (tool-calling turns); L only on silent turns.
- **Ours:** none.
- **Council:** append the nudge as a user message before the next lane iteration; one per lane.
- **Profile:** **always** (a loop detector; §0.1). It applies on tool-calling turns and on every re-asked silent turn. It fires on only 1.2% of blocks, so the cost is small even on short lanes.

### 2.3 Non-convergence nudge

- **Detects:** `DEFAULT_NO_TOOL_CALL_TURN_STREAK_LIMIT = 3` consecutive turns with no tool call, each carrying ≥ `NON_CONVERGENCE_MIN_REASONING_CHARS = 2000` chars of reasoning (`agent-runtime.ts:447`, `:469`, `:1483-1491`, `:2076-2090`).
- **Trip:** one nudge per run ("Stop analysing and make one tool call now…", `shared/src/prompt/format.ts:261-285`). Never ends the run. No human.
- **Knob:** `completionPolicy.noToolCallTurnStreakLimit`. It requires `maxNoToolCallNudges > 0`.
- **Scope:** **L only** (workers have budget 0).
- **Ours:** n/a. Our loop *ends* on the first no-tool turn (`claude_hooks/agent_loop/runner.py:494-518`), so a streak cannot form.
- **Council:** a streak only exists if we re-ask silent turns. The always-on re-asks (thinking-only, announced-intent, unparsed text) can create one, and the strict re-asks can create more. Streak **2** (lanes are short), same 2,000-char floor, one per lane.
- **Profile:** **always**. It is a loop detector, a thinking loop with no action. Its nudge is not drawn from the strict nudge budget.

### 2.4 No-tool-call nudge, and "Strong coding nudges"

- **Detects:** a turn with no tool call that would end the run.
  - **Without strong nudges** the turn still needs evidence of unfinished work before it is nudged (`agent-runtime.ts:1400-1426`). Any one of these counts:
    - the host names unstarted work (`describeUnstartedWork`);
    - the run already called a tool (`hasCalledAnyTool`);
    - the text ends on a first-person promise and does not claim completion (`announcedIntentWithoutActing`, `shared/src/prompt/format.ts:399-445`: `let me|i'll|i will|i'm going to|…|let's` in the last 400 chars).
  - **With strong nudges** (default) it is nudged regardless.
  - It stands down when the text is the "one short sentence" answer to a previous nudge (≤300 chars, one paragraph, not a question; `answersCompletionNudge`, `format.ts:447-470`; `agent-runtime.ts:1384-1391`).
- **Trip:** `NO_TOOL_CALL_NUDGE_MESSAGE` (`format.ts:185-189`), plus `ASK_QUESTION_NUDGE_CLAUSE` when `ask_question` is registered (`format.ts:209-212`; `agent-runtime.ts:1428-1433`).
  - The clause **routes the model to the user**: "If what you need is a decision from the user, call `ask_question`".
  - Budget `DEFAULT_MAX_NO_TOOL_CALL_NUDGES = 1`, consecutive (`:426`).
  - A second, different nudge quotes the model's own announcement, once per run (`buildAnnouncedIntentNudge`, `format.ts:399-412`; `agent-runtime.ts:2060-2068`).
  - When the nudges are spent, the run ends via the completion boundary (`:2142-2154`).
- **Knob:** `strongNudgesEnabled` (true) and `maxNoToolCallNudges` (1 on the lead).
- **Scope:** **L only.**
- **Ours:** `force_first_tool_call` + one corrective retry exist in `run_loop` (`runner.py:66-68`, `:494-514`), but every council role turns them off:
  - researcher (`council.py:2406-2414`);
  - role tool loop for planner, critic, meta-critic, synthesizer, adversary (`council.py:1607-1619`);
  - tool_executor (`consultants/engine/tool_executor.py:274-283`);
  - coder (`consultants/engine/coder.py:635-643`).
  - The rationale (`council.py:17-20`) is that the planner already gave direction. The empty-final fallback (`council.py:2557-2625`) covers a lane that ends *without text*, not one that ends with an ungrounded report.
- **Council:** see §3, the anchor of the strict profile. Never port the `ask_question` clause: there is no user to reach, and a lane that "needs a decision" should say so in its report.
- **Profile:** the evidence-based part (announced-intent) is **default** for tool-using lanes. The unconditional part ("strong") is **strict**.

### 2.5 Thinking-only turn and tool-call-in-thinking re-ask

- **Detects:** a turn with reasoning and nothing else (no text, no call). The tool-call variant is detected when the reasoning matches `TOOL_CALL_MARKUP = /<\/?tool_call>|<\/function>|<function=|<\/parameter>/` (`agent-runtime.ts:317-360`).
- **Trip:** re-ask with `THINKING_ONLY_REMINDER` or `TOOL_CALL_IN_THINKING_REMINDER`, up to `THINKING_ONLY_RETRY_BUDGET = 3` consecutive (`:332`, `:2155-2195`). It never fires on a turn the reasoning-loop guard cut (`:2158-2161`). No human.
- **Knob:** none; always on.
- **Scope:** L + W.
- **Ours:** the loop ends on that turn. The empty-final fallback then forces a tool-less summary (researcher only, `council.py:2557`). Other roles take `_single_shot`/fallback paths.
- **Council (D3):** port it for every role, since a thinking-only synthesizer reply is as empty as a lane's. Re-asks do not count against `max_iterations`. Budget: standard **2**, strict **3** (fork value). With 4-iteration lanes the cost of a re-ask is a real share of the lane.
- **Profile:** a nudge budget. Standard 2, strict 3 (§4).

### 2.6 Unparsable tool call

- **Detects:** two shapes.
  - Provider parser errors classified `tool_call_unparsable`: Ollama's XML errors, llama.cpp `Invalid diff`, opencoti `tool_call_rejected` with `swallowed_key` (`agent-runtime.ts:362-404`, `:2615-2640`).
  - `<tool_call>` text that came back as content (`unparsedToolCallInText`, `format.ts:300-395`).
- **Trip:** re-ask, naming the swallowed argument when known. `TOOL_CALL_PARSE_RETRY_BUDGET = 2`, reset by any parsed turn (`:406`). The payload is **never repaired**. The text variant is the unparsed nudge and is gated on the nudge budget (L only). No human.
- **Knob:** none.
- **Scope:** provider-error re-ask L + W; text variant L only.
- **Ours:** none (05 #6).
- **Council (D4):** port both variants for lanes, tool_executor and coder. The text variant needs no nudge budget for us, because it is evidence, not a policy question. Budget: standard **1**, strict **2**.
- **Profile:** a nudge budget. Standard 1, strict 2 (§4).

### 2.7 Truncated turn, empty turn

- **Detects:**
  - output cut at the cap (`MAX_TOKENS_INCOMPLETE_TURN_MESSAGE`, `agent-runtime.ts:150`);
  - a reply discarded with nothing delivered (`EMPTY_TURN_REMINDER`, `:304-307`).
- **Trip:**
  - Truncation: retry with the cap halved per consecutive failure, `DEFAULT_MAX_TOKENS_TURN_RETRIES = 2` (`:166`, `:1626-1662`), then end.
  - Empty turn: the reminder "take the smallest useful next step".
  - No human.
- **Knob:** `completionPolicy.maxTruncatedTurnRetries`.
- **Scope:** L + W.
- **Ours:** `_continue_truncated_answer` continues a cut *final* answer (`runner.py:207-273`; owner policy: continue, fail on the role's last call). There is no empty-turn reminder. Plan A2 adds "retry an empty response ≤2".
- **Council:** keep our continuation. Add the empty-turn reminder text to A2's retry. Don't port shrink-on-retry (plan §9).
- **Profile:** always.

### 2.8 Capped-thinking condenser

- **Detects:** a turn whose reasoning ran into the thinking budget. The evidence is either the model's budget message or proximity to the allowance (`core/src/extensions/context/capped-thinking.ts`; wired at `local-runtime-host.ts:1068-1148`, with a separate lead writer at `:1111`).
- **Trip:** an LLM call condenses the abandoned reasoning into a note that replaces it. No human.
- **Knob:** `cappedThinkingEnabled` (**true**) and `cappedThinkingPrompt`.
- **Scope:** L + W (delegated prepare-turn, `:1137-1146`).
- **Ours:** ported for tool loops (`runner.py:144-197`, `claude_hooks/capped_thinking.py:315-434`). It is broken at `high` (plan §10, B3).
- **Council:** already adapted. Plan C4 completes it.
- **Profile:** always.

### 2.9 Repeated-call loop guard → mistake limit

- **Detects:** several layers, all in `core/src/runtime/safety/loop-detection.ts`.
  - **Consecutive identical calls:** soft at `softThreshold: 2`, hard at `hardThreshold: 5` (`:408-411`).
  - **Per-signature failures:** `STRIKE_LIMIT = 6` (`:43`).
  - **Futile no-op edits:** `FUTILE_STRIKE_LIMIT = 4` (`:63`).
  - **(call, result) cycles:** `CYCLE_REPEAT_LIMIT = 3`, soft (`:427`); `CYCLE_HARD_LIMIT = 6`, write-class tools only (`:441`).
  - Polling tools are exempt (`:278`), and so are calls that move their own subject (`:329`).
- **Trip:**
  - A soft verdict is a warning on the tool result (`session-runtime-orchestrator.ts:1808-1821`).
  - The first hard verdict is a **last warning**, and the call still runs (`LOOP_HARD_ESCALATION_LIMIT = 2`, `:455`; `LOOP_FINAL_WARNING` `:462`; `:1822-1850`).
  - The second hard verdict forces the mistake tracker to its limit (`:1851-1859`). Then:
    - **Lead:** `onConsecutiveMistakeLimitReached`. With an expert this is the forced-escalation offer (`local-runtime-host.ts:2382-2401`). Otherwise VS Code posts "Cerebriline stopped the task … **Send a message to give Cerebriline guidance and continue the task**" and stops (`apps/vscode/src/sdk/sdk-interaction-coordinator.ts:72-98`). **The user restarts it.** `LOOP_STOP_NOTICE` says "Retry … or send a message" (`session-runtime-orchestrator.ts:483-484`).
    - **Worker:** stop → `awaiting_lead` (`stoppedByLoopGuard`, `agent-iteration-cap.ts:633`). **The lead LLM decides.**
- **Knob:** `execution.loopDetection` (on).
- **Scope:** L + W.
- **Ours:**
  - the exact `name|args` dedup stub only (`runner.py:324-340`), which survives compaction wrongly (plan §10, C5);
  - polling is not a concern, because lanes are read-only.
- **Council (D10):**
  - cycle detection on (call, normalized result);
  - soft warning on the tool result at repeat 3;
  - a last warning at hard 1;
  - at hard 2, *not* "ask": stop the tool loop and run the forced summary, with `stop_reason=loop_guard` and the repeated call named (`describeRepeatedCall`, `loop-detection.ts:221`).
  - The fork's hard limit exists for *write-class* tools; for read-only lanes repeat-3 cycles are "sometimes the job". So our hard stop keys on the **dedup stub being hit**: the model re-sent an identical call after being told.
- **Profile:** **always**, with one fixed setting in both profiles: soft warning at 2 identical calls (the fork's `softThreshold`), cycle warning at 3, and a stop on the 2nd stub hit after the last warning. The dedup stub and A→B→A detection are on for every role and every backend. The C5 fix (key on content hash, clear on compaction) is a prerequisite.

### 2.10 Consecutive-mistake limit

- **Detects:** `maxConsecutiveMistakes = 6` consecutive `api_error | invalid_tool_call | tool_execution_failed` records. Any productive call resets it (`session-runtime-orchestrator.ts:698`; `core/src/runtime/safety/mistake-tracker.ts:96-170`).
- **Trip:** same as the loop guard's terminal: user (lead) or `awaiting_lead` (worker). With no callback the decision is `stop` (`mistake-tracker.ts:230-238`).
- **Knob:** SDK `execution.maxConsecutiveMistakes`. The UI knob was removed.
- **Scope:** L + W.
- **Ours:** none. Tool errors return as text and the loop continues to the iteration cap.
- **Council:** count consecutive tool errors per lane. At the limit, forced summary with `stop_reason=mistake_limit`, `failure_class=task` (or `infra` when the errors are transport, A2).
- **Profile:** **always**, at 6. It is a stop, not a nudge, so the profile does not move it.

### 2.11 Lead struggle detector → escalation nudge / offer

- **Detects:** thresholds in 05 §1.6 (window 10, failed calls 6, distress 2, min iteration 20, ≤2 per task, edit streak 3, failed transactions 2). Verdicts are `nudge` (one failure short) and `trigger` (`core/src/runtime/safety/struggle-detector.ts:660-830`).
- **Trip:** a suggestion held and attached to the next tool result (`core/src/runtime/escalation/struggle-offer.ts:203-248`).
  - The nudge names `spawn_agent` when delegation can run in parallel, and names the expert on an edit streak or dense code (`:58-140`).
  - The offer names `escalate` and the remaining budget, and says "It is your call" (`:37-53`).
  - The model decides. The user is involved only when `requireApproval` is on (§2.13).
  - It runs only when an expert or delegation exists (`local-runtime-host.ts:2120-2134`).
- **Knob:** `escalationSettings.struggle*`.
- **Scope:** **L only** (workers have §2.12).
- **Ours:** nothing per lane. xauto escalates topology (`consultants/engine/escalation.py:250`).
- **Council:** **DROP as a lane guard.**
  - The edit-streak and transaction signals need edits and the atomic protocol (excluded).
  - Our lanes don't reach iteration 20.
  - The lane analogue is §2.12.
  - The hedging-decay signal is kept as a *candidate* confidence input for xauto (05 #23), not a guard.

### 2.12 Worker struggle supervisor

- **Detects:** phases `watching → nudged → stopped` (`core/src/runtime/safety/worker-struggle.ts:238-384`).
  - **Nudge** on any of:
    - ≥3 budget-exhausted reasoning turns in the last 6;
    - a struggle-detector verdict at the worker floor `WORKER_STRUGGLE_MIN_ITERATION = 8`;
    - non-progress at iteration ≥ `WORKER_NUDGE_AFTER_ITERATIONS = 20` (`:64-79`).
  - **Stop** only on ≥2 more budget-exhausted turns after a 2-iteration grace.
- **Trip:**
  - The nudge is worker-shaped: "Commit what you have now … as a SUMMARY" (`describeWorkerNudge`, `:215-236`).
  - The stop aborts with "Stopped by the struggle supervisor (…)" (`core/src/runtime/safety/worker-struggle-stop.ts`) → `awaiting_lead`, struggling. **The lead decides.**
- **Knob:** none in the UI; on for every delegated agent.
- **Scope:** **W only.**
- **Ours:** none.
- **Council (D2):** nudge and stop as in the fork. The stop becomes the forced-summary path rather than a suspension, with `stop_reason=struggle`, and the reasoning tail is recovered as the fork's `digestOf` does.
- **Profile:** **always**, with the fork's thresholds:
  - budget turns 3/6 → nudge;
  - +2 after a grace of 2 → stop.

  The thinking-budget loop it stops on is a loop. The one adaptation is the non-progress nudge, at `max(3, ⌈0.75 × researcher_loop_iters⌉)`, because the fork's 20 is beyond every lane cap except max.

### 2.13 Escalation (expert model): `escalate` tool, forced escalation, approval, expert guards

- **Detects:** no detector of its own. It is armed by §2.11, and the forced variant by the three terminal guards (mistake limit, loop guard, reasoning-loop streak; `local-runtime-host.ts:2382-2410`).
- **Trip:**
  - **`escalate` tool:** model-chosen.
  - **Forced escalation:** the guard stands down for exactly one turn, in which the model must call `escalate`, or the run ends (`core/src/runtime/escalation/forced-escalation.ts:96-141`). Once per task.
  - **`requireApproval`:** asks **the user** with the brief, Approve/Decline (`apps/vscode/src/sdk/escalation-approval.ts:34-70`; `escalation-session.ts:737`).
  - **Budgets:** `maxEscalations` 3, `maxFollowUps` 20 (`escalation-controller.ts:96-110`). A failed hand-over is refunded.
  - **Expert guards:** loop + struggle detectors run on the expert, but only as *notes to the base model*, which decides (`core/src/runtime/escalation/expert-guards.ts:1-139`).
  - **Stand-down:** the base is write-blocked while the expert works (`stand-down.ts:49-58`).
- **Knob:** `escalationModelEnabled` (false), `escalationSettings.*`.
- **Scope:** **L only.**
- **Ours:** none per lane.
- **Council ("later", plan §4):** keep the *ladder*, drop the *dialogue*.
  - **No `escalate` tool and no forced "one turn to call it".** The harness decides: a lane that reaches a terminal stop (reasoning_loop, loop_guard, mistake_limit, struggle) or a tombstone reruns its plan item **once** on `roles.researcher.escalation_model` with a brief. Budget ≤2 per council, refunded on infra failure.
  - **`requireApproval`: DROP.** There is no one to approve mid-run. Cost control is the static config, which is a pre-run decision.
  - **Expert guards: DROP** as a separate mechanism. The expert lane is just a lane and runs the same guards with the same terminal action.
  - **Stand-down, alternate-with-base, relay-nothing, follow-ups: DROP.** Lanes are read-only and the expert replaces the lane rather than working beside it.
- **Profile:** not a profile matter. It is off until an `escalation_model` is configured, then armed on terminal stops only.

### 2.14 Edit verification (unchecked edits completion guard)

- **Detects:** files touched by edit tools (`editor`, `apply_patch`) and not since touched by a check tool (`check_file`) when the run tries to finish (`core/src/extensions/tools/edit-verification.ts:1-225`).
- **Trip:** holds the run back with a nudge naming the files: 2 times in `nudge` mode, 4 in `require` (`local-runtime-host.ts:1303`; `EDIT_VERIFICATION_ATTEMPTS = 2`, `edit-verification.ts:42`). Then it lets the run end: "the work is the user's to judge" (`:35-37`). There is no ask, but the residual is left to the user. "a host that names no checker gets no guard" (`:23-26`).
- **Knob:** `editVerificationSettings.mode` (`nudge`).
- **Scope:** L only.
- **Ours:** the coder has a single sandboxed `write_file` tool and no reader or checker (`coder.py:10-13`, `:178`, `:382-400`). Lanes are read-only.
- **Council:** **DROP for now**, by the fork's own rule: no checker, no guard. Revisit with Milestone E (shell). When a coder can run a check command:
  - standard = `nudge` (2);
  - strict = `require` (4);
  - and the residual becomes a coder report line "unchecked: <files>" for the synthesizer, not a user question.
- Related: **check-on-failure** auto-runs the checker on files a failed command names (`core/src/extensions/tools/check-on-failure.ts`, `MAX_CHECKED_FILES = 3`). DROP until E, then port as-is (no human).

### 2.15 Checklist (focus chain) close-out, team obligations, rounds guard, required completion tool

- **Detects:** open checklist items (`task-progress.ts:407`, 2 hold-backs); unfinished team obligations and uncollected background rounds (`runtime-builder.ts:1680-1689`); `requireCompletionTool` (`agent-runtime.ts:1308-1326`).
- **Trip:** reminders before the run may end. No human.
- **Scope:** L only.
- **Council:** **DROP**. The council's structure (graph edges, fan-back) already guarantees that every lane is collected. The plan items are the checklist, and the synthesizer must address every lane, including tombstones (`council.py:483-503`).

### 2.16 Plan-mode command guard

- **Detects:** a `run_commands` line whose leading word is a mutating command (`rm`, `mv`, `cp`, `git add|commit|…`, package-manager installs, redirects, `sed -i`, …) when the plan preset sets `blockFileEditingCommands` (`core/src/extensions/tools/command-guard.ts:1-30`, `:479-521`; extension `command-guard-extension.ts:39-140`).
- **Trip:** a tool error in place of execution, *before* approval ("the user is never asked to approve a … blocked call", `command-guard-extension.ts:12-14`). The message tells the model to put the change in its plan "so it can run after the user approves switching to act mode" (`command-guard.ts:514-520`).
- **Knob:** plan preset.
- **Scope:** L, plan mode.
- **Council:** we have no shell yet, and our own `claude_hooks/process_guard` covers a different threat. For Milestone E, port the blacklist as the **read-only lane** policy (researcher/tool_executor shell = plan mode), with the act-mode sentence removed. Report 04 owns the shell design.
- **Profile:** always (when a shell exists).

### 2.17 Cap warning and the final report turn

- **Detects:** a delegated agent within `capWarningMargin(cap) = min(5, max(1, round(cap × 0.1)))` turns of its cap (`agent-iteration-cap.ts:708-750`).
- **Trip:**
  - a one-time "wrap up" warning;
  - at the cap, `FINAL_REPORT_NOTE`: a tool-less report turn (`:654`), and the agent is suspended `awaiting_lead`;
  - `resume_agent` adds `DEFAULT_RESUME_ITERATIONS = 30` (`:66`), which is the lead's decision.
- **Scope:** W.
- **Ours:** forced summary after the cap (`council.py:2557`).
- **Council (D6):** warn at `max_iterations − margin`, using the fork's margin, which is 1 for 4-iteration lanes. No resume.
- **Profile:** default. Strict may raise the margin to 2, because the warning is a nudge.

### 2.18 Stop and abort paths

| fork path | who acts | council |
|---|---|---|
| User Cancel / Stop ("Stop always wins", harness notes dropped on cancel) | **user** | our `cancel` (control surface); D9 graceful cancel gives each lane one report turn, as `stop_agents` does (05 §1.10) |
| Steer (queued user message mid-run, `STEER_RESUME_REMINDER`, `agent-runtime.ts:300-302`) | **user** | `POST /inject` already exists (M9). It is not a guard. |
| Mistake limit / loop guard terminal → "Send a message to continue" | **user** | forced summary + `stop_reason` (§2.9, §2.10) |
| Reasoning-loop streak → abort / forced escalation | harness / model | §2.1 |
| Worker supervisor stop, loop guard, iteration cap → `awaiting_lead` | **lead LLM** | forced summary; `retry-lanes` (D9) is the only "resume", and it is operator-initiated after the run |
| Forced escalation missed → stop (`describeMissedEscalation`) | harness | DROP (§2.13) |
| Context overflow with nothing to compact → terminal message "Reduce attached content or switch to a model…" (`agent-runtime.ts:471-475`) | **user** | C5: forced compaction + one retry; a second overflow → tombstone `failure_class=task`, `stop_reason=context_overflow` |
| `MaxIterationsReachedError` → `limit: "max_iterations"`, a state, not an error (`agent-runtime.ts:698-703`, `:2296`) | — | already a state for us (empty-final fallback) |
| Turn faults: infra retried with no deadline, Stop wins (05 §1.1) | harness | A2: bounded by the run deadline |
| Agent check: 3 identical failing verdicts end the run (05 §1.15a) | harness | coder, with E |
| Tool approval (`autoApprovalSettings`) | **user** | our `tool_approval.py` parks the lane for `DEFAULT_APPROVAL_TIMEOUT_S = 600` (`consultants/engine/tool_approval.py:84`). This is one of the two human checkpoints the council keeps, so it is out of scope here. |

---

## 3. "Strong coding nudges" mapped onto council roles

**The fork's question:** is a turn with no tool call a model that is finished,
or one that should have acted? A coding lead is nudged regardless (strong). A
chat lead is nudged only on evidence. Workers are never nudged: their silent
turn is the report.

**The council's question is per role**, because for some roles the no-tool turn
*is* the deliverable:

| role | what its no-tool turn is | "strong" meaning under strict | standard behaviour |
|---|---|---|---|
| **researcher lane** | its report | **(a)** `force_first_tool_call=True`: a first turn with no tool call is re-asked once (`runner.py:494-514` already implements it; `council.py:2411` turns it off). **(b)** A lane whose final report arrives with **zero tool calls in the whole loop** is re-asked once to ground it ("your report cites nothing you read; read the files it names, then report"). **(c)** The announced-intent re-ask also fires after tools have been called. | (c) only: re-ask once when the final text ends on a promise (`announcedIntentWithoutActing`, ported verbatim with its completion-claim escape). A lane that called tools and wrote a report is never nudged. |
| **tool_executor** | its PLAN-REPORT | (a) + (b): its plan item *is* a tool call, so a report without one is always wrong | (c) |
| **coder** | its summary after writing | (a), with the forcing aimed at `write_file`: a coder that answers in prose wrote nothing. (b) does not apply. | (c) |
| **planner** | the plan (JSON) | re-ask once on a reply that is announced intent or has no parseable plan. Never force a tool: the plan is legitimately tool-free. | today's fallback |
| **critic / meta-critic / refuter / adversary** | the verdict | **never.** A critic with nothing to verify must be free to answer at once (`council.py:1611-1616` comment). | never |
| **synthesizer** | the answer | **never.** Its own failure modes are covered by §2.1, §2.5 and the fallback chain. | never |

**Bundled with strict.** The fork couples these through the nudge budget (§1). Owner ruling: strict covers nudges only.
- §3 (a)/(b) above;
- the re-ask budgets raised to the fork's values: thinking-only 2 → 3, unparsable 1 → 2;
- the planner's plan-parse re-ask;
- the cap-warning margin raised to 2.

**Not bundled, by the owner ruling.** Every loop detector stays **always on**, outside the profile:
- non-convergence (§2.3);
- repetition (§2.2);
- the reasoning-loop cut (§2.1);
- the loop guard (§2.9).

The fork's nudge-budget coupling switched these off on workers. We deliberately break that coupling.

**Never ported, in either profile:**
- the `ask_question` clause;
- the completion boundary hook (`onCompletionAttempt`, used by the fork for the atomic protocol and the agent check). For the coder it returns with E.

**Rationing (both profiles):**
- at most **one** strong/announced-intent re-ask per lane (the fork's "the second nudge has never once changed an outcome", `agent-runtime.ts:409-425`);
- re-asks don't consume `max_iterations`;
- every re-ask emits a `nudge` event with its trigger (§0.4).

---

## 4. Proposed `[guards]` shape

```toml
[guards]
profile = "standard"          # "standard" | "strict"; default standard. Nudges only.
# Optional per-key overrides of the nudge knobs; a key set here beats the profile.
# strong_nudges = true
# thinking_only_reasks = 3
# unparsable_reasks = 2
# cap_warning_margin = 2
[guards.roles.researcher]     # per-role overrides, same keys
```

- **Scope (owner ruling, §0.1).** The profile holds **nudges only**. No loop guard is a key here, and no profile or override can switch one off.
- **Resolution** follows plan §0.2: shipped profile → user → project → per-role → per-(role, model) → per-request (`--guards strict` on `ask`/`followup`).
- **Configuration:** both the `/consultants config` menu and `claude-consultants config set-guards` set it (rule `feedback_configurable_means_skill_cli`).
- **Default:** `standard`, which leaves strong nudges **off**. That is the opposite of the fork's lead default (true) and matches the fork's workers (no no-tool nudges). Users turn on `strict` for small models.
- **Name collision.** `adversary_strictness = "strict"` already exists (`consultants/config.py:159-161`, `:1931-1941`) and tunes the critic's *judgement*. `[guards] profile` tunes *lane nudges*. Keep them separate, and say so in both help texts.
- **Recording.** The resolved profile goes on `runtime_control` and into `consultancy.json`, and is shown in `show`.
- **Parity:** an M12 cohort-2 test proves `standard` with the new nudges disabled sends byte-identical requests to today (plan §0.3). Loop guards are covered by their own tests, since they cannot be disabled.

**Nudges: what the profile changes.**

| nudge | standard | strict |
|---|---|---|
| strong no-tool nudges (§3 a/b) | off | **on** for researcher, tool_executor, coder (force_first + zero-evidence re-ask) |
| planner plan-parse / announced-intent re-ask | off | on, 1 |
| announced-intent re-ask, lanes (§3 c) | on, 1 per lane | on, 1 per lane |
| thinking-only / call-in-thinking re-ask (§2.5) | 2 | **3** |
| unparsable-call re-ask (§2.6) | 1 | **2** |
| cap warning margin (§2.17) | fork's `min(5, max(1, round(cap × 0.1)))` | **max(2, …)** |
| critic / meta-critic / refuter / adversary / synthesizer | never nudged | never nudged |

**Always on, outside the profile.** The thresholds are fixed. Every trip records the realized sampler.

| guard | setting |
|---|---|
| reasoning-loop cut (§2.1) | fork detectors, 3 trips → forced summary |
| reasoning-repetition nudge (§2.2) | fork thresholds, 1 per lane |
| non-convergence (§2.3) | streak 2, ≥2,000 chars, 1 per lane |
| loop guard: dedup, cycle, A→B→A (§2.9) | soft 2 identical / cycle 3; stop on the 2nd stub hit after the last warning |
| mistake limit (§2.10) | 6 consecutive tool errors |
| worker supervisor (§2.12) | 3/6 → nudge; +2 after grace 2 → stop |
| capped thinking, truncation continuation, fault classes, stall monitor | as today / A2 / C4 |

**Not profile-linked either:**
- per-lane model escalation (§2.13), which is armed by `escalation_model`;
- compaction flow (reduced by default, full per role by opt-in);
- edit verification / check-on-failure (§2.14), which is dropped until E and then gets its own mode knob, as in the fork.

The strict re-ask budgets are the fork's own values. The strong-nudge shape per
role (§3) is new design and needs the D-bench: one small local model (≤ 8B)
under both profiles, reporting grounded-report rate, tombstones, the
stop_reason mix and token cost per lane.

---

## 5. Plan review: where §0.4 and §4 assume a human or copy interactive behaviour

| plan item | issue | fix |
|---|---|---|
| §0.4 "a guard nudges before it kills" | "Kill" in the fork means hand to the user or the lead. For us it must name the terminal action. | Add: "the terminal action is the forced-summary path with `stop_reason` + `failure_class`, or a tombstone. Never a pause or a question." |
| §0.4 "every guard emits a `nudge` event" | Fine, but in the fork the notice feeds a UI the user watches live (`agent-runtime.ts:2112-2130`). Ours is read after the fact. | Make the events queryable per lane in `transcript.db` (they feed the replay fitting), not just SSE. |
| D1 "end the lane after 3 trips" | Copies the abort. The fork's lead path offers escalation instead, and a worker is suspended for the lead. | "After 3 trips, forced summary from the pre-cut transcript, `stop_reason=reasoning_loop`, realized sampler recorded. Escalation candidate." Always on, on every backend (§0.1). |
| D2 "+2 after grace → forced summary" | Correct adaptation. Note that the fork *suspends* (`awaiting_lead`) and we must not. | Also say the non-progress threshold (20) does not fit 4–10-iteration lanes (§2.12). |
| D3 "re-ask a plan-announcing turn, unparsed `<tool_call>` text, a thinking-only turn (≤3)…" | Copies the **lead's** nudge policy. Fork workers get none of the no-tool-call nudges (`delegated-agent.ts:502-511`), because a worker's silent turn is its report. An unscoped D3 would re-ask every lane's final report. | Split D3: thinking-only + tool-call-in-thinking + unparsed text → default; announced-intent → default, 1 per lane; the unconditional "strong" re-ask → strict only, and never for critic/synthesizer (§3). Never port `ASK_QUESTION_NUDGE_CLAUSE`. |
| D4 | No human. Fine. | — |
| D6 "a final tool-less report turn" | Fine (a fork worker behaviour). | — |
| D9 "infra-wait event after 10 min" | Copies the fork's lead nudge, whose purpose is to make the lead **decide** (`stop_agents`, do it yourself; 05 §1.2). Nobody decides mid-council. 05 §1.2 also floated a mailbox note to the requesting session, which would page a human. | Event plus `show` field, informational only. The unattended action is already defined: wait until the run deadline (A2), then tombstone `failure_class=infra`. A mailbox note is opt-in and never awaited. |
| D9 "retry-lanes" | Operator-initiated after the run. That is fine, but it is the fork's `retry_failed`, a lead decision. | Keep it out of the unattended loop. Under xauto, consider an automatic single retry of `infra` tombstones before synthesis instead. |
| D9 "graceful cancel" | User-initiated by definition. Fine. | — |
| D10 cycle-aware loop guard | Does not say what happens at the hard limit. The fork's answer is "ask the user" / `awaiting_lead`. | §2.9: forced summary on the second stub hit after a last warning. |
| D2/D10 vs the mistake limit | The plan has no consecutive-tool-error limit at all (§2.10). | Add it as D14, always on. |
| §0.7 "stricter guards are a toggle" | Owner ruling: loop guards are never in a profile. | Reword §0.7: "`[guards] profile` tunes nudges only; loop guards (D1, D10, repetition, non-convergence, dedup) are always on, on every backend, and every trip records the realized sampler (A5/B4)". Drop any profile↔compaction-flow link (§11 decision 1). |
| "Later: per-lane model escalation" | Fine as harness-driven, but must not import the fork's `escalate` tool, forced one-turn offer, `requireApproval`, or expert-guard notes. All four assume a model or a user deciding. | §2.13. |
| §4 note "candidates: external confidence scorer (Jev)" | No human. Fine. | — |
| `consultants/engine/interrupt_policy.py` (not in the plan) | `review_before_synthesis`, the low-confidence pause and `POST /interrupt` are unwired designs for human interrupts (`interrupt_policy.py:1-30`). | Under §0.7 these are the wrong direction. Record in §9 "not inherited": delete or keep explicitly opt-in, never default. |

---

## 6. Summary table

| guard | fork trip action | user involved? | fork toggle / default | council adaptation or DROP | profile |
|---|---|---|---|---|---|
| Reasoning-loop cut (`reasoning-loop-guard.ts:43`) | cut stream, continue; 3 trips → abort or forced escalation | no | `execution.reasoningLoopDetection`, on | cut in `chat_streamed`, condense, continue; at the limit, forced summary `stop_reason=reasoning_loop` (D1) | always |
| Reasoning-repetition nudge (`reasoning-repetition.ts:50`) | append 1 nudge | no | `execution.reasoningRepetition`, on | 1 nudge per lane | always |
| Non-convergence (`agent-runtime.ts:447-469`) | 1 nudge after 3 silent ≥2k-char turns | no | `noToolCallTurnStreakLimit` 3; lead only | streak 2, comes with strict re-asks | always |
| No-tool-call nudge + **Strong coding nudges** (`:1359-1433`) | nudge 1 (+ announced-intent 1); offers `ask_question` | **yes** (via the `ask_question` clause) | `strongNudgesEnabled` **true**; `maxNoToolCallNudges` 1 lead / **0 workers** | per role (§3): force_first + zero-evidence re-ask for researcher, tool_executor, coder; planner parse re-ask; never critic/synth; drop the `ask_question` clause | announced-intent: standard; strong: **strict** |
| Thinking-only / call-in-thinking re-ask (`:317-360`) | re-ask ≤3 | no | always on | port for every role, outside the iteration cap | standard 2; strict 3 (nudge budget) |
| Unparsable tool call (`:362-406`) | re-ask ≤2, name swallowed key, never repair | no | always on | port (D4) | standard 1; strict 2 (nudge budget) |
| Truncated / empty turn (`:150-166`, `:304`) | retry ≤2, halving cap; empty reminder | no | `maxTruncatedTurnRetries` 2 | keep our continuation; empty reminder into A2 | always |
| Capped-thinking condenser (`capped-thinking.ts`) | condense into a note | no | `cappedThinkingEnabled` **true** | already ported; finish via C4 | always |
| Loop guard (`loop-detection.ts:43-441`) | soft warnings; last warning; stop | **yes** (lead: "send a message to continue"); lead LLM for workers | `execution.loopDetection` on (2/5) | cycle-aware; forced summary `stop_reason=loop_guard`, naming the call (D10) | always |
| Mistake limit (`mistake-tracker.ts`, orchestrator `:698`) | stop | **yes** (lead) / lead LLM (worker) | `maxConsecutiveMistakes` 6, no UI | count consecutive tool errors; forced summary `stop_reason=mistake_limit` (new D14) | always (6) |
| Lead struggle detector (`struggle-detector.ts:660`) | nudge / escalation offer on the next tool result | only with `requireApproval` | `escalationSettings.struggle*` (6/2/10/20/2/3/2) | DROP as a lane guard; hedging decay → xauto candidate | — |
| Worker struggle supervisor (`worker-struggle.ts:64-79`) | SUMMARY nudge; stop → `awaiting_lead` | lead LLM | none (always on, workers) | nudge; stop → forced summary `stop_reason=struggle` (D2) | always |
| Escalation / forced escalation / expert guards (`forced-escalation.ts`, `expert-guards.ts`) | model calls `escalate`; one-turn forced offer; notes to base | **yes** if `requireApproval` | `escalationModelEnabled` **false**; `requireApproval` **false**; max 3 / 20 | harness reruns a stopped lane once on `escalation_model` with a brief, ≤2 per council; DROP the tool, approval, expert notes, stand-down | not profile-linked; off unless `escalation_model` |
| Edit verification (`edit-verification.ts`) | hold back the finish 2× (nudge) / 4× (require), then let the user judge | residual left to user | `editVerificationSettings.mode` **"nudge"** | DROP (no checker); with E: coder nudge/require, residual as a report line | own mode knob with E (nudge) |
| Check-on-failure (`check-on-failure.ts`) | auto-run the checker on blamed files | no | always on with a checker | DROP until E, then port | always (with E) |
| Checklist close-out / team / rounds / completion tool | reminders before finish | no | `focusChainSettings` on (2) | DROP; graph fan-back covers it | — |
| Plan-mode command guard (`command-guard.ts`) | tool error before approval; "after the user approves act mode" | indirectly | plan preset | with E: read-only lane shell policy, minus the act-mode sentence | always (with E) |
| Cap warning + final report (`agent-iteration-cap.ts:654`, `:708`) | wrap-up warning; tool-less report; suspend | lead LLM (resume) | always on (workers) | warning + forced summary; no resume (D6) | standard margin; strict margin ≥2 |
| Context overflow terminal (`agent-runtime.ts:471`) | stop, tell the user to reduce input | **yes** | — | C5 forced compaction + 1 retry, then tombstone `context_overflow` | always |
| Turn-fault recovery (05 §1.1) | retry without deadline | no | always on | A2: bounded by the run deadline; `failure_class=infra` | always |
| Infra lead nudge (05 §1.2) | ask the lead to decide after 10 min | lead LLM | always on | informational event only; the deadline decides (D9) | always (informational) |
| User Cancel / steer / tool approval | stop / inject / ask | **yes** | — | `cancel`, `inject`, `tool_approval.py`: our retained human surfaces, not guards | — |
| `interrupt_policy.py` (ours, unwired) | — | would be **yes** | — | not inherited; delete or keep opt-in | — |
