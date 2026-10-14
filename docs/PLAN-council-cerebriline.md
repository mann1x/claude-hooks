# PLAN — Consultants council: inheriting Cerebriline (and the PolyKV API)

Status: **all decisions taken, 2026-10-14** (draft of 2026-10-10). Nothing
here is implemented yet. Reports 07 (Milestone G) and 08 (§0.7, §4) are
folded in; §11 records the decisions.

The consultants council is the **reference implementation**: other projects
(giano first) inherit it, and there opencoti and xOllama with PolyKV are
central. So this plan never sizes a feature to solidpc, pandorum, or today's
all-`:cloud` config. Every host fact below is a *probe result the code will
detect*, not a default.

Sources. These are six maps of the Cerebriline fork (`/shared/dev/cline`,
1,226 commits since 2026-08-12, HEAD `400b75535`), opencoti
(`/shared/dev/opencoti`) and xOllama (`/shared/dev/xollama`), each mapped onto
our code with file:line on both sides:

| # | Report | Covers |
|---|---|---|
| 01 | [`research/cerebriline-map/01-providers-budgets.md`](research/cerebriline-map/01-providers-budgets.md) | providers, Ollama account, output cap, `num_ctx`, thinking, calibration, sampling (26-row gap table) |
| 02 | [`research/cerebriline-map/02-compaction-context.md`](research/cerebriline-map/02-compaction-context.md) | agentic compaction, thinking condensation, overflow, triggers, porting spec |
| 03 | [`research/cerebriline-map/03-polykv.md`](research/cerebriline-map/03-polykv.md) | the complete PolyKV API, coverage matrix, `opencoti_langgraph`, council design |
| 04 | [`research/cerebriline-map/04-sandbox-shell.md`](research/cerebriline-map/04-sandbox-shell.md) | the fork's launcher, measured gaps, our sandbox and shell design |
| 05 | [`research/cerebriline-map/05-agents-orchestration.md`](research/cerebriline-map/05-agents-orchestration.md) | resilience, guards, struggle, escalation, reports, timings (23-row gap table) |
| 06 | [`research/cerebriline-map/06-sweep.md`](research/cerebriline-map/06-sweep.md) | everything else, plus the area index of all commits |
| 07 | [`research/cerebriline-map/07-xollama-council.md`](research/cerebriline-map/07-xollama-council.md) | xOllama's council: the JSON protocol between roles, and 24 other mechanisms |
| 08 | [`research/cerebriline-map/08-guards-unattended.md`](research/cerebriline-map/08-guards-unattended.md) | every Cerebriline guard: trip action, human involvement, toggle; scoped for unattended runs |

Excluded by the owner: the fork's atomic-task / change protocol (early phase),
and xOllama's server-side council chat.

---

## 0. Principles (apply to every milestone)

1. **Portable by construction.**
   - Every backend capability is probed per `(base_url, model)` and cached with a TTL. Every limit is a knob with a shipped default and a user override.
   - Nothing reads a hostname, a path under `/srv`, or "this is solidpc".
   - The probe results feed the plan. They are never hand-copied into config.
2. **Knobs follow one layering**:
   - Layers, lowest to highest: shipped default → model template (`config/model-sampling.json`, extended) → user `~/.claude/consultants-config.toml` → project `.claude-hooks/consultants.toml` → per-role → per-(role, model) → per-request.
   - Exact keys beat wildcards, and ties are never broken on dict order. Today `model_sampling.py:155-162` breaks ties on dict order; that is a 06 §2.11 finding.
   - "Configurable" always means both the `/consultants config` menu **and** `claude-consultants config` (rule `feedback_configurable_means_skill_cli`).
3. **Unsupported means byte-identical to today.**
   - Each milestone adds an M12 cohort-2 parity test proving that a backend without the capability (or the feature off) sends exactly what it sends now.
4. **Every guard emits a `nudge` event carrying the number it fired on.**
   - The events are stored per lane in `transcript.db`, not only on SSE, because nobody watches a council live; they also feed the replay fitting.
   - Thresholds are fitted by replaying archived transcripts (`transcript.db`), not by argument.
   - A guard nudges before it acts, and acts only on the narrowest signal (05 §5).
   - **The terminal action is always named and unattended.** It is either a forced tool-less summary with `stop_reason` + `failure_class`, or a tombstone. Never a pause, a question, or a wait for a lead (report 08 §5).
5. **x-tier diversity is preserved end to end** (`feedback_xtier_diversity_priority`). There is one exception: compaction, §3.1.
6. **Cross-OS by default.** Windows parity is in scope for every milestone. The sandbox states honestly where a platform is weaker (§6.6).
7. **Inherit means scope, not copy (owner, 2026-10-10).**
   - Cerebriline is an interactive chat: many of its guards end by asking the user, offering an escalation, or waiting for approval.
   - The council runs **unattended** from submission until it has an answer, or until the adversary or an approval checkpoint asks for input.
   - So every inherited guard is adapted to an unattended action (nudge, condense, re-ask, stop the lane with a named reason, escalate the model, tombstone), or dropped when no adaptation works. Nothing that waits on a human is ported.
   - **Stricter guards** are a toggle, as in Cerebriline: `[guards] profile = "standard" | "strict"`, default `standard`, for users running the council on small models. Configurable from both the menu and the CLI, per project and per run.
     - Model: Cerebriline's **"Strong coding nudges"** (`strongNudgesEnabled`, `completionPolicy.strongNudges`, `agent-runtime.ts:~1380-1430`).
       - On: any turn without a tool call is asked to continue.
       - Off: a nudge needs evidence first — the host knows of unstarted work, the run has already called a tool, or the turn ends on a promise (`announcedIntentWithoutActing`).
     - The fork defaults it **on**; ours defaults **off** (owner).
     - In the council a lane's final report is a legitimate turn with no tool call, so "strong" needs a per-role meaning. The critic and synthesizer are presumably exempt; report 08 settles it.
   - The guard-by-guard mapping is in report 08 (§6 table). §4 below applies it.
   - Cerebriline's Strong coding nudges reach only its lead. Its workers get no no-tool-call nudge at all, because a worker's silent turn is its report (`delegated-agent.ts:502-511`). Our lanes are workers, so the strict profile is new per-role design, not a port. Cerebriline has no small-model switch.
   - **What `strict` changes** (report 08 §3–4; per role and per run, from both the menu and the CLI):
     - researcher, tool_executor and coder: the first call must be a tool call, and a report that arrives after zero tool calls is re-asked once;
     - planner: re-asked once when the reply doesn't parse as a plan (or G2 record);
     - thinking-only re-asks go from 2 to 3, unparsable-call re-asks from 1 to 2;
     - the wrap-up warning before the iteration cap comes at least 2 turns early.
   - Critic, meta-critic, adversary, refuter and synthesizer are never nudged under either profile.
   - The re-ask of a turn that announces work without doing it stays in `standard`, once per lane. Cerebriline's `ask_question` clause is never ported.
   - Naming: the existing `adversary_strictness = "strict"` tunes the critic's judgement, not lane behaviour; docs and menu say so side by side.
   - **Loop guards are not part of any profile (owner, 2026-10-10).** Reasoning-collapse / repetition (D1), cycle detection (D10), the duplicate-call guard and any other loop detector are always on and always enforced, on every backend including cloud. Cloud models loop too, especially when the sampling parameters are wrong. Each trip records the realized sampler (A5), so loops can be traced back to sampling. The strict profile covers the nudges only.

---

## 1. Milestone A — Providers, capabilities, account (foundation)

Everything after this milestone reads its numbers from here.

### A1. Provider clients and one capability probe

A `ProviderCaps` record per `(base_url, model)`, built by one probe pass and
cached (TTL, invalidated on `boot_id` change):

| Backend | Detected by | Learns |
|---|---|---|
| Ollama (local) | `/api/version`, `/api/show` | Modelfile `parameters.num_ctx`, `parameters.temperature`, trained ctx, `capabilities` (thinking/tools), think-budget support (POST `/api/show` with think + options; read back `think_budget_tokens`) — 01 #1, #13, #20 |
| Ollama cloud | `/api/tags` `remote_host`, `:cloud` tags | `model-recommendations` (A3) for ctx, max output and accepted thinking values |
| xOllama | `/api/xollama` features | top-level `session_id`, `placement{}` (never `options.num_ctx`, which reloads), `X-Context-Window`, `client_pools`, `/api/engine` route table — 01 #24, 03 §1.10 |
| opencoti / llama.cpp | `/props.features` (~38 flags), `/health` `boot_id` | `session_id`, `num_ctx`/`num_ctx_min`, `X-Context-Window`, `X-OpenCoti-Boot-Id`, `reasoning_budget_tokens`, full llama.cpp sampler, keepalive streaming — 01 #22, #23, 03 §1 |
| llamafile (`llamafile://`) | registry + `/props` | same as opencoti where the build answers |

- `ChatClient` grows an `OpencotiChatClient` (OpenAI-compatible) and an xOllama extension of the Ollama path.
- The llamafile client stops mapping only `max_tokens` + `temperature` and maps the full sampler with llama.cpp names (01 #22).
- The sampler allow-list gains `num_keep`, `stop`, `think_budget`, `think_budget_message` and friends (01 #21).
- Test endpoints for xOllama and opencoti come from the owner when this milestone starts (owner, 2026-10-10). Until then the offline fixtures are the opencoti docs' quoted responses and `opencoti_langgraph`'s 61 tests.

### A2. Transport hardening (all backends)

- Honour `Retry-After` on 429: seconds or HTTP-date, ≤30 s per wait, 4 attempts, 2 s when unnamed. The slot is always released (01 #17).
- Retry an empty response: `done_reason=stop`, no content, no tool calls, at most 2 times. Calibration uses the accepted attempt's prompt count (01 #18; fork `9d7cf0b6f`).
- **Fault classes** (05 #2, 06 §2.5):
  - transport/server → retry, bounded by the **run deadline**, not an attempt count;
  - refusal/admission → back off;
  - model fault → re-ask, don't retry.
  - Streams that die mid-body or send `data: null` count as transport.
- Tombstoned lanes carry `failure_class=infra|task`. The synthesizer prompt says an infra-failed lane is a coverage gap, not evidence.
- **Learned output ceiling:** a provider's refusal text names its max; parse it (Ollama Cloud, OpenAI, Anthropic patterns), retry once at that value, and remember it per `(provider, model)` (01 #4).
- "Model not found" moves to the next model in the role's chain without spending a retry (05 #16).

### A3. Ollama account

- Keep what we have; it is stronger than the fork. That is the account-wide connection slots from the plan (`ollama_slots.py`).
- Add, per 01 #15/#16:
  - `POST <base>/api/me` through the local server first (no key needed), falling back to the signed direct call;
  - `GET /api/experimental/model-recommendations` (TTL-cached). It supplies `context_length` (A1 window for cloud), `max_output_tokens` (the model max in B1), `required_plan` (warn at config load when the plan lacks it) and the accepted **thinking values** (validate role levels at load; B3);
  - `/api/tags` `remote_host` as the cloud marker, replacing tag-suffix guessing.
- `claude-consultants account` (and a skill menu entry) shows plan, slots in use, and per-model recommendations.

### A4. Calibration — inherit all of it (owner)

Exact algorithm in 01 §6.

- **Guard 1:** a provider count larger than the context window is dropped.
- **Guard 2:** chars/token < 1.2 drops the count *and* the ratio.
- **Guard 3:** residual signal share < 0.2 skips the ratio update. (`RESIDUAL_SIGNAL_SHARE`; our "mostly reasoning" threshold moves 0.6 → 0.8.)
- **Anchored estimate:** project from the last counted request, adding only the chars added since (`anchoredRequestTokens`).
- **Key:** anchor and calibration are keyed per **(lane/session, model)**, not per model.
  - 02 found our per-model key lets fan-out lanes contaminate each other (fork `a8d4ae171`, `4adbd6182`).
  - The model-level ratio stays as the cold-start prior.
- Tool schemas count in both the estimate and the calibration chars (01 #9).
- Every tool-loop iteration calibrates, not only `_single_shot`, via a `LoopConfig.on_response` hook (01 #10).
- Fix the latent bug: `token_calib.py:299-315` stores the count before any check (fork `c61cb3600`).

### A5. Timings and run record

- Persist per `llm_call`:
  - TTFT;
  - slot-queue time (wall − engine);
  - `prompt_eval_duration`, `eval_duration`, `load_duration`;
  - cached prompt tokens;
  - the **realized sampler** (seed, temperature);
  - the granted window;
  - the think budget actually enforced.
- Prefill rate is computed from evaluated tokens only (05 #17, 06 §2.12).
- These feed the stall recalibration (`.claude/rules/consultants-runtime.md`) and every bench below.

**Exit A:**
- every backend type answers the probe against a live endpoint;
- a parity cohort shows unchanged requests for the current cloud config;
- calibration guard tests port the fork's cases.

---

## 2. Milestone B — Budgets: output, window, thinking, sampling

**Closed 2026-10-14.** Exit evidence: a full council on opencoti `@bs2` (DCA on, `session_ctx_max` 2,097,152) completed in 92 s. Each `llm_call` row records:

- `num_predict` 96,000 and the stated cap;
- the thinking budget sent (`medium` 24,000, `high` 48,000);
- a per-instance seed and temperature (lanes distinct, reused within a lane).

Deadlines are re-validated against the measured decode rates (B1). The parity cohorts are green: a default run seeds no budget or think layers, and `seed=off` with range 0 adds nothing. The run also found three defects outside B, all fixed:

- llama.cpp templates rejected several system turns;
- two lanes failing in one step aborted the run;
- `Send` lanes never received `runtime_control`, so fanned-out lanes ran without the stall monitor, typed protocol, budget, think or sampling settings.

### B1. Output cap — fully automatic, 96k ceiling (owner)

**Built 2026-10-14.** The re-check found the per-call hard caps would kill long turns, so they now scale with `num_predict`. Deadlines are unchanged. See `docs/consultants-providers.md` "Budgets".

- `resolve_output_budget(ctx, model_max, user_max) = min(0.75×ctx, 96_000, model_max, user_max)`, replacing the flat 32,768 (01 #3; fork `output-budget.ts:36-102`).
  - `model_max` comes from the probe/recommendations (A1/A3), then the learned ceiling (A2).
  - Unknown ctx → 32,000.
- Wire names per backend:
  - Ollama/xOllama: `options.num_predict`;
  - opencoti/llama.cpp: `max_tokens` (and `n_predict` on `/completion`);
  - the opencoti engine knob `max_gen_tokens` is also honoured where `/props` reports it.
  - One mapping table, tested per backend.
- Reserve against estimate error: `max(1024, ⌈0.12×estimate⌉)` instead of a flat 2,048 (01 #5).
- On overflow the request **keeps its own cap** and flags `needs_compaction`. It no longer drops to 4,096.
- Tell the model: an `# Output Budget` section (cap, and the thinking share when known) is appended once per call, idempotent on the heading (01 #6).
- Knobs:
  - `[budget] output_ceiling` (96000);
  - `output_window_share` (0.75);
  - `reserve_min` / `reserve_share`;
  - per role and per (role, model).
- **Must re-check:** stall thresholds and the 20/60/90-min deadlines against 96k-token turns. Do this from `transcript.db`, never the toy bench (consultants-runtime rule).

### B2. Context window — a knob, defaulting to the model

**Built 2026-10-14.** The PolyKV `dynamic` mode (owner: sessions start without a fixed context; one connection carries many agent sessions, each up to `session_ctx_max`, the model's window or more with DCA) is `[endpoints.X] window = "dynamic"`, the default. `assured` is refused until F3's session manager can book and close per-agent sessions. A granted window is honoured only from a request that stated one.

- `[budget] num_ctx = "model"` by default. The window is then resolved in this order:
  1. Modelfile `num_ctx` (local);
  2. recommendations `context_length` (cloud);
  3. trained ctx;
  4. 32,768.
- An integer pins it, per role or per (role, model). `RoleConfig.ctx_max` becomes this knob's per-role form, finally wired (01 #1, #2).
- Local Ollama: the resolved window is sent as `options.num_ctx`, identical on every call to that model, because a change reloads the model. Cloud: not sent. xOllama: never in `options`, only via `placement` (A1).
- PolyKV backends support **both modes the owner named**:
  - **Assured:** state `num_ctx` (and `num_ctx_min`) and get a booked window; the granted size is read from `X-Context-Window`.
  - **Dynamic:** send no `num_ctx`. The engine admits per request (peak + 1 when `max_tokens` is set, opencoti patch 0533), and compaction is driven by the engine's pressure and admission signals (`/kv`, `/capacity`, 429/503 shapes, 03 §1.5/§1.8) instead of a fixed window.
  - Selected by `[polykv] window = "assured"|"dynamic"`; the implementation is in Milestone F.
- `budget.plan` always budgets against the window the backend **actually** gives:
  - granted window first;
  - then the resolved knob;
  - never the trained ctx for a local model.

### B3. Thinking — per backend, level fixed per (role, model)

**Built 2026-10-14, closed.** The template `think` layer exists, but no shipped template sets a value until one is measured. xOllama's read-back is verified live (reply, streamed, and `/api/show` before the call) on solidpc `:11433` and eleven2go `:22434`.

- **Level resolution** (owner):
  - fixed per role **and** per model, configurable as `roles.<role>.think` plus `roles.<role>.models.<model>.think`;
  - not derived from effort;
  - not the same for every model, and not the same for one model across roles.
  - Shipped defaults stay `DEFAULT_THINK_BY_ROLE`, extended with per-model overrides in the model templates.
- **Accepted values:** `false | true | "auto" | minimal | low | medium | high | xhigh | max | <int tokens>`. `"auto"` / `true` = bare `think:true`, which lets the model's own Modelfile budget apply (01 #11; fork `27ac1ba05`). `xhigh` maps to `max`.
- **Per backend:**
  - **Ollama cloud:** no token budget exists. Send the level only if `thinking_values` (A3) accepts it, else `think:true`. Never send an absent `think` to a reasoning model: on a 400 naming `think`, retry once with `think:true` and strip only when the probe says the model has no `thinking` capability (01 #12).
  - **opencoti / llama.cpp:** level → tokens via the fork's fractions of `min(num_predict, num_ctx)`. The fractions are max 4/5, high 1/2, medium 1/4, low 1/8, minimal 1/16. The result is sent as `reasoning_budget_tokens` plus `reasoning_budget_message`, because the engine discards `reasoning_effort` (01 #13, #22).
  - **xOllama:** full support for level, integer and message. The enforced budget is read back from `think_budget_tokens` in the final chunk.
  - **Local think-budget Ollama builds:** as xOllama, with read-back via `/api/show`.
- **Budget message:** the configured `think_budget_message`, else the model's own. We ship no invented default text, matching the fork.
- **Capped-thinking detection** compares against the **real thinking allowance** (read back, else computed), not `num_predict`:
  - the budget message and the fork's generic admission regex are passed into `LoopConfig` and into `_single_shot` (01 #14);
  - today the check can never fire at `high`.

### B4. Sampling diversity — every role (owner: CRITICAL)

**Built 2026-10-14.** See `docs/consultants-providers.md` "Sampling diversity".

The same model will often run in several pooled sessions and must produce
different outputs. The mechanism is the fork's (`spawn-sampling.ts:54-60,
223-433`; 01 §7, 05 #5, 03 §4), applied to **every role**, not only researcher
lanes:

- `seed`: default `"random"`, uniform in [0, 2^31) per call-site instance, recorded.
  - An integer seed shared by N same-model instances becomes `seed + i`.
  - `off` sends no seed.
- `temperature`: default **the provided temperature** (explicit option → model template → `/api/show` `parameters.temperature` or opencoti `/props` → unset).
  - `temperature_range_pct` defaults to **2** and is clamped to **10** (the fork's margin): `T = base × (1 + u × range/100)`, u ∈ [−1, 1], 3 decimals, ≥0.
  - If no base temperature is known, it stays unset.
- Per-role and per-(role, model) override of `seed`, `temperature`, `temperature_range_pct`.
- The realized values are written to `llm_call` (A5) and shown in `show`.
- Tests:
  - a parity cohort with `seed=off, range=0`;
  - a property test that same-model instances in one run never share a seed;
  - a test that the jitter respects the 10% clamp.

**Exit B:**
- the budget bench (Phase 0 of the earlier draft, now part of A5) shows caps, granted windows and think budgets per call;
- deadlines are re-validated;
- a parity cohort is green.

---

## 3. Milestone C — Compaction (CRITICAL)

### C1. Hard rules (owner)

1. **The compacting model is always the model of the agent being compacted.** Every step of every flow runs on that lane's own model and client.
   - Another lane's model is never used. Diversity is poison here.
   - Enforced in code: `compact()` takes the lane's `(client, model)` and has no parameter for another.
   - A test asserts the model id on every compaction call equals the lane's.
   - Already true today (`council.py:1106-1109`, `retrospective.py:520`); the fork enforces the same (`compaction-shared.ts:2192-2208`, `continuation-compaction.ts:601-606`).
2. **One serial connection.** The steps of one compaction run one after another on one connection.
   - Parallel steps are allowed only under PolyKV (Milestone F), where the half-critics run as workers on forked pools (fork `continuation-compaction.ts:959-1034`).
   - Compaction never goes through the council's own orchestration; xOllama measured a 13.5-minute compaction that way.
3. **Its own stall budget.** Compaction calls get their own stall thresholds and an operator-visible `phase=compaction`, so the stall monitor does not kill them. Refusal waits follow the fork: unbounded for the writer, 3 waits on a 2–30 s backoff for the others.
4. **Thinking off** for summarizer calls (fork `f53b7ab2e`: a thinking synthesizer spent its budget and returned nothing). Writer continuations use the session's settings so the prefix matches.

### C2. Two flows, one pipeline

| Flow | Calls | Default for |
|---|---|---|
| **reduced** (replay) | 1. present-tense **replay writer** → 2. **retrospective** → 3. **reviewer/synthesizer** of the whole replay | **every role, every model** — the shipped default |
| **full (5-step agentic)** | 1. replay writer with `<<<HALFWAY>>>` marker → 2. retrospective → 3. critic, first half → 4. critic, second half → 5. synthesizer (joins halves, sole reviser of the retrospective) | opt-in **per role** by the user; small models need it, so it must work as well as the fork's |

- `roles.<role>.compaction.flow = "reduced" | "full"`, default `reduced` (owner, 2026-10-10). The per-(role, model) layer of §0.2 applies like any other knob.
- **No automatic detection** of small or local models, and **no link to the guards profile**: the full flow is on only where the user turned it on for that role.
- Note from 02 §0: Cerebriline itself has no reduced flow; every model there gets all five calls. The reduced flow is our default by the owner's design. The one-call fold is xOllama's and is **not** adopted.
- **Writer:**
  - The prompt is the fork's present-tense replay, quoted in 02 §5.3. It uses positive exemplars and present-tense section labels. The rationale is kept in the prompt docs: written in the past tense the replay reads as history, which the model feels entitled to doubt.
  - It carries the ledger key and the **verbatim user requests block**: every typed request, follow-up questions included, within a 15% share, none dropped (02 P1-d). Today only the first question survives (`budget.py:429-431`).
  - It retries up to **3 attempts**, feeding back the measured overrun (02 P1-b).
  - Then the **echo cut** and `trimReplayOverflow` / long-fence trim (02 P1-c). Our prompt currently ends with `Conversation:`, which invites the echo.
- **Retrospective:** our port stays, including **no 0.2 floor**. That is a deliberate owner rule from August (`retrospective.py:74-87`): the summary writes first and the retrospective gets what is left.
- **Reviewer / critics:** must keep citations and fix tense. Ratio guards apply: halves within ±10%, synthesizer output ≤ 1.1×, rejected below 0.5× (02 §5.6).
- **Tool ledger:** built from `tool_calls`/results paired by id, with identical calls collapsed unless the result changed. The `[#N]` citations are spliced in after the review, so critics move 4-token citations rather than 400-char entries. The ledger is placed before the CitationLinter (02 P2-b).
- **Placement:** an A/B test of the compaction message as a `user` turn (fork and xOllama order: requests → retrospective → summary → ledger) against our mid-history `system` marker (02 §5.9). Present-tense voice may depend on it.
- **Full mode with no tail** uses the fork's state-record prompt (02 P3-b).

### C3. Target, trigger and rescue (02 P1)

- **Target:** 25% of free content, not `keep = plan.trigger_tokens`. Our digest budget is about 3.6× the fork's today (02 P1-a).
- **Trigger and room:**
  - trigger ratio 0.9 (ours 0.8) and output-room floor 8,000 (ours 2,048), both knobs;
  - the reply reserve follows the fork's outlier rule: the newest 3 turns at full size, else the second-largest with ≥4 samples (02 P1-g);
  - sizing uses what turns occupy in context, not what was billed.
- **No-tail rescue:** if the keep-tail result is still over the trigger, re-run without the tail and keep the smaller result. Measured 34,297 → 17,217 tokens on 32k (02 P1-e).
- **Cut planning** gets the parts not yet ported (02 §1.4):
  - the messages-ratio floor;
  - the last-turn ceiling;
  - the pinned latest prompt (a follow-up is never folded away);
  - the safe-boundary walk-back.
- Fold exactly the view that will be sent (xOllama: 8 of 8 folds dropped on a hash mismatch). Discard a fold that does not shrink the conversation. One fold at a time per lane.

### C4. Thinking condensation (owner: add)

- **Past-turn reasoning on the wire:** per-provider replay mode `none | all | inline`.
  - Measured by probe, overridable per model; the fork defaults ollama/xollama/opencoti to `none`.
  - `inline` is the "goose fallback" `<think>` in content (fork `8baed60ce`) for chat templates that drop the field.
  - The estimator uses the same mode, otherwise the estimate is off by 2.23× (02 §2.1).
- **Inside compaction:** reasoning enters the writer's input truncated to 2,000 chars per block. The retrospective sees 6,000 chars per turn paired with outcome verdicts and cost labels. Our serializer already does the retrospective half.
- **Capped-thinking condenser:** already ported for tool loops. Add:
  - the discarded-turn path, catching turns the loop throws away, which are exactly the ones ending at the budget message;
  - the degenerate-note detector;
  - detection in `_single_shot`;
  - use of the real think allowance from B3.
- **Tell the model** its earlier reasoning is not visible to it, so it doesn't distrust notices quoting it (fork `a16e35478`).

### C5. Overflow compaction (owner: add)

- **Per-tool-result truncation in history:**
  - middle-truncate at 32,000 chars (file content 50,000), with the fork's load-bearing marker: the gap was made by the harness, the file is complete, re-read the range if needed;
  - all are knobs;
  - today we cap only at the source (48 KiB).
- **Stale-read rewrite:** a superseded read becomes `[outdated - see the latest file content]`. It fires only when the transcript is ≥ 0.4 × window, and then all pending rewrites go at once, because each rewrite breaks the prefix cache (02 §3.1).
- **Provider-rejected overflow:** on `context_window_exceeded`, force a compaction with `require_smaller_request` and retry once; a second overflow fails the call with a named error. The order is basic (deterministic) as the floor, then one agentic attempt, accepted only if it is smaller and within target (02 §3.2).
- **Silent truncation detection** (Ollama truncates instead of rejecting, fork `f76029582`): compare `prompt_eval_count` with the estimate. A large shortfall at the window edge is treated as overflow.
- **Starved output cap:** when the last request's cap was window-bound and below the output-room floor, compact, latched once per descent. This wires the already-written `token_calib.last_output_cap` (02 §3.5), and the dead `consume_context_overflow` path is either wired or deleted.
- **Fix the duplicate-call stub after compaction:** `agent_loop/runner.py:331-358` keys duplicates on raw `name|args` and keeps answering "use the prior result" after that result was elided. Key on a content hash, clear on compaction and on any write, and re-serve the real content every 4th repeat, as the fork's `unchanged-reads.ts` does (06 §2.2).

**Exit C:**
- a bench on a small local model (≤ 8B, 32k window) runs long councils through ≥ 3 compactions in each flow, reporting fix-rate by compaction count against the fork's 85/63/50/25% baseline;
- the same-model test and the single-connection test pass;
- parity holds with compaction off.

---

## 4. Milestone D — Lane robustness (found by the full map)

None of these were in the first request; all are measured fork features that
close gaps in ours. Order: the money leaks first.

| # | What | Why | Ref |
|---|---|---|---|
| D1 | **Reasoning-collapse / repetition guard** in `chat_streamed`: low-uniqueness, line-cycle and phrase-cycle detectors with the fork's thresholds; on a trip, cut the stream, keep the text, condense, continue. After 3 trips, a forced summary from the pre-cut transcript with `stop_reason=reasoning_loop`, recording the realized sampler. Plus the repetition nudge (1 per lane) and non-convergence (streak 2). **Always on, every backend** | Reasoning chunks count as liveness, so a looping thought stream defeats the stall monitor and runs to the 5–60 min cap. Ollama cloud has no thinking budget to stop it | 05 #1, 06 §2.4 |
| D2 | **Struggle supervisor** (the fork's worker one): 3 budget-exhausted turns in 6 → nudge; +2 after a 2-turn grace → forced summary `stop_reason=struggle`. Never suspends; the fork's `awaiting_lead` has no analogue. Its non-progress threshold (20 iterations) is refitted for our 4–25-iteration lanes. **Always on** | Replay: 0 false stops in 121 answering workers | 05 #3, 08 §2.12 |
| D3 | **"No tool call" is not always "done"**, split by profile because a lane's final report is a legitimate turn without a tool call. **Standard:** re-ask a thinking-only turn (≤2), a call written inside the reasoning, unparsed `<tool_call>` text, and a turn announcing work it didn't do (1 per lane). **Strict:** the §0.7 additions. Each case has its own reminder | 13 of 15 fork agents ended with the call stuck inside their thinking | 05 #6, #7; 06 §2.3; 08 §2.4–2.5 |
| D4 | **Unparsable tool-call re-ask** naming the swallowed key; never repair the payload. **Argument coercion** only where exactly one reading exists | | 05 #6, 06 §2.6 |
| D5 | **`read_file` honesty**: a read cut at the cap reports the range actually returned and where to resume; `grep` gains context lines | Lanes cited lines they never saw, poisoning the CitationLinter's input | 06 §2.7 |
| D6 | Wrap-up warning K turns before the iteration cap; a final tool-less report turn | | 05 #9 |
| D7 | `stop_reason` + `failure_class` on `NodeFinished`, lane rows, `show` | | 05 #12 |
| D8 | Synthesizer/critic input = index of every lane first, whole reports while they fit (28k-char budget), then named-only with a pager tool | | 05 #8 |
| D9 | `retry-lanes` for tombstoned lanes (operator verb, after the run; under xauto, optionally one automatic retry of `infra` tombstones before synthesis); graceful cancel giving each lane one report turn; an infra-wait event after 10 min that is **informational only**: the run deadline (A2) decides, then the lane is tombstoned with `failure_class=infra` | Cerebriline's 10-min nudge asks its lead to decide; nobody decides mid-council | 05 #10, #11; 08 §5 |
| D10 | Cycle-aware loop guard (A→B→A) plus the duplicate-call guard, exempting polling tools. Soft warnings, a last warning, then on the second hit a forced summary `stop_reason=loop_guard` naming the call. **Always on, every backend** | Cerebriline's limit hands off to the user or the lead | 05 #13, 08 §2.9 |
| D11 | Peer-findings recall gets a relevance floor and per-session "already given" dedup; optional auto-recall at lane start with the fork's gate (`isAboutTheSearch`) | Our `recall_research(limit=5)` has no floor | 06 §2.1 |
| D12 | Per-role tool surface minimization measured from recorder data (fork: 17 → 4 tools, −72% schema) | | 05 #19 |
| D13 | Engine-authored messages carry one tag (`[SYSTEM MESSAGE]`), terse, never shown as the user's | | 05 #18 |
| D14 | **Mistake limit**: 6 consecutive tool errors → forced summary `stop_reason=mistake_limit`. **Always on** | The plan had no consecutive-error limit; Cerebriline's (`maxConsecutiveMistakes` 6) stops for the user | 08 §2.10 |
| D15 | **Empty-turn reminder** and the context-overflow terminal: an empty turn joins A2's empty retry; overflow goes to C5's forced compaction plus 1 retry, then the lane is tombstoned `context_overflow` (Cerebriline tells the user to shorten the input) | | 08 §2.7, §6 |

Later, once D1/D2 emit signals: **per-lane model escalation** (05 #4). A
struggling or tombstoning lane reruns its plan item once on a configured expert
model, given a brief instead of the transcript, at most 2 per council. This is
not compaction, so cross-model is fine here. The harness decides. Cerebriline's
`escalate` tool, the forced one-turn offer, `requireApproval` and the expert-guard
notes are not ported, because they all assume a model or a user making the
decision (08 §2.13).

**Dropped, with reason (08 §6):**
- Cerebriline's lead struggle detector and escalation offer: its hedging-decay signal is kept only as an xauto escalation candidate.
- Checklist close-out, team obligations and the rounds guard: the graph's fan-back already covers them.
- Edit verification and check-on-failure, until Milestone E gives the coder a checker. They come back then with their own mode knob (default nudge).

Candidates, evaluated after D: semantic code search as a `tool_registry`
provider where an index exists (06 §2.8), an external confidence scorer (Jev)
beside the self-rating (06 §2.15), and hub/event hygiene for SSE and
`transcript.db` (06 §2.16).

---

## 5. Milestone E — Sandbox and the shell tool (M-D)

Design in 04 §5. The goal is a stronger sandbox than the fork's. The fork
ships in VS Code and took the easy path, and its sandbox only blocks writes. On
solidpc, measured: its launcher does not even start (glibc 2.34 needed, bullseye
has 2.31). In direct mode a command ran as real root, read a raw NVMe device,
read `~/.claude.json` and `~/.ssh`, and saw every interface and every host PID.

### E1. Prerequisite fixes (found by the map)

- `tool_surface.py:100` calls `as_executor()` without `runtime_permissions`, so `POST /control` permissions never reach the gate.
- `control.py:55` validates against `allow/deny/ask` while the gate uses the four-level ladder, so "allow" is read as invalid and denied.
- `tool_cache` clears every lane's cache on any non-read-only call, which a shell would trigger constantly. Add an `ISOLATED_EFFECT_TOOLS` class that only invalidates its own lane.
- The executor has no lane identity: bind it in `graph.py:415-430` (`LaneBoundExecutor`).
- `coder-out/` and per-session dirs are never collected.
- Update the stale status header of `PLAN-council-tool-surface.md`; the approval channel is built.
- `scripts/sandbox_probe.py`: a host capability matrix. It covers namespaces, cgroup delegation, seccomp, Landlock ABI, FICLONE per filesystem, overlay copy-up with `xino=auto|off` and tmpfs `inode32`, bwrap, and free-space floors. It answers whether the fork's EOVERFLOW comes from `XINO_AUTO` + `TMPFS_INODE64` and not from quotas.

### E2. The launcher: `council-sandbox`, a deploy artifact

- Built **statically** (musl; CI asserts "statically linked", because the glibc trap is exactly how the fork failed here).
- Published as a release asset with a sha256 pin.
- `step_sandbox` runs before `step_services`; `check_sandbox` runs a real smoke test that tries escape attempts.
- `tests/test_deploy_completeness.py` gets the new class first (deploy rule).
- Linux layers, in setup order (04 §5.3):
  1. cgroup v2 leaf with cpu/memory/pids limits;
  2. user namespace mapped to nobody (or a `claude-council` uid on root hosts);
  3. a fresh root with the lane view bound at the repo's real path, the rest read-only, and no host `/dev`, `/proc` or `/sys` beyond a minimal set;
  4. PID, IPC, UTS and network namespaces, with only `lo` unless egress is granted (E6);
  5. rlimits;
  6. all capabilities dropped, plus no-new-privs;
  7. seccomp, denying `unshare`/`setns`/`CLONE_NEW*`, `mount`, `ptrace`, `io_uring`, `bpf` and similar;
  8. Landlock **optional**: used when the kernel has it active, never required. Its absence is not a missing layer (§11.5);
  9. an environment allowlist.
- **A missing required layer means no shell**, never a weaker shell silently. `preflight` says which layer is missing.
- **P1 interim backend:** bwrap (0.4.1 is present here and verified to run as nobody with only `lo`), wrapped in `systemd-run --scope` for limits, until the launcher lands.

### E3. Lane views: worktrees, snapshots, scratch

Chosen per root, by what its filesystem supports (probed, never assumed):

| View | When | Cost |
|---|---|---|
| read-only + scratch dir | default for `shell=read` | none |
| **git worktree** from a snapshot commit of the current state (including uncommitted changes), kept under `refs/claude-hooks/council/<sid>` | git roots | cheap; ignored dirs bound read-only, or overlaid for write builds |
| **reflink clone** of a declared write scope | non-git roots on XFS/Btrfs (e.g. `backup_models` is XFS with reflink) | O(metadata) |
| **overlay** mounted only inside the command's namespace, with a sparse-image upper for a hard quota | non-git roots where overlay copy-up works. E1 probe (2026-10-14): an unprivileged overlay with a host-filesystem lower fails copy-up (EOVERFLOW) inside a user namespace, so it needs a privileged mount step before capabilities drop; see report 04 | O(changed files) |
| capped copy | last resort | bounded by quota |

- Snapshots for non-git dirs replace the fork's in-RAM copies. Those are capped at 5,000 files / 128 MB, which is useless on a tree whose `scripts/` alone is 136 GiB.
- Views live on the root's own filesystem under a configured `store_root`, never under `~/.claude` (solidpc `/` is 96% full). A `store_root` without its free-space floor **fails** verify.

### E4. Lifecycle — no stale worktrees, no disk trashing

- **States:** `creating → active → frozen → held → applied | discarded | expired`, persisted in `sandbox.json` per session and in the inflight record.
- **Quotas:** lane 2 GiB, council 8 GiB, global 32 GiB, plus a free-space floor; all knobs.
- **Reaping** has four triggers:
  - council end (views with no changes are removed immediately);
  - the idle reaper (held views expire after a TTL, default 7 days, after which the diff is kept as a patch file and the view is removed);
  - engine startup (orphans from a crash);
  - `claude-consultants sandbox gc`.
- Nothing is ever mounted in the host namespace, so a crash or kill -9 leaves no stale mounts. That is tested at every state.
- **When the council finishes:**
  - the answer carries a **"Sandbox changes"** section listing per lane the files changed, the diffstat and the command log;
  - verbs: `changes`, `apply` (3-way or base-hash check), `discard`, `sandbox ls|gc`;
  - the skill is told explicitly what to do: review the diff, **ask the user before any apply**, discard what is not applied, and never leave a held view without telling the user its expiry.

### E5. Enablement — off by default, addable in a follow-up

- **Shell is off by default.** It is enabled only:
  - on request (`--shell[=read|write]` on `ask` and on `follow-up`);
  - by the user knob `[tools.shell] default = "always"` (menu and CLI);
  - per project.
- **Runtime-addable:** a follow-up body carries the shell setting. It is persisted on the session, the inflight record and the consultancy, and applied when the follow-up's graph is built (`runner.py:825`).
  - Use case: a consultancy came back confused because it could not run tests, so shell is enabled for the follow-up.
  - The answer's provenance line states "shell: enabled since follow-up #k (mode, net)" (04 risk 9).
- Survives an engine restart mid-command; the interruption is reported.

### E6. Shell tool semantics

- `run_command` is classified (`read_only=False`) through the taint ladder. The classifier now decides cost and egress, not host safety, because the jail is the safety.
- Unmatched commands go to `ask_assistant`.
- Network defaults to none. `net=proxy` goes through the M-F network provider's allowlist, with taint on the first response.
- Output is captured, capped and middle-truncated with the C5 marker.

### E7. Windows and macOS

- **Windows:** AppContainer + Job Object (limits, kill-on-close), a worktree view, and an optional Detours overlay view (a port of the fork's `hook.dll`) for non-git roots. Where AppContainer breaks a toolchain, Low-IL is the fallback, and preflight reports `net: unenforced`, gated as a policy choice (withhold, or ask per command).
- **macOS:** Seatbelt (no network) + rlimits + clonefile.
- Both are weaker than Linux, and the docs say exactly where. pandorum smoke runs before any tag.

### E8. Hardening

- Move the consultants unit under systemd hardening (`ProtectSystem=strict` like the daemon), since the engine runs as root today.
- Landlock stays optional (§11.5). Document the boot parameter that activates it (compiled in but inactive on solidpc). A reboot is scheduled only if the escape suite needs it.
- An optional VM backend (Firecracker or gVisor) for a `paranoid` mode, later.

**Exit E:**
- the escape suite is green on Linux (04 §5.11 list, plus ptrace of a host PID, `mount`, `unshare`, `io_uring`, writes to `/dev`);
- kill -9 at every lifecycle state leaves nothing behind;
- a `backup_models` bench creates a view in < 2 s with no data-proportional disk use;
- a follow-up adds shell to a consultancy that started without it;
- the Windows smoke passes.

---

## 6. Milestone F — PolyKV, last, complete and careful (owner)

Design in 03 §5; the API in 03 §1. The goal is broader coverage than
Cerebriline's. xOllama council chat is out of scope.

### F1. Client

- Port, not import, `opencoti_langgraph`. It pins newer langchain-core/langgraph than our env, and we don't use its chat-model class. Take:
  - its payload builders and error mapping;
  - its stream stall watch;
  - its `prompt_progress` liveness;
  - its 2 s keepalive;
  - its pool-prefix algorithm;
  - its 61 offline tests.
- Add what it lacks:
  - owner / `shared` / `unowned` pools;
  - `/kv`, `/capacity`;
  - session close/resize;
  - `/apply-template`;
  - `boot_id`;
  - `continue_pool`.
- **Beyond Cerebriline:**
  - copy-on-write branches;
  - explicit `tokens` / `from_slot` / `expect_len` pools;
  - DELETE on pools;
  - `keep_kv` on close;
  - `/polykv/sampling`, `/metrics`, `?live=1`, `return_progress`, `X-Sessions-Remaining`;
  - a warning when `n_pool_shared` is 0 (a silent miss today);
  - honouring `found` on release.
- Through xOllama: the `/api/engine` routes, `client_pools`, and `session_id` + `placement{}` on native `/api/chat` only.

### F2. Modes

- `off`.
- `affinity`: a stable session per lane, no pools.
- `pooled`: shared prefix pools; `num_ctx` **optional** (B2 dynamic mode). The engine's admission and pressure drive compaction.
- `assured`: one owner window per run, every role call a worker charged to it, booked with `num_ctx`/`num_ctx_min`; the granted window is read from `X-Context-Window`.

### F3. Per-run `KvRunManager`

- Held beside the chat clients through `GraphDeps`, never in council state.
- Sessions are `<sid>~<role>~lane<k>`; `/` and other route-breaking characters are replaced with `~` (fork gotcha).
- **Prefix layers** (requires the prompt reorder, F4):
  - P0: system and tools, find-or-create, shared across runs with the same toolset;
  - P1: grounding and question;
  - P2: plan;
  - P2r: researcher role, shared by all lanes and tool_executor items;
  - P3: reports, shared by critic, adversary, refuter and synthesizer.
- Each layer is rendered through `/apply-template` with a sentinel cut and byte-prefix-checked, so a one-token miss is detected, not silent.
- **Session capacity (owner, 2026-10-14):** one PolyKV session can carry up to **32 workers/agents**. A whole council fits in one owner session: lanes and roles are workers on its pools, not one session each. The engine's parallel slots (8 on bs2 after the 2026-10-14 restart; `/props` `total_slots`, elastic) bound how many decode at once. Workers beyond that queue on 429 + `Retry-After`; they are not refused.
- **Window with DCA:** with DCA on, bs2 reports `session_ctx_max` = `kv_pool_cells` = 2,097,152, so one session may grow to the whole pool. The owner booking and compaction pressure must read `/kv` live (`cells_free`, `largest_admissible`, pressure); a session ceiling equal to the pool is not room any one lane can count on.
- **Fan-out:**
  - a capacity read plus a semaphore before Sends;
  - owner grow/wait when full;
  - a 60 s owner keepalive;
  - 429 handled as a queue with `Retry-After`;
  - **B4 seeds and temperature jitter on every pooled same-model session**.
- **Compaction (C) under PolyKV** follows the continuation path:
  - freeze the session as P;
  - the writer continues in its own slot;
  - fork P′ at the writer's cache position;
  - the half-critics run **in parallel only here**, as workers on P′ when the owner has ≥ 2× room, else serially;
  - release leaves first;
  - erase the slot rather than close it.
  - Same model throughout.
- **Follow-ups** adopt the parent's owner and pools while held.
- **Durable resume:** pool state goes to a sidecar `polykv.json`. On resume, check `boot_id`; if it changed, rebuild lazily (pool ids are renumbered after an engine restart).
- **Settle** (`_settle_inflight`): release leaves first, then a final `/kv` audit that logs anything leaked.
- **Fallback:** if the probe fails at any point, requests are byte-identical to the non-PolyKV path.

### F4. Prompt reorder

- Today every role opens with its own system prompt, so roles share nothing.
- On PolyKV backends the shared council system turn and grounding go first and the role's instructions last.
- An A/B on answer quality decides whether that layout also becomes the default elsewhere; it would also help Ollama's implicit prompt cache.

**Exit F:**
- live runs against the owner's opencoti and xOllama endpoints report per council the prefill saved, `n_pool_shared` per call, peak cells and leaks (0);
- a restart test passes mid-council;
- same-model lanes produce distinct outputs (B4);
- parity holds on non-PolyKV backends.

---

## 6b. Milestone G — Typed messages between roles (from xOllama's council)

Source: report 07. The owner reports that xOllama's council became much more efficient when its roles started passing JSON to each other. Only the synthesizer still writes prose, for the final answer. The report confirms the design, but xOllama measured it only as part of a bundle of about ten changes (cost went from 13× plain to 1.25×, on a different model). There is no isolated A/B, so we run one (G6).

Ours today is prose read back by regex:
- the plan as numbered items (`_PLAN_ITEM_RX`);
- the critic's `DECISION:` and `CONFIDENCE:` lines, where a missing decision means ready;
- the xauto escalator's phrase matching;
- the adversary's `REFUTATION:`.

`_to_ollama` drops `format`, and the OpenAI path sends no `response_format` (`chat_client.py:851-876`, `:1131-1158`).

### G1. Carrier and plumbing

- **Typed results are tool calls the engine answers itself**, as in xOllama's `council_report` / `council_verdict` / `council_done` (`internal/council/report.go`, `3864cf345`).
  - Tool-call arguments work on every backend. Upstream docs say Ollama Cloud does not support structured outputs (unverified; the A1 probe answers it per model).
  - A result call counts only when it is the turn's **only** call, so a report is never written before the reads it rests on.
- **Format passthrough**: native `format` and OpenAI `response_format`, used where the probe says the backend supports them (route, plan). One-shot retries:
  - format refused → ask once without it;
  - empty record because thinking ate the reply → ask once with thinking off.
- **Schema rule:** bound item counts in the schema and text length at render. Never use `maxLength`: it made xOllama's grammar unparseable and killed run 0415 at 692 s (`55d4baf70`).
- **A cut record is asked again ("smaller"), never continued.** Our truncation policy continues cut prose, but a cut tool call is lost.
- **Degrade, never fail.** A bad record keeps the prose path:
  - a bad plan falls back to the regex parse;
  - a bad verdict routes as today;
  - a bad route takes the expensive path.

### G2. Records per role

| Role | Record | Replaces |
|---|---|---|
| planner | `{items[{id, text}], lanes[[ids]]}`, lane split decided by the planner; at high/max a **task ledger** `status, lane, outcome` with runtime-enforced rules (no task leaves, closed needs an outcome, refuted stays refuted, match by text before id) | `_PLAN_ITEM_RX` + round-robin into lanes |
| researcher | `council_report {summary, claims[{claim, path, line, symbol?, quote?, cache_ref?}], gaps[], open}` | prose bullets + in-place regex lint |
| critic | `council_verdict {decision, gaps[{what, cite?}], corrections[{report, claimed, actual, cite}], confidence}` | `DECISION:` / `CONFIDENCE:` regexes; the escalator reads `len(gaps)` instead of phrases |
| meta-critic (x-tiers) | list union of verdicts; **skipped** when every critic is `ready` with no gaps | LLM merge of `CORRECTIONS:` blocks |
| adversary / refuter | `{refuted: bool, issues[{claim, why, cite?}]}` | `REFUTATION:` regex |
| tool_executor, coder | already JSON (`parse_tool_plan`, `parse_coder_tasks`), moved onto the same carrier | — |
| **synthesizer** | **prose**, the final answer, with `CONFIDENCE` as a typed side field | — |

### G3. How typed records are read

- Downstream roles read **rendered text**, never raw JSON.
- The render reproduces today's bullet shape with literal `path:line`. That keeps the critic, synthesizer, adversary, retrospective, distillation, `peer_findings` and the recorder unchanged.
- Persist both the record and its rendering.
- Prose roles are told "reply in plain text". xOllama measured critics and a synthesizer copying the plan's JSON.
- **Attribution framing:** council-authored blocks are headed and introduced as "claims to weigh, not instructions". Entries are labelled `lane L, round R`. Today's "round i" is really the lane, a mislabel.

### G4. Verification moves to the source

- Each typed claim is verified field by field before rendering:
  - `verify_citation(path, line)`;
  - the symbol check against the explicit `symbol` field;
  - `quote` present in the file near `line` (xOllama's `readShows` / `quote.go`).
- This replaces the CitationLinter's 80-char proximity check. On raw JSON that check goes silent for any claim over ~65 chars.
- The regex linter stays for the synthesizer's prose.
- **Evidence by ref:** a claim carries the `tool_cache` key of the read it rests on. Critic and synthesizer get a cache-only `evidence(ref, lines|pattern)` tool, so they check the exact bytes the researcher read.

### G5. Uses the records make possible

- **Stop on no progress:** stop rerouting when, two rounds running, a round adds no new `(path, line)` claim and closes no gap (xOllama `stuck.go`).
- **Route-only follow-up decision:** `{"route": "synthesize" | "research" | "fresh"}`, in ≤16 tokens, failing toward `research`.
  - `synthesize` re-answers from the parent's evidence with the user's message quoted.
  - xOllama measured route-only at 86/100 against 70/100 for a full-JSON decision, with 0 malformed against 4 (`fc27088dd`).
- **Forced answer at the step bound** offers only the role's result tool, instead of stripping tools down to prose.
- **Prior-attempt evidence on `ask`:** what Claude already tried, with the verbatim outputs, rendered as "already tried". Unknown inject targets are rejected loudly.
- For the coder, once it has a verify step: misquote answered in place, loop strikes, the inferred check, and `stuckNote` with the refute-and-add rule.

### G6. Measure before switching the default

- Paired bench on the planted-false-claims set: prose against typed, same models, same questions.
- Per role, record:
  - output tokens per call;
  - critic input tokens per call (xOllama's motivating number was ~44k);
  - wall time;
  - caught-false-claim rate (ours is 100% today, n=72);
  - CitationLinter fabrication count.
- The typed path ships behind `[protocol] typed = false` (M12 parity) until the bench decides.

Already covered elsewhere in this plan, not duplicated here:
- seeds and jitter (B4, every role, as the owner ruled; xOllama jitters only researchers and critics);
- the Output Budget note (B1).

Not taken: the builder role, broadcast notes, the async review desk, and client-held sealed state (our checkpoints are at parity).

**Exit G:**
- the bench is recorded;
- the typed path passes the parity cohort when off;
- every record degrades to the prose path under fault injection (bad JSON, cut call, refused format, thinking-ate-it).

---

## 6c. Milestone H — Decision models (owner, 2026-10-14)

Decision models answer typed questions (noul / choice / score) with
calibrated probabilities and write no text. Two dialects serve one API,
`POST /v1/systemone`: TypeSafe's **Jev** (`jev-latest`, Bearer key) and
Ollama ≥ 0.35 / xOllama with local `decision`-capable models (`tev1`,
`nimble`, `clef-flash`, …). The owner: "they really fit well with the
consultants skill and the council, a precious addition". Cerebriline
(`c498e9cf4`, `f535f2035`) is the reference; its knowledge base is
`/shared/dev/cline/docs/knowledge-base/jev/`.

| # | What | Status |
|---|---|---|
| H1 | **Client** `claude_hooks/decision_client.py`: both dialects; root / `/v1` / full path accepted; `/api/version` dialect probe (not cached when unreachable); Ollama 64 KiB body and loaded-window cut-and-retry with the size remembered per server and model; ≤ 26 options on Ollama; 429/529 retried inside one deadline; key from `key_file` or `TYPESAFE_API_KEY`, sent only to the endpoint it belongs to, never logged | done |
| H2 | **Config** `[decision]` (enabled, url, model, key_file, floor 0.6, high_stakes_floor 0.85, timeout_s 15, seams); off by default, no host defaults; seeded as `runtime_control.decision` only when on, with the key file's path, never the key; `claude-consultants config set-decision`, `claude-consultants decision-models` | done |
| H3 | **Seams** `consultants/engine/decision.py`, every one a no-op when off or failed: `review` (accept / followup / research on the final answer → `decision_review` state → `ConsultationResult.decision` → `show`), `confidence` (scored p beside the critic's and synthesizer's `CONFIDENCE:` line; the scored value goes on the channel the escalator, low-confidence interrupt and adversary checkpoint read), `struggle` (stuck-vs-progressing every 6 tool results; one harness nudge per lane at the high-stakes floor), `citations` (does the cited code support the claim, after the CitationLinter's existence checks; `[unsupported — …]` annotation) | done |
| H4 | **Bench** `benchmarks/consultants/decision_bench.py` over `questions/decision/cases.json` (25 council-shaped cases) | done, 2026-10-14 |
| H5 | The `/consultants` review loop reads `decision` from `show` as one input to accept / follow-up | skill text |
| H6 | Re-bench on cases mined from real `transcript.db` councils before any default changes; floors tuned per server (TypeSafe's and Ollama's confidence differ in definition) | open |

Owner decisions (2026-10-14): **Jev cloud for every seam for now** ("I
don't have sensible code that I don't want to leak out", adjust later);
first cut wires review, confidence, struggle and citations, **not** a
compaction check. Other Ollama decision models (e.g. clef-flash) are to be
benched by the owner; the client and bench take any decision-capable tag.

**Bench (2026-10-14, 25 cases × 2 repeats, floor 0.6):**

| target | accuracy | Brier | confident & wrong | median |
|---|---|---|---|---|
| Jev `jev-latest` | 50/50 | 0.017 | 0 | 262 ms |
| Jev `jev-preview` | 50/50 | 0.018 | 0 | 270 ms |
| `tev1:latest` (xOllama) | 44/50 | 0.114 | 0 | 144 ms warm |
| `nimble:latest` (xOllama) | 42/50 | 0.144 | 4 | 191 ms warm |

tev1's misses all sat under the floor (gated to "unsure"); nimble was
confidently wrong on a compaction-kept-a-fact case (p = 0.98). The cases
are hand-written and cleaner than real council output: Jev's 50/50 is a
ceiling, not an estimate (H6).

**Exit H:** the seams are inert when off (parity tests); every failure path
keeps today's behaviour; the bench is recorded; H6 before any default
changes.

---

## 7. Order and effort

| Milestone | Effort | Depends on |
|---|---|---|
| A — providers, caps, account, calibration, timings | L | test endpoints for xOllama/opencoti from the owner |
| B — budgets, window knob, thinking, sampling | M–L | A |
| C — compaction (reduced + full 5-step, condensation, overflow) | L | A4, B1–B3 |
| D — lane robustness | M (D1–D5 first) | A2, B3 |
| E — sandbox + shell | L (E1 S, E2–E4 L, E7 L) | E1 only; can run in parallel with C/D |
| F — PolyKV | L | A, B, C |
| G — typed role messages | M (G1–G3 first; the default flips only after G6) | A1 (format probe); independent of C–F, can run beside D |
| H — decision models (Jev / `/v1/systemone`) | M | independent; the struggle seam joins D2 when it lands |

Releases can cut between milestones; the owner said no release before this
work starts landing. Each milestone ships with:

- tests (a parity cohort, plus the milestone's own);
- skill and CLI knobs;
- a docs update (`docs/consultants.md`, `docs/consultants-roles.md`, a new `docs/consultants-providers.md`);
- a CHANGELOG entry.

---

## 8. Shareability (owner concern)

The owner reports that colleagues found claude-hooks hard to install and
maintain, because it grew around two hosts. This plan does not add to that.
All of its numbers come from probes and knobs, and the sandbox and providers
detect what a host has. Fixing the existing tailoring is a separate effort. It
is listed here so it isn't forgotten:

- a `doctor` that explains a host's resolved config;
- install profiles (cloud-only, local engine, full);
- moving host facts out of shipped defaults.

---

## 9. Not inherited (with reason)

- **Atomic-task / change protocol:** excluded by the owner (early phase).
- **xOllama council chat:** out of scope as a backend. Its mechanisms are taken as ideas in G.
- **Swarm agent nodes and priority tiers:** `ollama_slots` already covers account limits. PolyKV pools and `boot_id` generations **are** inherited (F), contrary to 05's "do not port".
- **The one-call fold as default:** xOllama's choice, not ours.
- **Shrink-on-retry, and global (non-keyed) calibration.**
- **Anything that waits on a human mid-run** (§0.7). Our own unwired `consultants/engine/interrupt_policy.py` (`review_before_synthesis`, the low-confidence pause, `POST /interrupt`) points the wrong way: delete it, or keep it strictly opt-in and never default. Cancel, inject, tool approval and the adversary checkpoint stay; they are the human surfaces, not guards.

---

## 10. Known bugs found by the map (fix inside the milestone that touches them)

| Bug | Where | Milestone |
|---|---|---|
| Calibration stores the count before its bounds check | `claude_hooks/token_calib.py:299-315` | A4 |
| Window = trained ctx for local models; `num_ctx` never sent; `ctx_max` unused | `chat_client.py:157-182`, `config.py:185` | B2 |
| Capped-think check against `num_predict` never fires at `high` | `agent_loop/runner.py:155-170` | B3 |
| Duplicate-call stub survives compaction | `agent_loop/runner.py:331-358` | C5 |
| `last_output_cap` / `consume_context_overflow` written, never read | `token_calib.py`, `council.py:1124` | C5 |
| Only the first question survives compaction | `budget.py:429-431` | C2 |
| `read_file` truncation reports the requested range | `caliber_proxy/tools.py:221-231` | D5 |
| `runtime_permissions` never reaches the gate; `control.py` enum mismatch | `tool_surface.py:100`, `control.py:55` | E1 |
| `task-link kind=mail/consultancy` refused — **fixed** 52749c5 | `claude_hooks/tasks/service.py` | done |

---

## 11. Decisions (all taken)

The owner ruled on the body of this plan on 2026-10-10 (providers, account,
96k output cap, window knob, thinking per backend, fixed level per (role,
model), calibration in full, the compaction rules, sampling on every role,
PolyKV last and complete, the sandbox requirements, atomic tasks excluded,
guards scoped for unattended runs, loop guards always on). The remaining items
were settled on 2026-10-14:

1. **Full 5-step compaction:** no detection. Reduced replay flow everywhere by default; the user enables the full flow per role (§3 C2). (2026-10-10)
2. **Compaction message role:** A/B `user` (fork order) against `system` (ours) on the C bench; the winner becomes the default.
3. **PolyKV role-instruction placement:** on PolyKV backends the role text moves out of the system turn (F4). An A/B on answer quality decides whether that layout becomes the default elsewhere.
4. **PolyKV pools for a finished run:**
   - held until the consultancy is accepted, or until an idle TTL expires (default 300 s, matching the engine's session TTL, configurable);
   - one P0 root per distinct toolset (read-only lanes vs coder/shell lanes).
5. **Sandbox defaults:**
   - network none, with an allowlist proxy on request;
   - held-view TTL 7 days, then kept as a patch;
   - quotas 2/8/32 GiB.
   - **Landlock is optional:** first try without the boot parameter and rely on the other layers. Activate it (a scheduled reboot) only if the escape suite shows a layer is needed.
6. **Milestone G approved** (typed role messages). The default still flips only after the G6 bench.
