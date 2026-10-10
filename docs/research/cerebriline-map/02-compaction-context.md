# Cerebriline map, slice 02: context management and compaction

Read-only map of the Cline fork "Cerebriline" (`/shared/dev/cline`, `main` @ `400b75535`, 2026-10-10) against the claude-hooks consultants council (`/srv/dev-disk-by-label-opt/dev/claude-hooks`, `dev` @ `52749c5`). Every claim has a `file:line` on both sides. "Not found" means I searched and it isn't there.

Abbreviations:

- `ctx/` = `/shared/dev/cline/sdk/packages/core/src/extensions/context/`
- `ours/` = `/srv/dev-disk-by-label-opt/dev/claude-hooks/`
- `xo/` = `/shared/dev/xollama/`

---

## 0. Corrections to the brief, up front

1. **The fork has no REDUCED / FULL compaction mode, and no cloud vs local/small default.** I searched `ctx/`, `apps/vscode/src`, `apps/cli/src`, the README and the docs. Every model class runs the same pipeline. It has five calls, and every step is on by default:
   - `councilCompactionEnabled` defaults to true (`apps/vscode/src/shared/storage/state-keys.ts:408`).
   - `thinkingCompactionEnabled` defaults to true (`state-keys.ts:407`).
   - Core tests `councilEnabled !== false` and `thinkingSummaryEnabled !== false` (`ctx/agentic-compaction.ts:834-835`).

   The fork has two orthogonal axes:
   - **Tail vs no-tail.** `keepRecentMessages` selects the present-tense *replay* prompt or the *full* state-record prompt (`ctx/compaction.ts:283-296`). It is on by default (`state-keys.ts:401`).
   - **Transcript-as-text vs continuation.** Hosted APIs (cloud) use the text path. `opencoti`, `ollama`, `xollama`, `lmstudio` and `openai-compatible` use the continuation path (`ctx/continuation-compaction.ts:86-92, 598-627`). This is the only cloud vs local split. It changes *how* the calls run (prefix reuse, P/P′ pools, serial or concurrent critics), not *which* calls run.
2. **The "reduced" one-call fold is xOllama's, not Cerebriline's.** xOllama ported the 5-call fold, then made the writer the only default pass. Review and retrospective are opt-in (`xo/server/council_compaction.go:125-131`; `xo/STATE_SUMMARY.md:784`; `xo/plans/agentic-council-chat.md:2376-2377`).
   - Cerebriline's equivalent is `councilEnabled:false, thinkingSummaryEnabled:false`.
   - Not found in either repo: a 3-step "replay writer → retrospective → single reviewer/synthesizer" mode.
   - Cerebriline's council always has two half-critics plus one synthesizer, or it is skipped.
3. **An xOllama council model gets no client compaction at all.** The settings are `enabled: useAutoCondense && !councilModel` (`apps/vscode/src/sdk/cline-session-factory.ts:2661`) and `cappedThinkingEnabled && !councilModel` (`:2705`). The server folds instead (`:2352-2362`).

---

## 1. Agentic compaction: the pipeline, step by step

Entry: `createContextCompactionPrepareTurn` (`ctx/compaction.ts:726-2055`). The agent runtime calls it as `prepareTurn` **before every model request**, including inside a long tool loop (`ctx/compaction.ts:651-653`). So the fork has no separate "mid-turn compaction" mechanism: every request boundary is a compaction point.

### 1.1 Order of operations (default: tail kept, council on, retrospective on)

| # | Step | Where | Notes |
|---|---|---|---|
| 0 | KV-pressure turn, granted window, tail decision, trigger | `compaction.ts:785-1198` | §4 |
| 1 | Cut plan | `compaction-shared.ts:1470-1534` (`findCutPlan`) or `:1559-1571` (`planFullCut`) | §1.4 |
| 2 | Writer (summary or replay), up to 3 attempts | `agentic-compaction.ts:856-994` | §5.4 |
| 3 | Echo cut, then `trimReplayOverflow` (tail only), then `ensureFilesSection` | `agentic-compaction.ts:912-938, 1017` | §5.5 |
| 4 | Retrospective | `agentic-compaction.ts:321-422, 1055-1091` | text path: before the council (`:1116`); continuation path: beside the critics on P′ when room, else after the release (`:1373-1427`) |
| 5 | Critic, first half ∥ critic, second half | `council-compaction.ts:661-738` | parallel or serial, §1.3 |
| 6 | Synthesizer (joins the halves, revises the retrospective) | `council-compaction.ts:773-850` | |
| 7 | Splice `[#N]` citations into the reviewed text | `agentic-compaction.ts:1133-1147` | after the council, so the critics move 4-token citations rather than 400-char entries |
| 8 | Build the summary message, pinned prompt, tail | `agentic-compaction.ts:1168-1194` | §5.9 |
| 9 | No-tail rescue, if the result is still over the trigger | `compaction.ts:1557-1630` | §3.3 |
| 10 | Journal the pre-image, release revisions, repoint the pool, KV shrink | `compaction.ts:1873-1922` | |

Planned call count: `1 + (retrospective?1:0) + (council?3:0)` (`agentic-compaction.ts:428-448`). A retry grows the total (`:466-477`). The progress notice is `(step/stepTotal stepLabel)` (`:842-854`).

### 1.2 The same model runs every step (where this is enforced)

- `resolveSummarizerConfig` returns the **active provider config** when no `compaction.summarizer` is set (`compaction-shared.ts:2192-2208`).
  - Every summarizer call is forced to `thinking:false, auxiliary:true`, and `maxOutputTokens` is set to the ladder cap (`:2175-2191`).
  - The caller's cap outranks the user's per-turn cap (`:2194-2198`).
- No host sets `compaction.summarizer`:
  - grep over `apps/vscode/src` and `apps/cli/src` finds none.
  - `local-runtime-host.ts:1073` only passes it through.
- The retrospective, critics and synthesizer all reuse `summarizerProviderConfig` or the same resolver (`agentic-compaction.ts:369-374, 1297-1303, 1463-1469`).
- The capped-thinking condenser does too (`capped-thinking.ts:731-735, 924-928`).
- **The continuation path refuses a separate summarizer outright:** "a summarizer model of its own is configured; it cannot continue this session" (`continuation-compaction.ts:601-606`).
- Reasoning settings differ by call type:
  - The writer and critics as continuations use **the session's own reasoning settings**, so the prefix matches (`continuation-compaction.ts:897, 1132`).
  - The retrospective and synthesizer as fresh calls use the **summarizer's** settings, which means `thinking:false` (`:1347, 1271`). Commit `f53b7ab2e`: with the model default left on, a thinking synthesizer "spent its output budget reasoning and returned no replay".

**Ours:** `retrospective.write(chat_client=chat_client, model=model, …)` is called with the role's own client and model (`ours/consultants/engine/council.py:1106-1109`), with `think: False` (`ours/consultants/engine/retrospective.py:520`). The same-model rule already holds.

### 1.3 Serial vs parallel, session reuse, PolyKV

**Text path.**
- Critics run `Promise.all` (parallel) unless `summarizerProviderConfig.engineSessionId` is set. Then they run serially, "two concurrent reviewers are two concurrent requests against a window the other agents are drawing on" (`agentic-compaction.ts:1292`; `council-compaction.ts:597-604, 736-738`).
- The parallel critics both correct the **original**. They are never chained (`council-compaction.ts:30-32`).
- A text-path summarizer strips `polykvWorker` and sets `polykvLeadPool:false` but **keeps the engine session**, "the call runs on the agent's admission, not a new one" (`agentic-compaction.ts:242-251`).

**Continuation path** (`continuation-compaction.ts:1-43` is the design):
1. Freeze the session as pool P (`:786-806`).
2. The writer continues the session in its own slot, with `tool_choice:"none"` (`:850-915`).
   - With no booking held, the writer gets an exact booking: `ceil(requestTokens×1.1)+instr/3+maxTokens+512`, aligned to 256 (`:861-886`).
3. P′ = fork(P, `branch_pos = writer cache_n`) (`:921-958`).
4. The critic plan (`:959-1034`):
   - Owner with room ≥ 2× need: concurrent workers. Room ≥ 1× need: serial. Less: skip.
   - Unowned P′ with private windows: concurrent, on exact bookings.
   - No pools: serial in the session's slot, skipped when `requestTokens + instr + summary + criticNeed > window`.
   - `criticNeed = (summaryChars×2+6000)/3 + criticMaxTokens` (`:926-927`).
5. Release, leaves first: the critics' sessions, then P′, then P, then the session's cells (`:1397-1411`).
   - The slot is **erased, not closed**, so the conversation keeps its admission (`:1459-1476`). Measured at 388 re-admission refusals before this.
6. The retrospective and synthesizer continue P′ when it has room (`pool_continue_v1`, `:1163-1206`). Otherwise they run on an exact booking after the release (`:1292-1360`).

Refusals:
- The writer waits without bound.
- Critics, the retrospective and the synthesizer wait at most `OPTIONAL_CALL_REFUSAL_WAITS = 3` times, on backoff `[2,4,8,15,30] s` (`:114-126, 808-840`).
- Only infra faults are retried (`isInfraFault`, `:582-587`).

Fallbacks:
- A continuation that throws or writes nothing falls back to the text path (`compaction.ts:1451-1520`).
- One log line per compaction: path, reason, prefill, `cache_n` (writer, critics, siblings), peak booked cells, wall time, released/LEAKED (`continuation-compaction.ts:1509-1538`).

**Relevance to the council:** low for the pooling itself. We run Ollama cloud or local over HTTP and have no pool tree. Two things do carry over:
- The principle "continue the same conversation as the writer, so a prompt-caching server only prefills the instruction". Ollama's prompt cache would honour it. Our summariser instead builds a fresh `[system, user=serialized transcript]` request (`ours/consultants/engine/retrospective.py:515-522`).
- Serial critics when the lane shares a window with siblings.

### 1.4 Cut planning (tail mode)

`findCutPlan` (`compaction-shared.ts:1470-1534`):

**Recency walk** (`:1349-1373`). Walk back from the newest message:
- Stop when `total ≥ tokenCeiling`, **or** when `total ≥ tokenFloor` and `kept ≥ ceil(len × messagesRatio)`.

Bounds (`resolveRecencyBounds`, `:1308-1336`):
- `tokenFloor = min(preserveRecent, target)`.
- `messagesRatio = 0.25` (`DEFAULT_PRESERVE_RECENT_MESSAGES_RATIO`, `:1268`).
- `tokenCeiling = messageTargetTokens`.
- `lastTurnCeiling = (maxInput − overhead) × 0.66` (`compaction.ts:210, 358-362`).

`preserveRecent` (`:65-97`):
- `20000 × (window/128k)^(2/3)`, so 20k at 128k and about 79k at 1M.
- Capped at `0.6 × target` (`PRESERVE_RECENT_TARGET_SHARE`, `:54`).

**Clamp to the latest typed prompt**, so that whole turn survives verbatim. Exceptions:
- If the last turn alone exceeds `lastTurnCeiling`, use the **pinned cut**: the prompt survives verbatim and the tool loop under it is folded (`:1500-1518`).
- If the clamp folds nothing (the `[summary, prompt, …loop]` shape), also pin (`:1528-1533`).

**Pinned cut** (`:1590-1642`):
- Fold at least `PINNED_PROMPT_FOLD_RATIO = 0.5` of what follows the prompt (`:1251`), but never keep less than the floor, and never more than the ceiling.
- `pinnedIndex === 0` is allowed. That is the fix for the "one prompt + long loop" shape (`:1573-1589`).
- A summary is never pinned (`:1605-1607`).

**Safe boundaries:** only an assistant message or a typed user turn, never a tool-result-only user message (`:1237-1239`, `walkBackToSafeBoundary` `:1375-1385`).

**No-tail plan:** `cutIndex = len`, and the last typed request is pinned (`:1559-1571`).

**Ours:** `compact_messages` (`ours/consultants/engine/budget.py:388-501`):
- Keeps: every leading system message, the **first** non-system message (the question), a marker, and a tail sized by `preserve_recent_tokens` (ported, `:186-202`).
- A minimum of `keep_recent=4` messages.
- Drops leading `tool` messages from the tail (orphan trim, `:445-451`).

Not ported: the messages-ratio floor, the last-turn ceiling, the pinned latest prompt (a follow-up question in the middle is folded away), and the safe-boundary walk. Ours trims orphans forward instead of walking the cut back, which drops the results but keeps the call side. That is valid, but it loses them.

---

## 2. Thinking condensation (past-turn reasoning)

### 2.1 What happens to reasoning of past turns on the wire

**Reasoning replay is per provider, decided from a measurement** (`/shared/dev/cline/sdk/packages/llms/src/providers/model-facts.ts:510-615`).
- `auto` for unprobed providers (`unprobedReasoningHistory`, `:537-550`):
  - `cerebras`, `ollama`, `xollama`, `opencoti`: `"none"`.
  - Everything else: `"all"`, because hosted wire formats expect it and Anthropic needs signed blocks back.
- A measured probe can flip it, and the operator's per-profile setting wins (commits `f45abdd16`, `6fa6da515`).
- `8baed60ce`: when the chat template drops the field, the prior thinking is **inlined into content** as `<think>`. This is the "goose fallback".
- The estimator must use the same mode, or the budget is wrong by 2.23× (`model-facts.ts:597-615` comment).

Inside compaction serialization:
- The writer sees `[Bot thinking]` truncated to **2,000 chars** per block (`compaction-shared.ts:766-771`).
- The retrospective sees reasoning truncated to **6,000 chars per turn**, paired with outcomes (`:813-862`, `:854`). Tool results are reduced to a verdict (`describeToolOutcome`, `:865-880`):
  - `refused by the loop guard as an unchanged repeat`
  - `refused: the change was already in the file`
  - `failed: <first line ≤200>`
  - `reported failure: …`
  - `applied`
- Each reasoning block carries a cost label: `(N output tokens, M chars of it thinking)` (`:897-907`).

**Ours:** the tool loop pops every `REASONING_KEYS` field from the assistant message before appending it (`ours/claude_hooks/agent_loop/runner.py:548-563`), so replay is always "none". There is no inline fallback and no per-provider probe. The retrospective serializer is a faithful port: 6,000-char cap, verdicts, cost label (`ours/consultants/engine/retrospective.py:92-97, 323-413`).

### 2.2 Capped-thinking condenser (a turn that hit its thinking budget)

`ctx/capped-thinking.ts`. Constants:

| Constant | Value | Line |
|---|---|---|
| `CAP_PROXIMITY` | 0.9 of budget, measured from that turn's output tokens × thinking-char share, else the 2.7 chars/token estimate | `:59`, `:408-425` |
| Budget-message tail window | last `marker.length + 400` chars; generic admission regex in the last 800 | `:68`, `:102-146` |
| `CONDENSED_THINKING_MAX_CHARS` | 12,000 (guard, not target) | `:158` |
| `CONDENSED_THINKING_NOTE_TOKENS` | 2,000; cap = `note×probe/(probe−think)` when a thinking level eats the cap | `:183`, `:207-246` |
| Degenerate note | ≥6 lines with distinct/lines ≤0.6, or 4-word stems ≤0.5; or ≥40 words with distinct <0.35 | `:267-297` |
| Tool outcomes in request | input 1,000 chars, result 2,000 chars | `:380-381` |

Prompts, verbatim by range:
- System: `CAPPED_THINKING_SYSTEM_PROMPT` (`:328-329`).
- User: `DEFAULT_CAPPED_THINKING_PROMPT` (`:331-345`), first person, present tense, no headings.
- Lead-in that goes back into the thinking channel (`:318-319`): "I have already reasoned this far on this problem, up to the point where I ran out of thinking budget. Picking up from here rather than starting again:"

Flow:
- Only the **most recent** capped turn is condensed (`:476-545`). Call-only fragments are stepped over (`:501-511`).
- The note replaces that turn's thinking block **for the next request only** (`:1116-1130`). It is keyed by the reasoning text, so a retry pays once (`:1000, 1054-1079`).
- A **discarded-turn path** (`createCappedThinkingNoteWriter`, `:844-902`) catches turns the agent loop throws away. Those are exactly the turns that end at the budget message.
- A second pass, a retrospective, runs unless the turn was window-bound (`:878-893`).

**Ours:**
- `ours/claude_hooks/capped_thinking.py` ports the constants: `CAP_PROXIMITY 0.9` `:61`, 400 tail `:67`, 12,000 `:74`, 2,000 `:85`.
- It is wired into the tool loop per iteration (`ours/claude_hooks/agent_loop/runner.py:144-197, 557-563`), comparing against our own `num_predict`.
- Not ported: the discarded-turn retrospective, and condensation in `_single_shot`. `_single_shot` continues truncated answers instead (`ours/consultants/engine/council.py:1236-1278`), which is right for an answer.

### 2.3 Related reasoning guards (adjacent)

- `823f20dc6`: a stream-level repetition-cycle cutter on the reasoning channel. It matters for Ollama cloud, which has no thinking budget. Measured 60k chars of checklist flip-flopping (mann1x/cline#69).
- `4c3aa0ec7`: a turn that is thinking-only is asked again, not taken as the answer. 13 of 15 agents ended this way, with tool-call XML stuck inside the thinking. **Ours:** no equivalent found.
- `a16e35478`: tell the model that its own past reasoning is not in front of it, so it does not distrust notices quoting it.

---

## 3. Overflow compaction

### 3.1 Per-tool-result truncation (message builder, before compaction)

`/shared/dev/cline/sdk/packages/core/src/session/services/message-builder.ts`:

| Setting | Value | Line |
|---|---|---|
| `DEFAULT_MAX_TOOL_RESULT_CHARS` | 32,000, middle-truncated | `:38` |
| `DEFAULT_MAX_FILE_CONTENT_CHARS` | 50,000 | `:39` |
| `DEFAULT_MAX_TOTAL_TEXT_BYTES` | 6 MB, a rare valve because it breaks prefix caches | `:44` |
| Assistant text | 200,000 chars | `:45` |
| Repeated tool markup | 12,000 chars | `:46` |

The truncation marker is load-bearing. It names Cline as the author of the gap and says the file is complete: "Read the missing range again if you need it" (`:239-248`).

**Stale-read rewrite:** a superseded read becomes `[outdated - see the latest file content]` (`:215`). Since `c3742772f` it fires only when the transcript is ≥ `0.4×window×4 bytes` (`OUTDATED_REWRITE_PRESSURE_SHARE`, `:94-115`). Then all pending stale reads go at once, because every rewrite breaks the prefix cache. Measured: 834 of 7,465 builds rewrote, and 499k tokens were re-prefilled. The threshold is `2% window`, floor 4 KB, ceiling 64 KB (`:74-78, 122-140`).

Per-profile `maxToolResultChars` (`apps/vscode/src/sdk/cline-session-factory.ts:1986-1997`).

**Ours:**
- Per tool at the source only: `_READ_MAX_BYTES = 48 KiB` (`ours/claude_hooks/caliber_proxy/tools.py:27`); a 2,000-char snippet in the tool_executor render (`ours/consultants/engine/tool_executor.py:656`).
- No history-side middle truncation with an explanatory marker. **No stale-read rewrite.** `ours/consultants/server/tool_cache.py` dedups *calls* but not the copies already in history.

### 3.2 Provider-rejected overflow (once per run)

`generateAssistantMessageWithOverflowRecovery` (`/shared/dev/cline/sdk/packages/agents/src/agent-runtime.ts:2530-2585`):
- On `context_window_exceeded` with no tool calls in the partial (`:2832-2845`), it forces a compaction with `overflowRecovery:true, requireSmallerRequest:true` and retries once.
- A second overflow throws `ContextWindowOverflowError`.

In compaction (`ctx/compaction.ts:1633-1767`):
1. A custom compactor first, held to the bar.
2. **Basic** (deterministic, no LLM) as the floor.
3. Then **one** agentic attempt. It is accepted only if `len>0 && tokens < input && tokens ≤ messageTarget`.
- Basic is kept otherwise, because "the model does not survive it" when basic is the answer, yet basic guarantees an answer.
- The continuation path is refused under overflow recovery (`continuation-compaction.ts:612-617`).

**Ours:**
- Only the proactive check: `budget.plan` marks `context-overflow` when `available < MIN_OUTPUT_TOKENS (4096)` (`ours/consultants/engine/budget.py:325-340`).
- `token_calib.note_context_overflow` is written there, but **`consume_context_overflow` is never called** in production (grep: only `ours/claude_hooks/token_calib.py:213` and `__all__`). It is a dead path.
- No reactive recovery on a provider 400 "context length exceeded" was found in `ours/claude_hooks/get_advice/chat_client.py`.
- On Ollama an overflowing prompt is **silently truncated**, not rejected. Fork commit `f76029582` documents a sub-agent running 34 requests past its window that way.

### 3.3 Keep-tail result still over the trigger: the no-tail rescue

`escalateToNoTailIfStillOversized` (`ctx/compaction.ts:1557-1630`, commit `0fc7744c3`):
- If the keep-tail result is above the **trigger** (not merely above the target), re-run agentic with `keepRecentMessages:false`, using the full prompt and the council too.
- Take the no-tail result only if it is smaller.
- Measured: at 4× overshoot on a 32k window, keep-tail returned 34,297 tokens (over the window) and no-tail 17,217.

**Ours:** not found. `_plan_and_compact` re-plans once after compacting (`ours/consultants/engine/council.py:1121-1123`) and sends whatever results.

### 3.4 Decline handling and the basic fallback

- Agentic throws: basic (`compaction.ts:1777-1793`).
- Agentic declines: basic, **unless the tail is disabled**. Then it declines honestly (`:1814-1838`).
- Basic compaction (`ctx/basic-compaction.ts:453+`) prunes tool results and keeps the 3 latest assistant texts verbatim inside dropped-work summaries (`:62`).
- **Ours:** a failed digest falls back to the bare elision marker (`ours/consultants/engine/budget.py:477-488`). That is the equivalent of "basic".

### 3.5 Starved output cap (the trigger fires after the cap goes thin)

`compaction.ts:1124-1175` (commit `247e72ce3`):
- Fires if the last request's cap was **window-bound** and `< outputRoomTokens`, and its estimate is not contradicted by the provider count (`estimate > observed×1.25` means ignore, `OUTPUT_CAP_ESTIMATE_TOLERANCE`, `:687`).
- Latched per session, so it fires once per descent (`:689, 1168-1175`).
- **Ours:** `token_calib.note_output_cap` is written on every plan (`ours/consultants/engine/council.py:1124`), but **`last_output_cap` is never read**. Not wired.

---

## 4. Trigger logic

### 4.1 Fork thresholds

| Quantity | Value | Where |
|---|---|---|
| Effective max input when only a window is known | `0.9 × window` | `compaction-shared.ts:24, 568-587` |
| Ratio trigger | `0.9 × maxInput` | `:26, 474` |
| Room trigger | `max(window − outputRoom, 0.5 × window)` | `:271, 479-483` |
| Trigger used | `min(ratio, room)` | `:483` |
| Output room, cold | `min(declared, 0.25×window)` | `:286, 461-464` |
| Output room, observed | `min(declared, 0.5×window, max(8000, 1.5×observed))` | `:313-336, 452-460` |
| Observed output | 12 newest assistant turns, measured **from the transcript as sent**, not from the bill; newest 3 at full size, older outlier rule (2nd largest when ≥4 samples) | `:297-304, 377-412` (`9b53fc656`) |
| Content target (auto) | `0.25 × (maxInput − overhead)`, kept `< trigger` | `compaction.ts:539-556, 1305-1319` |
| Manual target | `ratio` (default 0.5, clamped 0.05–0.95) × message tokens, ≤ trigger | `compaction.ts:500-520` |
| Prefer provider count | `lastObservedRequestTokens(measurementsKey)`, per **conversation**; foreign counts excluded (`a8d4ae171`, mann1x/cline#68: 302 bad vetoes) | `compaction.ts:1061-1068` |
| Calibration on resume | seeded from the transcript's last `metrics.inputTokens` | `compaction-shared.ts:2394-2425`, `compaction.ts:1054-1060` |
| Retried turn ≠ bigger context | summed retries excluded from calibration and trigger | commit `9d7cf0b6f` |
| PolyKV pressure | engine pool pressure ≥ `0.85` (configurable) | `polykv-session.ts:68, 611-630` |
| Global KV pressure | wants compaction only if `request ≥ 1.25×compacted` and the shrink gains `≥ 0.25×window`; never below the floor; a lead only for its own agents | `kv-pressure.ts:96-103, 584-663` |
| KV shrink/grow | shrink fill 0.6, grow at 0.75, align 256, min shrink 4096 cells or 10% | `kv-pressure.ts:70-87` |
| Granted window | size against the negotiated window when smaller | `compaction.ts:703-724, 797-804`; `polykv-session.ts:644` |
| `shouldCompact` | `overflow ∥ observed≥trigger ∥ polykv ∥ starvedCap ∥ kvPressure` | `compaction.ts:1193-1198` |
| Cause label | manual > overflow > auto (own threshold or starved cap) > pressure | `compaction-cause.ts:24-45` |

### 4.2 Compaction-count effects

**Output budget ladder by generation:** `[0.33, 0.40, 0.45, 0.50, 0.55]` of the content target (`compaction-shared.ts:185`).
- The summary takes 70% (`:188`).
- The retrospective takes `max(0.2×C, min(0.5×C, C − summaryTokens))` (`:199-200, 249-259`).
- Floors: `DEFAULT_SUMMARY_MAX_OUTPUT_TOKENS = 4096` for both combined and summary (`:99, 226-239`).
- With no target, the fallback is `clamp(1024, 0.08×maxInput, 8192)` (`:110-169`).
- The generation is carried in metadata (`:506`). A pre-ladder summary reads as generation 1 (`:1035-1044`).

**Fix rate by compaction count** (335 runs, commit `80f4289c8`, recorded in `compaction-shared.ts:942-969`):

| Compactions | Runs | FIXED |
|---|---|---|
| 0 | 218 | 85 % |
| 1 | 41 | 63 % |
| 2 | 24 | 50 % |
| 3 | 8 | 25 % |
| 4+ | 44 | 25 % |

**FORCE_FULL revert.** `FORCE_FULL_FROM_COMPACTION` was 2: drop the tail from the 2nd compaction onward. It is now **0, off** (`compaction-shared.ts:970`, reverted in `c9c3ffb41`). The reasons are recorded at `:942-969`:
- The first compaction became weak by construction: median −30%, leaving 20 messages, against −65% leaving 4.
- The v6 A/B ran `--force-full-from-compaction 2` in *both* arms, so it measured nothing.
- "Whatever the right tail policy is, it has to be one policy for a whole run before any arm can measure it."

The CLI flag survives (`bdd20625d`; `apps/cli/src/utils/compaction-mode.ts:33-54`). The setting is `forceFullFromCompaction`, default 0 (`state-keys.ts:405`).

### 4.3 Stall and time budgets for compaction calls

- The fork has **no wall-clock timeout** on compaction calls (grep over `ctx/*compaction*.ts`, `capped-thinking.ts`: none). They inherit the provider/abort signal.
- Bounded refusal waits only: 3 waits, backoff 2/4/8/15/30 s (`continuation-compaction.ts:124-126`). The writer is unbounded.
- `44d942ece` (2026-10-10): a capped-thinking note or retrospective on xOllama is sent as a side call (`sideCallOf`) with its own exact booking. That avoids a 300 s wait behind its own idle conversation's booking. Measured: 604 refusals and 306–336 s pauses before the fix.
- xOllama's member wrapper does have stall budgets: 5 min with no output, 20 min total, 2 retries at 2 s then 4 s (`xo/plans/agentic-council-chat.md:2378-2382`).

### 4.4 Ours

| Quantity | Ours | Fork | Cite (ours) |
|---|---|---|---|
| Ratio trigger | **0.8** | 0.9 | `budget.py:131` |
| Min output room | **2,048** | 8,000 | `budget.py:104` |
| Observed output | billed `_output_tokens` / `metrics.output_tokens`, plain max, no recent/outlier rule | transcript-measured, recent-3 + 2nd-largest | `budget.py:159-183` |
| Compaction target | **`keep = plan.trigger_tokens`** (the target *is* the trigger) | 25 % of free content | `council.py:1092-1094` |
| Ladder base | `retrospective.write(target_tokens=keep)`, so the combined budget is 0.33 × *trigger* (about 3.6× the fork's at the same window) | 0.33 × 25 % content | `council.py:1106-1109`, `retrospective.py:139-151` |
| Min phase output | 512 | 4,096 | `retrospective.py:90` |
| Retrospective floor | **none, deliberate policy** (the summary is load-bearing) | 0.2 × combined | `retrospective.py:74-87, 154-169` |
| Calibration key | per **model** | per conversation/engine session | `token_calib.py:114` vs `compaction.ts:762` |
| Window | `/api/show` model max (`context_length_of`) | granted/negotiated window | `budget.py:267-285` |

In fan-out lanes several lanes share one model's calibration bucket. That is exactly the cross-conversation contamination `a8d4ae171` and `4adbd6182` fixed in the fork.

---

## 5. The detail rules, one by one

### 5.1 Tool ledger and `[#N]` citations

All in `ctx/tool-ledger.ts`.
- **Build** (`:464-527`): pair calls with results by id, never by position. Read both block shapes (`:413-454`).
- **Collapse** identical `(tool, input, result)` repeats into `N×`. A changed result is kept, because it is the informative pair (`:501-513`).
- **Limits** (`:97-102`): field 200 chars, input 400, result 300. A ≥4-line string is reported as `<N lines, M chars>`.
- **Lists:** element names are listed, not counted (`:202-229`, `0e4f7b2eb`).
- **Structured `{query,result,success,error}`** outputs render as such (`:288-319`, `927143061`). Before this, 50 of 99 results were blank and a checker's `ok:false` read as success.
- **Refusal detection:** `"ok": false`, `"error": "`, or a leading `error|refused|failed` (`:350-363`).
- **Carry across generations:** merge previous + new, collapse at the seam, renumber (`:552-575`).
- **Evict** to budget, oldest first within each tier (`:595-620`):
  1. successful non-verdict calls;
  2. failed non-verdict calls;
  3. verdict tools `run_commands` and `check_file` (`:540`).
- **Budget:** `max(2000, target×3 chars×0.10)` (`agentic-compaction.ts:171, 180, 728-744`).
- **The writer gets the key only:** `"N. tool  input"`, without results (`:657-663`). It is asked to cite `[#N]` or `[#a-b]`.
- **Splice after the council** (`:676, 710-769`, `c50a75880`, `762112530`):
  - Each cited entry is rendered inline.
  - Unknown, duplicate, backwards or over-wide citations are dropped and counted as `invalid`.
  - Uncited entries are appended after the prose (`agentic-compaction.ts:1176-1183`).
- **Measured:** tool transcription took 69% of a small model's output. 11,420 chars of replay became 4,875. Invalid citations went from `[#31-33]` against 30 entries to none.
- The ledger is switched off with Checkpoints, because file-revision spans are `restore_file` addresses (`agentic-compaction.ts:1041-1047`; spans per file, `:796-818`).

**Ours:** no ledger. The closest artifact is `ours/consultants/engine/citation_linter.py:253,392` (`verify_citation`, `lint_answer`), which verifies `path:line` claims in an *answer* against the code graph.
- It is the same idea as the splice's `invalid[]`, an origin-side check that prose did not run past the record.
- It does not check against a tool record.
- `ours/consultants/server/tool_cache.py` holds the raw material: per-run `(tool,args)→result`.

### 5.2 Verbatim user requests (the 15% share)

- **Collect:** turn-start user messages only (`compaction-shared.ts:1154-1169`). Consecutive duplicates collapse. Merged with the previous summary's `userRequests` by append (`agentic-compaction.ts:190-202, 583, 761-764`).
- **Budget:** `USER_REQUEST_BUDGET_SHARE = 0.15` (`agentic-compaction.ts:169`).
  - Of the summarizer input in the *request*: `summarizerInputLimit×3×0.15` (`:783-785`).
  - Of the message target in the *stored message*: `target×3×0.15` (`:1186-1190`).
- **Per request:** `clamp(200, budget/n, 2000)`. A request is **never dropped**, only middle-elided, with the note `… N characters elided by the harness …` (`compaction-shared.ts:1103-1152`).
- **Defaults:** 6,000 total when unnamed (`:1108`).
- **Pinned prompt:** shown to the summarizer but not stored, because it survives verbatim below (`agentic-compaction.ts:584-605`). Measured failure: "The exact instructions are missing from my current view".
- **Request block text** (`compaction-shared.ts:2071`): "What the user asked, in their own words. The harness quoted these; they are not always present in the transcript, and they are what you must reproduce verbatim rather than describe:", followed by `<user_request>` blocks.
- **Stored block text** (`:2297`): "What the user asked, in their own words — every instruction given so far, quoted by the harness:".

**Ours:** not found. Only the first question survives as the head (`ours/consultants/engine/budget.py:429-431`). Follow-up questions in a carried thread (`ours/consultants/server/runner.py:746-754`; `ours/consultants/engine/council.py:2069-2071`) that fall into the middle are summarized, not quoted.

### 5.3 Prompts: writer, retrospective, council

| Prompt | Fork location | Ours |
|---|---|---|
| Writer system, tail ("re-telling your own recent work in your own voice…") | `agentic-compaction.ts:128` | not found; ours: "You are writing a hand-over note…" `retrospective.py:263-266` |
| Writer system, full ("You write the state record…") | `agentic-compaction.ts:129` | not found |
| Retrospective system ("assessing your own reasoning… judgement, not a record… Be terse") | `agentic-compaction.ts:133-134` | different text: `retrospective.py:268-273` (copied from the capped-thinking retrospective system prompt, `capped-thinking.ts:305-306`) |
| **Replay** user prompt (tail mode, present continuous, cite `[#N]`, closed record) | `replay-compaction.ts:59-100` | not found |
| **Full state-record** prompt (9 required sections incl. Standing instructions, Working set, Retrospective; `(none)` for empty; "Do not call any tool") | `full-compaction.ts:43-93` | not found |
| Legacy hand-over note (Goal/Done/In progress/Ruled out/Key facts/Next/Files) | `compaction-shared.ts:1951-1982` | ported, investigative wording: `retrospective.py:176-216` |
| Retrospective user prompt | `compaction-shared.ts:2110-2132` | verbatim: `retrospective.py:225-261` |
| Previous-retrospective carry ("Carry forward what still holds…") | `compaction-shared.ts:2143-2147` | verbatim: `retrospective.py:470-474` |
| Council writer marker (`<<<HALFWAY>>>`) | `council-compaction.ts:59-72`, inserted before "Write the replay and stop." (`:142-156`) | not found |
| Critic system / user | `council-compaction.ts:43-44` / `:81-107` | not found |
| Synthesizer system / user | `council-compaction.ts:45-46` / `:116-124` + retrospective paragraph `:379` + section format `:381-392` | not found |
| Overrun retry text | `agentic-compaction.ts:224-232` | not found |
| Continuation span text ("…up to, and not including, the message that begins: «…»") | `agentic-compaction.ts:1230-1254` | n/a |

Prompt overrides can come from a prompt template's `# compaction: <id>` section, with ids `replay`, `full`, `retrospective`, `council-writer`, `council-critic`, `council-synthesizer`. A template outranks the Features setting, which outranks the built-in (`apps/vscode/src/sdk/cline-session-factory.ts:1965-1980`; `sdk/packages/core/src/extensions/config/prompt-template-compaction.ts`; commits `347e52ad7`, `d6c0e2d05`, `d64f44b50`).

**Present-tense rationale** (`replay-compaction.ts:1-56`):
- A tail-kept summary sits in front of the model's own turns. A third-person past-tense note "changes voice mid-stream".
- A past-tense replay "reads as history… you will re-read files you already know, re-derive what you have already settled, and treat a warning that is still in force as something that merely once happened". This was measured on a 29-minute pandorum run.
- Negative exemplars were removed: "a negative exemplar is still an exemplar".
- Section labels were rewritten in the tense they ask for.

Only the tail mode uses it. The full mode is a third-person state record, because the summary *is* the context there.

### 5.4 Writer retries: 3 attempts

`SUMMARY_ATTEMPTS = 3` (`agentic-compaction.ts:137-145, 204`). The loop is at `:856-994`.

| Failure | What it means | Next attempt |
|---|---|---|
| `threw` | transport error | resend the same request; abort is not retried (`:890-904`) |
| `empty` | nothing left after the echo cut and trim; `likelyCause = output_budget_consumed_by_reasoning` when `reasoningChars>0` | resend the same request (`:956-974`) |
| `over_budget` | `estimateTokens(text) > summaryMaxTokens` | request + `describeOverrun(measured, limit)` (`:863-869, 975-990`) |

Outcomes:
- An over-budget summary on the last attempt is **kept**. Empty or thrown ones are not, and the compaction declines (`:995-1015`).
- A summary cut by the output cap (`incompleteReason`) is kept with a warning (`:939-954`).

**Ours:** `retrospective._call` makes **one attempt** and returns `("", 0)` on any failure (`ours/consultants/engine/retrospective.py:504-532`). There is no overrun check.

### 5.5 Echo cut and `trimReplayOverflow`

- **`cutEchoedTranscript`** (`replay-compaction.ts:125-148`):
  - Truncates at the first line, outside a fence, that starts with `Conversation:`, `Previous summary:`, `[Bot tool calls]:` or `[Tool result]:`.
  - `[User]:` and `[Bot]:` are deliberately excluded.
  - Measured: 8,925 of 13,846 stored chars were the transcript echoed back.
  - It is applied to the writer, the retrospective (`agentic-compaction.ts:390-395`), each critic and the synthesizer (`:1304, 1461, 1471`).
- **`trimReplayOverflow`** (`replay-compaction.ts:150-207`):
  - Tail mode only (`agentic-compaction.ts:925-938`).
  - A fenced block longer than **1,200 chars** keeps head and tail of `max(80, 600−40)` chars each, with the note "… N characters elided by the harness — this block was not trimmed, and the content is still on disk …".
  - An unterminated fence runs to the end. Prose is never trimmed.

**Ours:** neither. Our summary prompt also ends with `Conversation:` (`ours/consultants/engine/retrospective.py:461-462`), so the echo risk is identical.

### 5.6 Ratio guards

| Guard | Value | Where |
|---|---|---|
| Halfway marker accepted if the first share is in `[0.3, 0.7]`, else rebalanced to the blank line nearest the middle, then any line | `HALF_MARKER_BAND = 0.3` | `council-compaction.ts:176, 189-243` (measured: marker at 19%, one half 919 chars, the other 3,939, which came back byte-identical) |
| Critic told: return within about **±10%** of the half's length | prompt | `council-compaction.ts:105` |
| Synthesizer: aim at the original length, may go to **1.1×** | `Math.round(originalLength*1.1)` | `council-compaction.ts:124, 369` |
| Merge rejected if `< 0.5 ×` the original (keep the original, keep the synthesizer's retrospective if any) | `COUNCIL_MIN_MERGE_RATIO = 0.5` | `council-compaction.ts:489-505, 814-829` |
| Critic splice-back only on a **real** marker (not a rebalance) | — | `council-compaction.ts:696-715`. "Every 'the writer deleted instead of revising' measurement turned out to be" the splice halving a correct answer |
| A critic request over `summarizerInputLimit×3` chars declines that half | — | `council-compaction.ts:673-688` |
| No usable half from either critic: the original, marker stripped | — | `council-compaction.ts:739-745` |
| Overflow-recovery acceptance | `tokens < input && ≤ messageTarget` | `compaction.ts:1683-1686, 1735-1738` |

### 5.7 Council mechanics

- Critics get **their own half to rewrite**, the **other half for reference only**, the **ledger key**, and the **whole transcript**, or a pointer to the context in continuation mode (`council-compaction.ts:268-339`). The halving is of the replay, not of the evidence (`:658-660`).
- The synthesizer sees each half's original beside its rewrite, plus the retrospective (`:352-431`). It is the **only** role that touches the retrospective (`:345-351`).
- Its answer is parsed by `## Replay` / `## Retrospective` headings, tolerant of `#`–`####` (`:452-475`). With no retrospective it may answer with bare prose (`:788-792`).
- Any failure keeps what it was given (`:33-35, 836-850`).
- Log excerpts are head+middle+tail, 1,500 chars (`:486, 516-533`). An unbounded debug log once took a host down.

### 5.8 The retrospective 0.2 floor

`THINKING_BUDGET_MIN_SHARE = 0.2`, `MAX_SHARE = 0.5` (`compaction-shared.ts:190-200, 249-259`).
- `retrospectiveNeed` reserves `reasoning+previous+4000` chars (`agentic-compaction.ts:1316-1335`).
- The retrospective is skipped when there is no reasoning and no previous retrospective (`:347-352`).
- It is skipped when the request exceeds the summarizer input limit (`:358-368`).

**Ours:** no floor, deliberately (`ours/consultants/engine/retrospective.py:74-87`). It is skipped when the remaining budget is `< 512` (`:607-617`). A degenerate retrospective is dropped (`:627-630`).

### 5.9 Where the replay is inserted

`buildSummaryMessage` (`compaction-shared.ts:2239-2370`) returns **`role: "user"`**, with `metadata.displayRole: "system"` for the UI only. Content parts, in order:
1. The quoted user requests (`:2293-2302`).
2. `Retrospective on the work this summary replaces — your own assessment, carried forward:\n\n…\n\n` (`:2313-2328`).
   - As text, not a reasoning block. A reasoning part on a user message died with `AI_TypeValidationError` (`:2304-2312`).
   - The trailing `\n\n` is load-bearing, because Ollama's converter joins parts with nothing (`:2317-2324`).
3. `Context summary:\n\n<summary>` (`:2329-2332`).
4. The uncited ledger entries (`:2337-2344`).

Then the pinned prompt and the tail (`agentic-compaction.ts:1192-1194`). The system prompt is untouched; it is a separate field.

The metadata carries:
- `generation`, `thinkingSummary`, `toolLedger`, `toolLedgerEntries`, `userRequests` (the *shown*, elided ones), `details`.
- `getCompactionSummaryMetadata` must name each field explicitly or it is dropped on rebuild (`:1049-1054`).

**Ours:** `render_marker` returns **`role: "system"`**, inserted *after* the head (system + question) and before the tail (`ours/consultants/engine/retrospective.py:643-677`; `ours/consultants/engine/budget.py:489`). Order: retrospective, summary, elision note. It carries `_compaction_*` keys. A mid-conversation system message is legal for Ollama /api/chat. But it is the "third-person note" seam the replay prompt was written to remove, and some OpenAI-compatible templates fold or reject non-leading system messages.

### 5.10 Journal and undo

`compaction-journal.ts:1-103` keeps the 2 newest pre-images per session, in memory only (`65b74c63a`). It is recorded before revision release (`compaction.ts:1873-1890`). **Ours:** not found. Councils already checkpoint state (`<cwd>/.claude-hooks/consultants/<sid>/checkpoints.db`), which could hold one.

---

## 6. Related commits since 2026-08-12

R = relevance to a council: **H** port, **M** consider, **L** lesson only, **–** none.

| Commit | What it changed | R |
|---|---|---|
| `a8d4ae171` 09-05 | Only the conversation's own request writes the observed token count, cap and overflow note; aux calls are excluded (#68: 302 bad vetoes) | **H**: key our calibration per lane/role |
| `4f90a241b` 09-07 | A compaction asked for mid-turn is queued for the running→idle edge | L |
| `f76029582` 09-10 | Sub-agents had no compaction or thinking cap at all; ran 34 requests past the window on Ollama, which truncated silently | M: every lane path must get `prepare_payload` |
| `749906cfe` 09-16 | Hand-over note made domain-neutral (not code-specific) | L |
| `fd42eb28c` / `3695e0bd3` 09-16 | Tool ledger introduced, placed after the summary | **H** |
| `3c767871e` 09-16 | Replay prompt for tail mode ("a note is the wrong artifact") | **H** |
| `f080e74b7` / `69603f6e9` 09-16 | Full state-record prompt and no-tail cut | M |
| `d6fc36c44` 09-16 | Strategy setting; both prompts kept | L |
| `65b74c63a` 09-16 | Journal, 3 attempts with overrun measurement, never swap strategy | **H** (retries) |
| `0fc7744c3` 09-16 | No-tail rescue when keep-tail stays over the trigger | **H** |
| `f331d6af2` / `198e442ac` / `2459f758e` / `57ee34fe9` 09-16 | File-revision history retention for 2 compactions; spans per file | – (no edits in councils except coder) |
| `80f4289c8` 09-17 | Drop the tail from the 2nd compaction; the 85/63/50/25% data | L (data) |
| `bdd20625d` / `a0fe7daa9` 09-17 | `--force-full-from-compaction` flag and setting | L |
| `f08c8526a` 09-18 | Measure the context; one setting owns the cap | M |
| `55fb603ad` / `2403f40e8` 09-18 | Reasoning charged at the reasoning rate; refuse a provider count above the window | M (we have the reasoning rate) |
| `f45abdd16` / `d3252696a` / `6fa6da515` / `8baed60ce` 09-18 | Reasoning replay decided per measured capability; inline `<think>` fallback | M |
| `247e72ce3` 09-18 | Starved window-bound output cap forces compaction | **H**: we record the cap, nothing reads it |
| `1153b739c` / `3c1d289c0` 09-15/18 | Thinking budget actually sent to llama.cpp/opencoti | – |
| `c61cb3600` 09-12 | A count claiming 1 token/char is not a measurement | M (calibration sanity) |
| `927143061` 09-19 | Structured tool results were blank to the summarizer (50/99) | **H** if we port the ledger (our results are strings, so lower risk) |
| `c9c3ffb41` 09-19 | Content-share target 0.25 of free content; fixed-price breakdown; FORCE_FULL→0 | **H**: our target is the trigger |
| `9d7cf0b6f` 09-20 | Retried turn's summed usage poisoned calibration and trigger | **H**: our `_single_shot` sums continuations into the returned pt/ct; `_observe_usage` is per call, so OK, but check fallbacks |
| `75344700e` 09-20 | Verbatim user requests block (15%), never dropped | **H** (follow-ups) |
| `d2d1311e3` 09-20 | Compaction council introduced | M (cost) |
| `0e4f7b2eb` / `c50a75880` / `762112530` 09-20 | Ledger names args; `[#N]` citations; closed record; ranges | **H** with the ledger |
| `ea9d75e41` / `4d01ec9f4` / `d58739214` 09-20 | Manual compaction reads all settings; guard the invariant | L ("settings that travel with a gated object get dropped") |
| `fc329149e` / `c0625a696` 09-21 | Step counter and duration on the divider | M (observability; our SSE could emit it) |
| `178c27320` 09-21 | Output cap per turn ≤ 96,000 | L (ours 32,768) |
| `347e52ad7` / `d6c0e2d05` / `d64f44b50` 09-23 | Council prompts editable; template `# compaction:` sections | L |
| `c8ef7aff4` 09-25 | Worker struggle supervisor: nudge once, stop only on a thinking-budget loop | M (stall) |
| `a7ad44b07` / `2ee5008e8` / `fe5382002` 09-22/25 | Window negotiation; compaction against the grant | M: our window should be the running `num_ctx` |
| `97389108b` / `287761ee3` / `8814ea767` / `450368d4a` / `f38c37a01` 09-25/26 | KV-pressure compact/shrink/grow | – |
| `07b717e67` / `c0ebb71af` / `9396f6b96` 09-25/29 | Per-agent compaction counters; compaction spend counted as the agent's | M (record compaction calls in transcript.db by role) |
| `4fee8b6bb` / `474626b2a` / `bcce4dce6` 09-26 | Compaction as a continuation of the session | M (prompt-cache reuse on Ollama) |
| `f53b7ab2e` 09-26 | Fresh synth/retro calls use the summarizer's thinking:false | **H** (we already use think=False; keep it) |
| `c85810453` 09-26 | Retro and synth continue P′ when room | – |
| `4adbd6182` 09-26 | Measurements filed under each agent's own engine session | **H** (same as `a8d4ae171`) |
| `c3742772f` 09-26 | Stale reads rewritten only at ≥40% window, all at once | M |
| `9b53fc656` 09-27 | Reply reserve: newest 3 at full size, else 2nd largest; bar marks the trigger | **H** (cheap; our max() is outlier-prone) |
| `5dd6fa7c9` 09-29 | Pooled writer sends its head; text summaries leave the pool | – |
| `413a6b680` 09-28 | Compaction keeps the session running (erase slot, don't close) | – |
| `8de6864e2` 10-04 | Saved compaction survives transcript reshaping (hash mismatch dropped it silently) | **H** lesson: our follow-up threads rebuilt from disk must keep `_compaction_*` markers |
| `4c3aa0ec7` 10-03 | Thinking-only turn asked again | **H** (our roles can end on reasoning-only) |
| `f4bfb8609` 10-01 | Output split into thinking/text/tool-input per request | M (recorder) |
| `44d942ece` 10-10 | Thinking-note side calls get their own exact booking on xOllama | L |
| `823f20dc6` 09-07 | Reasoning repetition-cycle cutter | M (Ollama cloud has no think budget) |
| `a16e35478` 09-21 | Tell the model its reasoning isn't in front of it | L |
| `ff88c806e` 09-23, `bbf00f193`, `e17c436ab`, `a81fba2fa` | Loop guard: identical calls in one message count once; warning wording | L (we dedup in `runner.py:325-340`) |
| `9f42ee459` 10-06 | Memory: auto-recall per message with HyDE "after claude-hooks' pipeline" | – (theirs ports ours) |
| `bc7cedbbd`, `a860dc15e`, `bb2fd57da` 10-06 | Memory notes panel, per-workspace memories, rename | – |
| `d6a7d0b95`, `c8a573081`, `744d25e33`, `ce9088226`, `42ed6d32a`, `5db26e633` 10-06 | Retrieval: embedding/rerank client, chunker, keyword store, LanceDB hybrid, resumable embedding, model-change visibility | L (a hybrid-search design parallel to our pgvector RRF) |
| `94fe9acce`, `f43736860`, `9f1e76cca`, `cb7c09832`, `5fc76c2e8`, `ef1acfdb8`, `018db6ea0`, `e733d6d15`, `4eca2878b` 10-06 | Library: shelves, librarian, web books, checks, OCR bounds, progress/cancel | – |
| `84b0a4c6e`, `3c4c9564a`, `c2c914a46`, `645349a34` | History UX: tags, size on disk, child sessions, error counts | – |

Upstream merges (`#13xxx`) touching history and tokens (`6ff6a1ecb`, `a56af4efa`, `29530caa5`, `274e0c574`) are telemetry and UI only. Relevance –, except `274e0c574` ("silently repaired truncated tool-call JSON"), which is L and matches our arg-parse guard.

---

## 7. xOllama's port, and its revert to a one-call fold (lessons only)

- **The port** (`xo/server/council_compaction.go:1-80`) copies Cerebriline's constants by name, `:46-80`. It keeps a per-conversation server-side record applied to each turn (`:253-330`).
- **Revert to one pass** (`:125-131`; `xo/plans/agentic-council-chat.md:2347-2377`). Owner ruling 2026-09-30, after the all-glm council fixed "hard" at **13× plain cost**: 10,866 s, 875k tokens out, critics 1.94M input tokens at ~44k per call. Review and retrospective are opt-in (`council.context.review`, `.retrospective`).
  - **Lesson:** at council scale (N lanes × roles), the 3 extra council calls per compaction multiply. Default to the writer only and make the review opt-in. This is the opposite of Cerebriline's single-agent default.
- **A fold must be applied and measured on the same view it was made from.**
  - Folds of the client's raw messages never matched the members' view, so "every fold was dropped the moment it was made": 8 of 8 dropped, about 6 min each (`xo/STATE_SUMMARY.md:665-668, 676`; `council_compaction.go:462-468`).
  - **Ours:** compact the exact message list that will be sent. `_plan_and_compact` already does this per payload.
- **A compaction request must not go through council orchestration.** A client compaction through the council took 13.5 min (`xo/STATE_SUMMARY.md:669, 677`).
  - **Ours:** `retrospective._call` is a plain `chat_client.chat`. Keep it that way, and never route it through council lanes.
- **Report the conversation's size, not the largest member's prompt.** A 229k synthesizer prompt nearly folded a 159k conversation (`xo/STATE_SUMMARY.md:719-720`).
  - **Ours:** the per-model calibration bucket mixes roles. Key it per role and lane.
- **Discard a fold that does not shrink the conversation** (`council_compaction.go:550-557`).
- **One fold at a time per conversation** (`singleflight`, `:444-453`).
- **No-tail rescue is kept** (`:530-545`).
- **Compact on a full owner and resume the turn** (11.26, `xo/STATE_SUMMARY.md:842-856`).
- **Idle fold:** compact between turns at a lower threshold (`council_compaction.go:483-500`).
- **Stall wrapper per member:** 5 min no output, 20 min total, 2 retries at 2 s / 4 s, no retry on refusal (`xo/plans/agentic-council-chat.md:2378-2382`). This applies to compaction calls too, if ours ever gain a timeout. Ours currently inherit only ChatClient retries.

---

## 8. Porting spec, in priority order

Each item: fork source → target in ours → spec.

### P1-a. Fix the compaction target

- Fork: `compaction.ts:539-556, 1305-1319`.
- Ours: `council.py:1092-1094`.
- Spec:
  - `target = 0.25 × (context_length − request_overhead)`, where overhead is the system message plus tool schema tokens.
  - Clamp to `≤ trigger − 1`.
  - Pass `target` (not the trigger) to `compact_messages(keep_tokens=…)` and to `retrospective.write(target_tokens=…)`.
  - Expect smaller digests: 0.33×0.25 of free content at generation 1.

### P1-b. Writer retries with overrun measurement

- Fork: `agentic-compaction.ts:204, 224-232, 856-1015`.
- Ours: `retrospective.write` / `_call`.
- Spec:
  - Up to 3 attempts. A throw resends as-is, and so does an empty response.
  - Over budget (`estimate(text) > summary_max_tokens`): append the `describeOverrun` text verbatim, with numbers.
  - Keep an over-budget text on the last attempt. Never keep an empty one.
  - Log `likelyCause=output_budget_consumed_by_reasoning` when reasoning chars > 0.

### P1-c. Echo cut on every compaction output

- Fork: `replay-compaction.ts:125-148`.
- Ours: after `_call` in `retrospective.write`, both phases.
- Spec: the regex `^(Conversation:|Previous summary:|\[Bot tool calls\]:|\[Tool result\]:)` outside fences. Adapt it to our serializer's markers `[assistant]`, `[tool]` and `(called:` (`retrospective.py:437-442`). Consider renaming our serializer markers to harness-only strings, so prose can't collide.

### P1-d. Verbatim user requests block

- Fork: `compaction-shared.ts:1103-1181, 2064-2078, 2293-2302`; `agentic-compaction.ts:169, 190-202, 583-605, 783-785, 1185-1190`.
- Ours: `render_marker` and `compact_messages`.
- Spec:
  - Collect user turns that are not tool results and not markers. Merge with `_compaction_user_requests` from the previous marker.
  - Budget `0.15 × target × 3 chars`, per request `clamp(200, budget/n, 2000)`, head+tail elision.
  - Never drop a request.
  - Put the block first in the marker, and in the summarizer request. This mostly matters for follow-up threads (`runner.py:746-754`).

### P1-e. No-tail rescue

- Fork: `compaction.ts:1557-1630`.
- Ours: `_plan_and_compact` after `result.changed`.
- Spec:
  - If `estimate(result) > trigger`, run `compact_messages` with tail budget 1. Keep the last user turn and use the full prompt.
  - Take that result only if it is smaller.

### P1-f. Wire the starved-cap and overflow signals, or delete them

- Fork: `compaction.ts:1124-1175, 1073`.
- Ours: `token_calib.last_output_cap` and `consume_context_overflow`, both currently unread.
- Spec:
  - Before planning, if the last cap was window-bound and `< output_room`, and its estimate is within 1.25× of the observed count, force compaction once (latched).
  - Key the calibration per (role, lane), not per model (`token_calib.py:114`).

### P1-g. Reply-reserve outlier rule

- Fork: `compaction-shared.ts:297-304, 401-411`.
- Ours: `budget.observed_output_tokens`.
- Spec: `max(max(newest 3), second_largest if ≥4 samples else largest)`. Measure from transcript content, not from the billed `_output_tokens` (fork `:367-376`). Consider raising `MIN_OUTPUT_ROOM_TOKENS` 2048 → 8000 (`:316-328`). At that floor the fork saw caps that cut tool JSON.

### P2-a. Present-tense replay prompt for tail mode

- Fork: `replay-compaction.ts:59-100`; system `agentic-compaction.ts:128`.
- Ours: replace `DEFAULT_COMPACTION_PROMPT` and `SUMMARY_SYSTEM_PROMPT` when the tail is kept, which is always for us.
- Spec: insert the replay as a `user` message **after the kept head**, not a mid-history `system` message. Keep the retrospective first and the elision note last.

### P2-b. Tool ledger with `[#N]` citations

- Fork: `tool-ledger.ts` whole; `agentic-compaction.ts:171-180, 722-750, 1133-1147, 1176-1184`.
- Ours: new `consultants/engine/tool_ledger.py`, fed from the dropped `tool_calls` and `tool` messages.
- Spec:
  - Limits 200/400/300 chars. Collapse repeats.
  - Verdict tools: `run_*`, `check_*`, and our `grep` with zero matches is also a verdict.
  - Evict in 3 tiers. Budget `max(2000, 0.10×target×3)`.
  - The writer gets the key only. Splice after the writer.
  - Count `invalid` citations and surface them like CitationLinter issues.

### P2-c. Thinking-only turn re-ask

- Fork: `4c3aa0ec7`.
- Ours: `agent_loop/runner.py` final-turn handling.
- Spec: a turn with reasoning but no content and no tool calls is asked again once, `think=False`.

### P3-a. Compaction council (2 half-critics + synthesizer), opt-in only

- Fork: `council-compaction.ts` whole.
- Ours: an optional third phase in `retrospective.write`.
- Spec:
  - The writer prompt gets the HALFWAY section (`:59-72`).
  - Split with band 0.3.
  - Critics run in parallel only when the lane's window allows. Serial otherwise.
  - Critic ±10%, synthesizer ≤1.1×, merge reject <0.5×. Splice-back only on a real marker. Each half falls back to itself.
  - **Default off**, per the xOllama cost data (§7).

### P3-b. Full state-record prompt

- Fork: `full-compaction.ts:43-93`.
- Spec: for the rescue path in P1-e only.

### P3-c. Stale-read rewrite at 40% window

- Fork: `message-builder.ts:94-115`.
- Ours: researcher loop history. Rewrite superseded `read_file` results for the same path once the history is ≥ 0.4×window.

### P3-d. Prompt-cache continuation for Ollama

- Fork: `continuation-compaction.ts:837-915`.
- Spec: the writer as the next turn of the role's own conversation, `tool_choice none`, the same reasoning settings. Only where the endpoint has prompt caching (local Ollama). The text path stays for cloud.

---

## 9. Gap table

| Feature | Fork | Ours | Status |
|---|---|---|---|
| Trigger: ratio + room, min 0.5 window | `compaction-shared.ts:467-484` | `budget.py:205-230` | ported (ratio 0.8 vs 0.9) |
| Output-room observed rule (recent-3 / 2nd largest, from transcript) | `compaction-shared.ts:377-412` | `budget.py:159-183` (plain max, from bill) | partial |
| Min output room 8000 | `compaction-shared.ts:328` | `budget.py:104` (2048) | differs |
| Content-share target 0.25 | `compaction.ts:539-556` | `council.py:1092` (target = trigger) | **gap** |
| Preserve-recent ladder (2/3 exponent, 0.6 cap) | `compaction-shared.ts:44-97` | `budget.py:116-128, 186-202` | ported |
| Messages-ratio floor 0.25, last-turn ceiling 0.66, pinned prompt | `compaction-shared.ts:1251-1336, 1470-1642` | — | not found |
| Safe cut boundary / orphan tool results | `compaction-shared.ts:1237-1239, 1375-1385` | `budget.py:445-451` (forward trim) | ported (different method) |
| Same model for every step | `compaction-shared.ts:2192-2208`; `continuation-compaction.ts:601-606` | `council.py:1106-1109` | ported |
| Summarizer `thinking:false` | `compaction-shared.ts:2190`; `continuation-compaction.ts:1347` | `retrospective.py:520` | ported |
| Generation ladder 0.33…0.55, 70% summary | `compaction-shared.ts:185-240` | `retrospective.py:64-67, 139-151` | ported (base differs, see target) |
| Retrospective 0.2 floor / 0.5 cap | `compaction-shared.ts:199-200, 249-259` | `retrospective.py:72-87, 154-169` (cap only, no floor by policy) | deliberate divergence |
| Retrospective prompt + previous carry | `compaction-shared.ts:2110-2152` | `retrospective.py:225-261, 466-477` | ported verbatim |
| Reasoning-with-outcomes serializer (6000/turn, verdicts, cost label) | `compaction-shared.ts:813-907` | `retrospective.py:323-413` | ported |
| Replay (present-tense) prompt | `replay-compaction.ts:59-100` | — | not found |
| Full state-record prompt | `full-compaction.ts:43-93` | — | not found |
| Writer system prompts per mode | `agentic-compaction.ts:127-135` | `retrospective.py:263-273` (hand-over) | differs |
| 3 attempts + overrun measurement | `agentic-compaction.ts:204-232, 856-1015` | `retrospective.py:504-532` (1 attempt) | **gap** |
| Echo cut | `replay-compaction.ts:125-148` | — | **gap** |
| trimReplayOverflow (1200-char fences) | `replay-compaction.ts:150-207` | — | gap |
| Files section guaranteed | `compaction-shared.ts:1925-1933` | `retrospective.py:449-457` (in prompt only) | partial |
| Verbatim user requests (15%, never dropped) | `agentic-compaction.ts:169`; `compaction-shared.ts:1103-1181, 2293-2302` | — | **gap** |
| Tool ledger + `[#N]` citations + splice/invalid | `tool-ledger.ts`; `agentic-compaction.ts:722-750, 1133-1147` | — (CitationLinter verifies answers: `citation_linter.py:253, 392`) | gap |
| Council: HALFWAY marker, 2 critics, synthesizer, guards | `council-compaction.ts` | — | gap (port opt-in) |
| Summary message role/order | user; requests → retro → summary → ledger (`compaction-shared.ts:2282-2345`) | system; retro → summary → elision note (`retrospective.py:643-677`) | differs |
| No-tail rescue over trigger | `compaction.ts:1557-1630` | — | **gap** |
| Basic fallback on decline | `compaction.ts:1777-1838` | bare marker (`budget.py:477-488`) | ported (equivalent) |
| Provider-overflow recovery (once, basic floor + 1 agentic) | `agent-runtime.ts:2530-2585`; `compaction.ts:1633-1767` | — | not found |
| Starved window-bound cap trigger | `compaction.ts:1124-1175` | `token_calib.note_output_cap` written, never read | **gap (dead path)** |
| Overflow report consumption | `compaction.ts:1073` | `token_calib.consume_context_overflow` never called | **gap (dead path)** |
| Per-conversation calibration key | `compaction.ts:762, 1067`; `a8d4ae171` | per model (`token_calib.py:114`) | gap |
| Granted / real window | `compaction.ts:797-804` | `/api/show` max (`budget.py:267-285`) | gap (Ollama `num_ctx`) |
| Capped-thinking condenser (transcript) | `capped-thinking.ts:975-1151` | `agent_loop/runner.py:144-197, 557-563`; `capped_thinking.py` | ported |
| Discarded-turn condenser + retrospective | `capped-thinking.ts:844-965` | — | not found (we continue instead) |
| Reasoning replay per provider / inline fallback | `model-facts.ts:510-615` | always stripped (`runner.py:559-560`) | differs |
| Tool-result middle truncation w/ marker (32k) | `message-builder.ts:38-46, 239-248` | per-tool caps at source (`caliber_proxy/tools.py:27`) | partial |
| Stale-read rewrite at 40% window | `message-builder.ts:94-115` | — | gap |
| Per-iteration prepare (mid-turn) | `compaction.ts:651-653` | `council.py:1128-1149` (`prepare_payload`) | ported |
| Continuation compaction / prompt-cache reuse | `continuation-compaction.ts` | — | gap (P3) |
| KV pressure / PolyKV / pools | `kv-pressure.ts`; `polykv-session.ts` | — | n/a |
| Journal (undo, 2 entries) | `compaction-journal.ts` | — (checkpoints.db exists) | gap (low) |
| Step progress notice | `agentic-compaction.ts:428-479, 842-854` | log lines only | gap (low) |
| Compaction spend counted per agent | `compaction.ts:124-129`; `continuation-compaction.ts:368-444` | `_call` drops usage except completion tokens (`retrospective.py:531`); not recorded via `recorder.record_llm` | gap |
| Thinking-only turn re-ask | `4c3aa0ec7` | — | gap |
