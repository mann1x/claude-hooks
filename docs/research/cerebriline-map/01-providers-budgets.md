# Cerebriline → consultants: providers and per-request budget knobs

Slice 01 of the Cerebriline inheritance map. Covers model **providers**
(Ollama, xOllama-as-plain-provider, opencoti, llama.cpp sampling), **Ollama
account / cloud metadata**, and every **per-request budget knob**: output cap,
context window, thinking budget, token-estimate calibration, sampling and
parallel-agent diversification.

- Fork: `/shared/dev/cline` (`main` @ `400b75535`, 2026-10-10). Paths below
  are relative to `/shared/dev/cline` unless they start with `consultants/`,
  `claude_hooks/` or `config/` (those are this repo).
- Our side: this repo, `dev` @ `52749c5`.
- Method: read the vendor files whole (`ollama.ts`, `ollama-account.ts`,
  `llamacpp-sampling.ts`, `output-budget.ts`, `session-budget.ts`, both
  `gateway.ts`, `tokens.ts` to line 600, `retry-rate-limit.ts`,
  `spawn-sampling.ts`) and the relevant parts of `opencoti.ts`, `polykv.ts`,
  `xollama.ts`, `provider-option-rules.ts`, `compaction-shared.ts`,
  `provider-settings.ts`, `cline-session-factory.ts`. "Not found" means I
  grepped for it and it is not there.
- xOllama **council chat** (`council_chat_state`, `council_tags`, council
  tools) is out of scope and only mentioned where it gates plain-provider
  behaviour.

---

## 0. Headline findings

1. **Our context-window probe is wrong for local Ollama models.** We read
   `model_info["<arch>.context_length"]` (the *trained* window) for every
   model (`claude_hooks/get_advice/chat_client.py:157-182`, used by
   `consultants/engine/budget.py:267-285`). The fork uses the Modelfile's
   `parameters.num_ctx` first and accepts the trained window **only for cloud
   models**, because on a local model the trained window is a ceiling the
   runner may never load (`sdk/packages/llms/src/providers/vendors/ollama.ts:175-256`).
   We also never send `num_ctx` from the consultants engine (grep: no
   `num_ctx` under `consultants/`), so a local model runs at its Modelfile/server
   default while we budget it against the trained window. Every role today is a
   cloud model (`~/.claude/consultants-config.toml`), so the current impact is
   low. The bug is real for any local route.
2. **Output cap: flat 32,768 against a share of the window.** Ours is
   `MAX_OUTPUT_TOKENS = 32_768` (`consultants/engine/budget.py:74`). The fork's
   auto budget is `0.75 × ctx`, clamped to 96,000, the model's own max output and
   any user ceiling (`sdk/packages/shared/src/llms/output-budget.ts:36,49,78-102`).
   On top of that, the gateway learns a ceiling from a provider's refusal text and
   retries once (`sdk/packages/llms/src/providers/gateway.ts:125-203,963-996`).
3. **Calibration guards we lack.** Our `observe_request_tokens` keeps the
   provider count **before** any sanity check (`claude_hooks/token_calib.py:299-303`).
   The fork drops a count larger than the context window (2403f40e8) and a count
   whose ratio is under 1.2 chars/token (c61cb3600). It also refuses to learn a
   ratio from a residual under 20% of the count (`RESIDUAL_SIGNAL_SHARE`), and
   anchors each estimate on the last counted request rather than re-estimating
   the whole transcript (`sdk/packages/shared/src/llms/tokens.ts:279-463`).
   Nothing on our side reads `last_observed_request_tokens` yet, so the
   count-keeping bug is latent. Two more gaps: tool-loop calls never calibrate
   (only `_single_shot` calls `_observe_usage`, `consultants/engine/council.py:1219`),
   and our estimate leaves tool schemas out.
4. **Thinking levels mean budgets.** On think-budget Ollama builds a level is a
   share of `min(num_predict, num_ctx)`: max 4/5, high 1/2, medium 1/4, low 1/8,
   minimal 1/16. A level also **outranks** the model's own `think_budget`, so the
   fork sends a bare `think: true` when nobody chose a level
   (`sdk/packages/llms/src/providers/routing/provider-option-rules.ts:486-541`;
   commit 27ac1ba05). We always send a level (`DEFAULT_THINK_BY_ROLE`,
   `consultants/config.py:230-245`), and on a 400 we **strip `think` entirely**
   (`claude_hooks/get_advice/chat_client.py:505-529`). The fork documents that
   "with `think` absent a reasoning model thinks into `content` instead". Our
   capped-thinking detector compares against the whole `num_predict`
   (`claude_hooks/agent_loop/runner.py:162-168`), not the level's share, and the
   council never passes `thinking_budget_message` into `LoopConfig`, so the
   `/api/show` probe of it (`chat_client.py:417`) is unused by consultants.
5. **Ollama account.** The fork reads three endpoints through the *local*
   server: `POST /api/me` (account and plan), `GET /api/experimental/model-recommendations`
   (per-model `context_length`, `max_output_tokens`, `required_plan` and accepted
   thinking values) and `GET /api/tags` (`remote_host`/`remote_model` = cloud)
   (`sdk/packages/llms/src/providers/vendors/ollama-account.ts:1-27`). We already
   read the plan (signed directly to `ollama.com/api/me` with
   `~/.ollama/id_ed25519`) and turn it into account-wide connection slots
   (`claude_hooks/ollama_slots.py:61,129-183`). **We are ahead on slots.** The
   fork has only a per-profile `parallelSessions` (default 1, max 64). We do not
   read recommendations at all.
   Usage and quota endpoints: **not found in the fork either.**
6. **Diversifying identical-model agents.** The fork has an opt-in per-spawn
   sampler. `seed: "random"` draws from [0, 2^31). A numeric seed covering N
   agents becomes seed+index. `temperature: "random"` draws the model's own
   temperature ± `temperature_range`%, default **2%**, hard max **10%**, as
   `T = base × (1 + u × range/100)` with u uniform in [-1, 1], rounded to 3
   decimals (`sdk/packages/core/src/extensions/tools/team/spawn-sampling.ts:54-60,272-306,399-433`).
   We have no equivalent: `seed` is an allowed template key
   (`claude_hooks/model_sampling.py:53-55`) but nothing varies it per lane.
7. **429 / Retry-After.** The fork wraps every model in a rate-limit middleware
   that honours `Retry-After` (seconds or HTTP-date). Defaults: 4 attempts,
   ≤ 30 s per wait, 2 s when no delay is named
   (`sdk/packages/llms/src/providers/middleware/retry-rate-limit.ts:67-71,107-145`).
   Our `ChatClient` retries 429 on its own exponential schedule and ignores
   `Retry-After` (`claude_hooks/get_advice/chat_client.py:39-42,540-552`; grep
   for `Retry-After` under `claude_hooks/get_advice`, `consultants` and
   `agent_loop` finds nothing).
8. **opencoti / xOllama: no support on our side** (grep for `opencoti`,
   `xollama`, `X-Context-Window` and `/props` under `claude_hooks/` and
   `consultants/` finds only unrelated comments). Our llamafile client maps only
   `num_predict→max_tokens` and `temperature`, and drops `think`
   (`claude_hooks/get_advice/chat_client.py:1131-1158`). It sends no
   `reasoning_budget_tokens`, no sampler template and no seed.

---

## 1. Providers

### 1.1 Ollama (native `/api/chat`)

**Transport.** The fork uses the vendored `ollama-ai-provider-v2` with a patch:
`think` accepts a level, and the options schema gets a catchall so `think_budget`
survives the parse (`sdk/packages/llms/src/providers/vendors/ollama.ts:1-17`;
`patches/ollama-ai-provider-v2@4.0.1.patch`). It deliberately avoids
`/v1/chat/completions`, because that path ignores `options.num_ctx`
(`ollama.ts:11-17`).

**Base URL.** `normalizeOllamaBaseUrl` strips `/v1` or `/api` and appends `/api`
once (`ollama.ts:86-94`). Ours strips `/v1` only (`chat_client.py:243-244`).

**Auth.** `Authorization: Bearer <apiKey>` only when a key is configured
(ollama.com). Configured headers win (`ollama.ts:990-1003`).

**Capability probe: one `/api/show` per (server, model)**, awaited before the
first request so `num_ctx` is stable across turns (a change reloads the model).
`primeDeclaredNumCtx` (`ollama.ts:481-552`) caches five facts from the same
response:

| fact | parser | used for |
|---|---|---|
| `parameters.num_ctx` | `parseDeclaredNumCtx` `ollama.ts:453-468` (regex `^num_ctx "?(\d+)"?$` over the text blob) | `num_ctx` on the wire and the budget window |
| `details.family` | `parseDeclaredFamily` `ollama.ts:259-273` | prompt-template family (a renamed quant is still its GGUF arch) |
| `model_info["*.context_length"]` | `parseDeclaredTrainedCtx` `ollama.ts:203-220` | window **for cloud models only** |
| `parameters.temperature` | `parseDeclaredTemperature` `ollama.ts:298-314` | read-only base for `temperature:"random"` |
| `parameters.think_budget_message` | `parseDeclaredThinkBudgetMessage` `ollama.ts:340-361` (unquotes, decodes `\n \t \r`) | capped-thinking marker |

Failure caches `null` and falls back silently (`ollama.ts:506-541`). The account
status is primed alongside, once per server root (`ollama.ts:499`, see §2).

Ours: one `/api/show` per model per `ChatClient`, harvesting `capabilities`
(for the think verdict), `*.context_length` and the budget message
(`chat_client.py:340-415`, `_budget_message_from_show` at 198-227). We do
**not** read `parameters.num_ctx`, `details.family` or `parameters.temperature`.

**Reasoning re-injection probe.** `primeOllamaReinjection` makes two
`/api/chat` calls with `num_predict: 1`, once per model per process, to learn
whether the template re-renders `thinking` (`ollama.ts:1061-1113`). Adjacent to
this slice; our side has no equivalent (not found).

**Request options.** Built by the `provider.ollama.native-options` rule
(`provider-option-rules.ts:508-541`):
`options = { num_ctx: readOllamaNumCtx(ctx), num_predict?: request cap, ...configured sampler }`.
The user's sampler is spread **after** `num_predict`, so a configured
`num_predict` wins (`:530-536`). A bare `think: true` goes in the provider
bucket when no level was named (`:523-528`). A named level stays top level
(`buildOllamaStreamConfig`, `ollama.ts:1297-1324`).

**Sampler wire names.** `temperature`, `top_k`, `top_p`, `min_p`, `typical_p`,
`repeat_last_n`, `repeat_penalty`, `presence_penalty`, `frequency_penalty`,
`seed`, `num_predict`, `num_keep`, `num_gpu`, `stop`, `think_budget`,
`think_budget_message` (`ollama.ts:917-934`). Only fields that are set are sent;
an empty string or `stop: []` is dropped (`ollama.ts:945-974`). Ours applies
`config/model-sampling.json` templates under explicit options and drops explicit
`None`s (`chat_client.py:857-866`). Our allow-list lacks `num_keep`, `num_gpu`,
`stop`, `think_budget` and `think_budget_message` (`claude_hooks/model_sampling.py:53-55`)
and has `mirostat*`, which the fork lacks.

**Timeouts and streaming liveness.** Time to first byte defaults to 300,000 ms
(`OLLAMA_DEFAULT_TIMEOUT_MS`, `ollama.ts:619`), but it is a **probe interval**,
not a deadline. On silence the client asks `/api/ps`, and dies only after 3
unanswered probes (`ollama-stall-watchdog.ts:26` = 30,000 ms interval,
`:29` = 10,000 ms probe timeout, `:38` = 3 failures; wiring at
`ollama.ts:807-907`). undici's `bodyTimeout`/`headersTimeout` are disabled
through an injected dispatcher, and Bun's with `timeout:false`
(`ollama.ts:645-795,869-894`). Ours: a flat `timeout_s=600` socket read
(`chat_client.py:22,477`) plus the consultants stall monitor (other slice).

**Error / retry.** Empty responses are retried by the shared middleware: 3
attempts, 250 ms apart, 2 s before the first network-interruption retry
(`middleware/retry-empty-response.ts:98-102`). 429s are retried by the
rate-limit middleware (§1.5). Both wrap every vendor (`ai-sdk.ts:2818-2826`).
For a retried turn, the accepted attempt's own prompt count travels as
`requestInputTokens` beside the billed sum (`ai-sdk.ts:1600-1606,2365`; commit
9d7cf0b6f).

### 1.2 opencoti (llama.cpp + KV control plane)

**Transport.** `@ai-sdk/openai-compatible` at `<base>/v1`
(`normalizeOpencotiBaseUrl`, `opencoti.ts:2202-2210`). The API key defaults to
the literal `"opencoti"` (`:2368`). The error schema is a `looseObject`, so
`error_kind`, `largest_admissible` and `pressure` survive
(`opencoti.ts:2225-2270`). Timings are read from the final `timings` frame
(`llamacpp-timings.ts:34`).

**Capability probe: `GET /props`, once per root, bounded**
(`polykv.ts:1054-1112`). Fields (`polykv.ts:805-849`): `build_info` → release
(unreliable; never branch on it, `:1016-1039`); `opencoti.polykv.pools_enabled`;
`opencoti.elastic_slots.{enabled, slots_live, slots_max}`; `chat_template`;
`features[]`; `default_generation_settings[.params].temperature`. Branches key
only on `features` (`OPENCOTI_FEATURES`, `polykv.ts:872-969`, 25 flags). The
ones that touch budgets are `elastic_guaranteed_alloc_v1` (`num_ctx` books a
window), `ctx_min_negotiation_v1` (server-side floor), `polykv_private_window_v1`
(the shared prefix rides above `num_ctx`), `kv_pressure_v1`, `kv_resize_v1`
(+ `_deferred_v1`, `_live_v1`) and `stream_keepalive_v1`.

**Body extras added by the fetch wrapper** (`createOpencotiFetch`,
`opencoti.ts:399-822`): `pool_id` (number), `session_id`,
`shared_prefix_n_tokens`, `overcommit`, `num_ctx` (only where
`guaranteedAlloc` is advertised), `num_ctx_min` (only with atomic negotiation;
otherwise the floor stays client-side), `continue_pool` (only new turns are sent
and tools are dropped, `:2393-2403`), and `stream_options.keepalive` +
`sse_ping_interval: 10` (`opencoti-liveness.ts:44,162-185`; dead after 3 silent
periods + 5 s margin, `:47,54`; silent-alive ceiling 600,000 ms, `:74`).

**Window granted.** `X-Context-Window` on every admitted response. **Absent
means unknown, not unchanged** (`opencoti.ts:283-292,786-801`). The resume rule:
a session already granted a window asks for exactly that window, floored at
itself (`resolveOpencotiWindow`, `opencoti.ts:2149-2178`).

**429 handling.** A window refusal carries `x-context-largest-admissible` or a
body `largest_admissible`. Non-atomic servers get one retry at that value if it
is ≥ the floor. Below the floor, a new session waits once (`Retry-After`, capped
by `maxRetryAfterMs`, default 30,000; 2,000 ms when the server named none) and
then refuses. A resume refuses at once (`negotiateWindow`, `opencoti.ts:956-1052`).
A 429 that is not a window refusal is handed to the rate-limit middleware. The
**status line is trusted, not the body**: c7 sends 429 with a body saying 503
(`opencoti.ts:720-736`).

**Worker output-cap fallback** when the request has none: the model's
`maxOutputTokens`, else `max(1024, ctx/4)`, else 8,192
(`resolveWorkerMaxTokens`, `opencoti.ts:2117-2127`).

**Profile knobs** (`config.ts:441-496`; zod in
`core/src/services/llms/provider-settings.ts`): `dynamicContextSize`
(default **off**: nothing is booked and the engine decides), `contextFloor`,
`maxRetryAfterMs`, `overcommit`, `pinPrefix`, `continuationCompaction`
(default on where advertised), plus admission policy fields.

### 1.3 xOllama as a plain provider (opencoti engine underneath)

**Same Ollama vendor**, `providerId === "xollama"`, default base URL
`http://localhost:22434` (`xollama.ts:68`; `ollama.ts:996-998`).

**Probes:**
- `GET /api/xollama` → `{xollama:true, version, features[]}`. A stock Ollama
  404s. Gate on feature names, never on the version (a dev build reports
  `0.0.0`) (`xollama.ts:7-9,230-266`). A failed read is not cached.
- `/api/show` → `xollama.council.enabled` and `xollama.session.client_pools`
  (`xollama.ts:271-312`).
- `xollamaDrivesModel` = not a council model AND `client_pools > 0` AND
  `context_window_v1` (`xollama.ts:321-336`).

**Wire additions** (`withXollamaRequestFields`, `xollama.ts:1130-1469`):
- top-level `session_id` (from the `x-cerebriline-engine-session` header,
  `:75`);
- `x_read_only` on read-only tools (`:78`);
- top-level `placement: {pool_id?, num_ctx?, num_ctx_min?}` (`:1066-1071`).
  The window is booked through `placement`, **never through `options.num_ctx`**,
  because that is the model load and changing it reloads the runner
  (`xollama.ts:102-103`).

The granted window comes back in `X-Context-Window` (`:1437-1454`). The window
ask follows the same rules as opencoti (`windowAsk`, `xollama.ts:141-172`).

**Engine control plane.** `/api/engine?model=M&endpoint=<route>`, mapped to a
per-model pseudo-root `/xollama-engine/<model>`. `/apply-template` becomes a
render-only chat (`xollama-engine.ts:1-45`).

**Side calls** (summaries, capped-thinking notes) get an exact booking of
`tokenize(prompt).length + num_predict + 256`, so a refusal is answered at once
instead of after xOllama's two-minute wait. The idle conversation's session is
closed in exactly one case (`xollama.ts:822-1064`, `SIDE_CALL_SLACK_TOKENS = 256`
at `:842`).

**Auth.** `Authorization: Bearer <local key>` on every same-origin call
(`withXollamaAuth`, `xollama.ts:352-386`).

### 1.4 llama.cpp sampling (shared by opencoti and generic openai-compatible)

- Wire names: `temperature`, `top_k`, `top_p`, `min_p`, `typical_p`,
  `repeat_last_n`, `repeat_penalty`, `presence_penalty`, `frequency_penalty`,
  `seed`, `n_predict`, `n_keep`, `stop`, `reasoning_budget_message`. No
  `num_gpu` (`llamacpp-sampling.ts:30-45`).
- **Unset fields are never sent**, so a hosted endpoint on the same path is
  byte-identical (`:4-19`).
- `reasoning_budget_tokens` is computed (§5). `enabled:false` → `0` (the
  engine's floor) (`:244-249`).
- Rule `provider.llamacpp.native-options` (`provider-option-rules.ts:563-619`).
  It falls back to `DEFAULT_GATEWAY_MAX_OUTPUT_TOKENS` (32,000) as the
  `numPredict` the level is a share of (`:595-598`).

### 1.5 429 / Retry-After layer (all vendors)

`createRetryRateLimitMiddleware` (`middleware/retry-rate-limit.ts:154-223`):
- Classifies by HTTP status alone (`classifyProviderError(...) === "rate_limited"`;
  429 or a "rate limit"/"per minute" message, `error-classification.ts:74-78,330-338`).
- Reads `Retry-After` from `responseHeaders`/`headers` down the `cause` chain
  (depth 8). It accepts integer seconds or an HTTP-date; a past date means 0 and
  an unparseable value counts as unnamed (`:107-145`).
- Waits `min(maxRetryAfterMs=30,000, asked ?? 2,000)`, up to 4 attempts in
  total, abort-aware (`:67-71,186-211`).
- `maxRetryAfterMs` comes from the profile's PolyKV section (`ai-sdk.ts:2537-2555`).

Ours (`chat_client.py:39-42,531-552`): 15 retries, 1.5 s × 2^n up to 90 s, on
`{408,429,500,502,503,504}` plus known-flaky 4xx bodies, an outage hold
(`is_outage`, `:67-77`), and slots released during backoff (`:473-475`). No
`Retry-After`. Our caliber proxy and API proxy have their own layers (out of
slice).

---

## 2. Ollama account and cloud model metadata

**Fork** (`sdk/packages/llms/src/providers/vendors/ollama-account.ts`):

| endpoint | auth | parsed into | where |
|---|---|---|---|
| `POST <server>/api/me` | none from the client. The local server signs it with `~/.ollama` (`:6-9`) | `{reachable, signedIn, plan, name, email, signinUrl}`. Go field names `Name/Plan/Email` and snake_case are both accepted. 401 → not signed in + `signin_url`. Non-200 → unreachable (503 is "could not check"). A missing plan defaults to `"free"` | `parseOllamaWhoami` `:137-171` |
| `GET <server>/api/experimental/model-recommendations` | none (served from a local cache) | per model `{model, description, contextLength, maxOutputTokens, requiredPlan, thinkingValues[], thinkingDefault, vramBytes}`. `false`→`off`, `true`→`on`. Local entries have `context_length:0` = "model decides" and carry `vram_bytes` | `parseOllamaRecommendations` `:174-219` |
| `GET <server>/api/tags` | none | `{name, cloud = remote_host‖remote_model, remoteHost, remoteModel, family, capabilities}` | `parseOllamaCatalog` `:259-288` |

- The three reads run in parallel and each stands alone (`:372-407`).
- Cached per API root, once per process; no TTL, reset only via
  `resetOllamaAccountStatus` (`:416-458`).
- **Cloud discriminator** = `remote_host`. The `:cloud`/`-cloud` name rule is a
  fallback only (`:17-27,297-299,466-479`).
- **Unpulled cloud recommendations** are offered as usable models, because the
  server fetches the manifest on first use (`:235-256`; reachability probe
  `ollama-reachability.ts:149-184`).
- **Recommendation matching** tries `remote_model` before the tag name, then a
  stem match without the `:cloud` suffix (`:313-336`).

What it feeds:
- the cloud **context window** (`readResolvedOllamaWindow`, `ollama.ts:241-256`:
  recommendation `context_length`, then the trained ctx);
- the settings strip: plan, "requires pro" shortfall, thinking values, max
  output (`webview-ui/.../OllamaAccountStrip.tsx:55-120`; controller
  `apps/vscode/src/core/controller/models/readOllamaAccount.ts:17-62`).

**`maxOutputTokens` and `thinkingValues` from recommendations are display-only.**
grep for `readOllamaRecommendation(` finds only the `contextLength` use
(`ollama.ts:253`). Output ceilings are instead learned from refusals (§3).

Not found in the fork: usage, quota or limit endpoints, per-plan connection
counts, `whoami` outside `/api/me`. The closest knob is
`parallelSessions` per profile: 1..64, default 1, a ceiling on delegated agents
and lifted on elastic or PolyKV opencoti (`sdk/packages/llms/src/providers/agent-slots.ts:22-140`;
schema `provider-settings.ts:307-317`).

**Ours** (`claude_hooks/ollama_slots.py`):
- Plan from `https://ollama.com/api/me?ts=<t>`, signed directly with
  `~/.ollama/id_ed25519` (needs `cryptography`). Only `Plan` is kept, cached a
  day in `~/.claude/ollama-plan.json` (`:125-169`).
- `PLAN_CONNECTIONS = {free:1, pro:3, max:10}`, minus `reserve_for_hooks=1`
  (`:61-64,172-183`).
- Cross-process file-lock slots (`:186-339`).
- Cloud detection via `/api/tags` `remote_host`, with a 600 s TTL and the name
  rule as fallback (`:95-120`).

We do not read recommendations, `signin_url`, `required_plan` or thinking values.

---

## 3. Output cap knobs

### Fork: resolution order

`resolveSessionOutputCap` (`core/src/services/llms/session-budget.ts:66-88`)
takes the first of:
1. `sampling.numPredict`;
2. per-turn `maxTokensPerTurn`;
3. `outputBudget.mode == "manual"` + `maxTokens`;
4. **auto**: `resolveOutputBudgetTokens` (`shared/src/llms/output-budget.ts:78-102`):
   - `manual` + value → `floor(min(value, 96,000))`. Not clamped to the model's
     max, on purpose;
   - no window → `undefined`;
   - otherwise `max(1, floor(min(ctx × 0.75, min(userMax, 96,000), modelMaxOutput)))`;
5. the gateway default `resolveDefaultMaxOutputTokens`
   (`llms/src/providers/gateway.ts:96-109`): if the model publishes a max output
   **or** has no window → 32,000; else `max(1024, floor(ctx × 32000/128000))`
   (a share of 0.25).

The same resolver runs for the CLI (`provider-settings.ts:422-439`, commit
e64572779), so the CLI and the plugin send the same cap.

Constants: `OUTPUT_BUDGET_CEILING_TOKENS = 96_000` (`output-budget.ts:36`; was
512,000 until commit 178c27320), `OUTPUT_BUDGET_AUTO_WINDOW_SHARE = 0.75`
(`:49`), `DEFAULT_GATEWAY_MAX_OUTPUT_TOKENS = 32_000` (`gateway.ts:44`).

### Fork: gateway clamp per request

`resolveGatewayOutputCap` (`gateway.ts:538-669`):
- Candidate caps:
  - `requested`, or else `default` (session default or synthesized, lifted to
    `reasoningBudget + reserve` when a reasoning budget is set);
  - `model-max-output`;
  - learned ceiling;
  - `remaining-context` = `ctx − estimatedInput − reserve`.
- **Reserve** = `max(1024, ceil(estimatedInput × 0.12))`
  (`OUTPUT_RESERVE_ESTIMATE_ERROR_SHARE`, `:448-464`). The 0.12 is a measured
  8.9% estimate error plus margin.
- If `remaining < GATEWAY_MIN_OUTPUT_TOKENS (1024)`, the request **keeps its own
  cap** (the smallest non-window candidate), is marked `context-overflow`, and an
  overflow report is left for compaction (`:582-633`).
- Ties go to `remaining-context`, so the window is blamed (`:644-655`).
- The winning source is recorded (`noteOutputCap`) for the truncation verdict
  (`:998-1004`).

### Fork: learned ceilings

`OUTPUT_CEILING_PATTERNS` (`gateway.ts:125-135`):
- Ollama Cloud / Z.ai: `maximum output tokens\s*\((\d+)\)`;
- Anthropic: `max_tokens:\s*\d+\s*>\s*(\d+)`;
- OpenAI: `supports at most\s*(\d+)\s*completion tokens`.

On a refusal of a **synthesized** cap (never a user's), it retries once at the
named ceiling and remembers it per `(provider, model)` for the process
(`:203,963-996`). The first stream event is primed so the refusal can be caught
before anything has reached the caller (`:219-236`).

### Fork: wire names

- Ollama/xOllama: `options.num_predict` (`readOllamaNumPredict`,
  `ollama.ts:586-601`; rule `provider-option-rules.ts:516,533`).
- opencoti/llama.cpp: `max_tokens` from the AI SDK, plus `n_predict` if
  `sampling.numPredict` is set (`llamacpp-sampling.ts:41`). The wire readers
  accept `max_tokens`, `max_completion_tokens` or `n_predict`
  (`opencoti-agent-window.ts:80-91`; `opencoti.ts:1979-1984`).

### Fork: prompt statement

`buildOutputBudgetSection` (`output-budget.ts:137-166`) appends the following
section verbatim (the wording is the behaviour):

> `# Output Budget` — "Each reply you produce is capped at N tokens, thinking
> included. Anything past the cap is cut off mid-sentence and the turn is
> wasted." [+ "Of that, at most T tokens may be spent thinking (effort L);
> reasoning past that point is cut short, so reach a decision inside it and
> write the answer with what is left."] [+ if cap ≥ 0.9×ctx: "keep each reply
> under ⌊0.75×cap⌋ tokens and leave the remaining 25% free for compaction",
> else "The context window is W tokens."] + "Prefer several focused tool calls
> over one oversized reply: …continue in the next turn."

It is idempotent on the `# Output Budget` heading (`:186-205`).

### Fork: UI and settings

- `outputBudget {mode: auto|manual, maxTokens}` (`provider-settings.ts:318-338`).
- The webview `OutputBudgetField.tsx` duplicates `AUTO_WINDOW_SHARE = 0.75` and
  `CEILING_TOKENS = 96_000` (`:17-18`) in a slider shown as a % of the auto
  figure.
- `sampling.numPredict` sits in the advanced sampler panel.

### Ours

`consultants/engine/budget.py`:
- `ask = 32,768` (`MAX_OUTPUT_TOKENS`, `:74`), or the caller's `target_output`.
- `MIN_OUTPUT_TOKENS = 4096` (`:79`); flat `WINDOW_MARGIN_TOKENS = 2048` (`:83`).
- An unknown window sends the ask (`:313-318`).
- Overflow → `MIN_OUTPUT_TOKENS` + compaction (`:325-340`).
- `retry_cap` halves (`:308-311`), but `_single_shot` never passes it
  (continuations by design, `council.py:1258-1278`).
- Sent as `options.num_predict` (`council.py:1193`, `:1145-1148`).

There is no output-budget section in our role prompts (grep for
`Output Budget` under `consultants/` finds nothing), and we learn no ceiling from
refusals.

---

## 4. Context window (`num_ctx`)

### Fork, Ollama

`readOllamaNumCtx` (`ollama.ts:102-115`) takes the first of:
1. `model.contextWindow ?? maxInputTokens`, i.e. the user/profile setting
   (factory `cline-session-factory.ts:918-974`: providers.json `contextWindow`,
   then the legacy `ollamaApiOptionsCtxNum`, then the declared value, then 32,768);
2. `readResolvedOllamaWindow`: Modelfile `parameters.num_ctx`, then **(cloud
   only)** recommendation `context_length`, then trained `*.context_length`
   (`ollama.ts:241-256`);
3. `OLLAMA_DEFAULT_NUM_CTX = 32768` (`builtins.ts:100`).

The **same number** is sent as `options.num_ctx` and used for compaction and the
output budget. The fork notes that the two drifted apart "once per host every
time they were not" (`ollama.ts:237-240`).

### Fork, opencoti and xOllama

- `num_ctx` / `num_ctx_min` are **booked windows**, sent only with
  `dynamicContextSize` on (default off) or with an agent-window share
  (`opencoti.ts:2149-2195`; `xollama.ts:141-172`).
- The grant is read from `X-Context-Window`. The resume rule asks for
  grant = floor.
- The agent-window floor = `minimum + share × (window − minimum)`, where minimum
  = system tokens + tool-schema tokens + output cap measured off the body
  (`opencoti-agent-window.ts:93-128`).
- With dynamic sizing off, nothing is booked and the engine's admission/pressure
  decides. Pressure reads are `GET /kv` `pressure` and `error.pressure` on 429,
  with a 60 s window (`opencoti-kv-pressure.ts:70,85-195`). Live resize is
  `POST /kv/sessions/{id}/resize` (`:720`).
- Owner window = `GET /kv` `session_ctx_max` (`polykv-swarm.ts:2013-2027`).

### Ours

- `budget.context_length_of` → `ChatClient.context_length` → trained
  `*.context_length` for every model (`chat_client.py:157-182,397-415`).
- `RoleConfig.ctx_max` is stored and shown (`consultants/config.py:185-186`;
  `consultants/server/app.py:2191`) but **not used** by the engine.
- `LoopConfig.num_ctx_override` exists (`agent_loop/runner.py:63,409-415`), but
  grep shows the consultants engine never sets it.
- `/get-advice` sends `num_ctx=cfg.ctx_max` only when it was explicitly set,
  because cloud tags 500 on an explicit `num_ctx`
  (`claude_hooks/get_advice/cli.py:239-248`). That claim does not appear in the
  fork. The fork sends `num_ctx` for cloud models too (rule `:529`), so at least
  current Ollama accepts it. Not verified here.

---

## 5. Thinking budget

### Level → fraction table

The same table is used by Ollama server-side and by the fork client-side for
llama.cpp (`llamacpp-sampling.ts:61-69`, ported from Ollama `api/types.go`
`thinkBudgetFraction`):
`max 4/5 · high 1/2 · medium 1/4 · low 1/8 · minimal 1/16`, with the alias
`xhigh→max` (`:78-80`).

The share is taken of `min(num_predict, num_ctx)`, or of `num_predict` when
there is no ctx, or of `ctx` when there is no cap (`resolveLlamaCppThinkBudgetWindow`,
`:97-107`). The result is floored, and ≤ 0 means no budget (`:137-143`).

### Ollama (local think-budget builds)

- The level goes on the wire as top-level `think: "<level>"`, and the server
  computes the budget.
- **Unnamed → bare `think: true`**, which leaves the model's own
  `PARAMETER think_budget` in force. The measured table at `num_predict` 8,000,
  Ollama 0.34.2 (`provider-option-rules.ts:493-501`):

  | model declares | `think` absent | `think: true` | `think: "medium"` |
  |---|---|---|---|
  | nothing | unbounded | unbounded | 2,000 |
  | `think_budget "high"` | 4,000 | 4,000 | 2,000 |

- Explicit off → `"none"`. `sampling.thinkBudget` → `options.think_budget` (a
  level or a count).
- The number the server will enforce is **read back** by asking `/api/show` with
  `{model, think, options:{num_predict, num_ctx}}` → `think_budget_tokens`
  (+ `think_budget` level). The probe makes 3 attempts with timeouts of
  2/4/6 s and 250/500 ms backoff (`probeOllamaThinkBudget`, `ollama.ts:381-450`).
  An older build answers without the field → `undefined`.
- The default level for the read-back is `OLLAMA_DEFAULT_REASONING_EFFORT = "medium"`
  (`ollama.ts:1259`; `session-budget.ts:125-126`).

### Ollama Cloud

Not found: any client-side cloud-specific thinking mapping. The cloud flag
affects only the window. Per-model accepted values come from recommendations
`thinking.values` (e.g. `[false,"low","high","max"]` or `[false,true]`,
`ollama-account.ts:55-63`) but are display-only. Whether Ollama Cloud enforces
`think_budget` is not established in the fork.

### opencoti / llama.cpp

- `reasoning_budget_tokens` = explicit `sampling.thinkBudget` (a count or a
  level) > the request effort level; `enabled:false` → 0; nothing set → **not
  sent (unbounded)** (`llamacpp-sampling.ts:236-255`).
- `reasoning_effort` is discarded by the engine (measured, `:160-168`;
  commit 1153b739c).
- Budget message: `sampling.thinkBudgetMessage` → `reasoning_budget_message`.
- Observation, not verified: `resolveSessionThinkingAllowance` states a
  `medium` budget in the prompt when nothing was chosen (`session-budget.ts:125-136`),
  while the wire rule sends a budget only for a configured or effort level
  (`provider-option-rules.ts:603`). Whether `portableReasoning` is always set
  there was not traced.

### xOllama

Same as Ollama (native wire). No xOllama-specific think field was found outside
council chat.

### Budget message text

The fork ships **no default text**. Order: configured
`sampling.thinkBudgetMessage` (sent on the wire, overrides the Modelfile), then
the model's own `PARAMETER think_budget_message` from `/api/show`
(`session-budget.ts:154-167`; `cline-session-factory.ts:1918-1933`).

### Per-role / per-model levels

- Fork: a per-profile `reasoning.effort` (`provider-settings.ts:485-487`) plus
  per-spawn overrides. Auxiliary calls (summarizer, retrospective) run with
  `thinking: false, auxiliary: true` (`compaction-shared.ts:2177-2191`). No
  per-role default table was found.
- Ours:
  - `DEFAULT_THINK_BY_ROLE = {planner: medium, researcher: high, tool_executor: False, critic: medium, coder: high, synthesizer: high}`
    (`consultants/config.py:230-245`);
  - `role_think` falls back to `True` (`:343-352`);
  - `_merge_role` accepts only bool / `low` / `medium` / `high`, so `max`,
    `minimal` and integer counts are silently ignored (`:858-865`);
  - the runner also sets `reasoning_effort = <level>` (`agent_loop/runner.py:400-407`).
  - The live config sets no `think` overrides (`~/.claude/consultants-config.toml`;
    nothing is copied here because that file carries a DSN).

---

## 6. Token-estimate calibration (exact algorithm)

All of this is in `sdk/packages/shared/src/llms/tokens.ts`.

### Constants

| constant | value | line |
|---|---|---|
| `CHARS_PER_TOKEN` | 3 | `:22` |
| `THINKING_CHARS_PER_TOKEN` | 2.7 | `:48` |
| `RESIDUAL_SIGNAL_SHARE` | 0.2 | `:69` |
| `MIN_OBSERVED_CHARS_PER_TOKEN` | 1.2 | `:70` |
| `MAX_OBSERVED_CHARS_PER_TOKEN` | 16 | `:71` |
| `OBSERVATION_WEIGHT` (EMA) | 0.3 | `:74` |

The gateway adds `MOSTLY_REASONING_SHARE = 0.8` (`gateway.ts:52`) and
`OUTPUT_RESERVE_ESTIMATE_ERROR_SHARE = 0.12` (`:448`). Compaction adds
`OUTPUT_CAP_ESTIMATE_TOLERANCE = 1.25` (`core/src/extensions/context/compaction.ts:687`).

State lives on `globalThis[Symbol.for("cline.shared.tokenCalibration")]` so that
bundled copies share it (`:76-97`). It is **owner-scoped**: a record's
session id must match the reader, and an unknown id on either side matches
(`:112-136`).

### Measurement

- `measureRequestInputChars` = `JSON.stringify({systemPrompt, messages, tools}).length`,
  counting only the reasoning the provider will actually be sent
  (`withSentReasoningOnly` by `reasoningHistory`; Ollama → none) (`:786-813`).
- `measureRequestReasoningChars` = serialized reasoning parts (`:763-784`).
- The gateway measures what the provider **will send**, after resolving the
  reasoning-history mode (`gateway.ts:839-861`).

### Estimate: `anchoredRequestTokens(chars, reasoningChars, reader)` (`:279-346`)

1. `plain(total, r)` = `ceil((total−r)/cpt) + ceil(r/tcpt)` when `0<r<total`,
   else `ceil(total/cpt)`. Each term is `max(1, …)`.
2. No anchor (`requestTokens`/`requestTokensChars` absent or owned by someone
   else) → `plain`.
3. `delta = chars − anchorChars`.
   `blended = delta==0 ? anchorTokens : max(1, round(anchorTokens + delta / (anchorChars/anchorTokens)))`.
4. If `tcpt ≤ 0` or (`anchorReasoning ≤ 0` and `reasoningChars ≤ 0`) → `blended`.
5. `anchorContentTokens = anchorTokens − anchorReasoning/tcpt`,
   `anchorContentChars = anchorChars − anchorReasoning`. If either ≤ 0 → `blended`.
6. `contentRatio = anchorContentChars/anchorContentTokens`,
   `reasoning = min(chars, max(0, reasoningChars))`,
   `contentDelta = chars − reasoning − anchorContentChars`.
   Return `max(1, round(anchorContentTokens + contentDelta/contentRatio + reasoning/tcpt))`.

This is used for the output-cap window term (`gateway.ts:870-874`).

### Observation: `observeRequestTokens(chars, tokens, reasoningChars, owner, ctx)` (`:371-463`)

1. Non-finite or ≤ 0 → ignore.
2. **`tokens > ctx` → ignore both count and ratio** (commit 2403f40e8, measured
   166,848 against a 131,072 window).
3. `reasoning = min(reasoningChars, chars)`. `ratio = chars/tokens`. If
   `0<reasoning<chars` and `rem = tokens − ceil(reasoning/tcpt) > 0`, then
   `ratio = (chars−reasoning)/rem`.
4. **If `reasoning>0` and `tokens − ceil(reasoning/tcpt) < 0.2 × tokens`**,
   store the anchor (count, chars, reasoning, owner) but **do not** update the
   ratio (`RESIDUAL_SIGNAL_SHARE`, `:421-437`).
5. **`ratio < 1.2` → return without storing count or ratio** (commit c61cb3600:
   a "token per character" count is impossible; measured 138,549 tokens for
   138,262 characters).
6. Store the anchor. If `ratio > 16`, return (keep the count, refuse the ratio).
7. `cpt = cpt===undefined ? ratio : 0.7·cpt + 0.3·ratio`.

### Other calibration paths

- **Reasoning ratio.** `observeThinkingTokens(reasoningOutChars, outputTokens)`
  runs only when `reasoningOutChars > 0.8 × outputChars`, with the same
  1.2..16 bounds and EMA (`tokens.ts:234-254`; caller `gateway.ts:1092-1113`).
- **Anchor source on retries.** `usage.requestInputTokens` (the accepted
  attempt's prompt) when present, else `inputTokens` (`gateway.ts:1072-1091`;
  commit 9d7cf0b6f).
- **Only conversation requests calibrate** (`request.conversation`), so
  auxiliary calls cannot overwrite the anchor (`shared/src/llms/gateway.ts:276-320`;
  `llms gateway.ts:1012-1023`).
- **Resume seeding.** `seedRequestTokenCalibration` adopts a count reconstructed
  from the transcript, only while nothing has been measured live (`tokens.ts:483-492`).
- **Starved-cap trust check.** A window-bound cap whose estimate exceeds the
  provider's count by more than 1.25× does not force compaction
  (`compaction.ts:687,1150-1175`; commit 9d7cf0b6f).

### Ours (`claude_hooks/token_calib.py`)

The same base constants (`:74-87`). Two improvements on the fork:
calibration is per model (`_states`, `:111-121`), which suits x-tier fan-out,
and the state is lock-guarded. Differences:

- the count is stored **before** any guard, and there is no window guard, no
  sub-floor drop and no residual guard (`:299-315`);
- no anchored estimate: `estimate_request_tokens` is always the plain split
  (`:383-393`);
- `measure_request_chars` serializes `{system, messages, tools}`, but
  `budget.estimate_messages_tokens` passes messages only, **without tools**
  (`consultants/engine/budget.py:145-156`), and `_observe_usage` measures
  messages only (`council.py:1057`). So the ratio is learned against counts that
  include tool schemas, while the chars exclude them;
- the "mostly reasoning" threshold is 0.6 (`council.py:1066`) against the
  fork's 0.8;
- `_observe_usage` runs only on `_single_shot` (`council.py:1219`). Tool-loop
  iterations (`_loop_payload_budgeter`, `:1128-1149`) never calibrate;
- prompt counts are summed across continuations for accounting (`:1211-1213`),
  but calibration is fed per call, so the retry-sum bug (9d7cf0b6f) does not
  apply here;
- no reserve proportional to the estimate: flat 2,048 against
  `max(1024, 0.12×est)`.

---

## 7. Sampling and parallel-agent diversification

### Fork, defaults

Nothing is invented. An unset field is never sent, so the Modelfile or
llama.cpp sampler stands (`ollama.ts:936-974`; `llamacpp-sampling.ts:4-19`;
schema `provider-settings.ts:103-134`). A lift bug where `sampling` never reached
the wire was fixed in `handler-factory.ts:59-73`. No per-model temperature table
was found in the fork. Our `config/model-sampling.json` (glm-5.3*,
deepseek-v4.1-flash* → 0.7) cites Cerebriline prompt-template work as its source
(`config/model-sampling.json:6-12`).

### Fork, diversification (`core/src/extensions/tools/team/spawn-sampling.ts`)

It is opt-in per spawn. The lead passes `temperature`, `seed` and
`temperature_range`; spawn tool schema at `:121-142`.

| knob | behaviour | constant / line |
|---|---|---|
| `seed: <int>` covering N agents | agent *i* gets `seed + i` | `samplingForCopy` `:223-236` |
| `seed: "random"` | `floor(rand × 2^31)` per agent, drawn once (a re-placement keeps it) | `RANDOM_SEED_LIMIT = 2**31` `:60`, `:280-285` |
| `temperature: "random"` | base = model's own temperature: connection `temperature` → `providerConfig.sampling.temperature` → Ollama Modelfile `temperature` (`/api/show`) → opencoti `/props` `default_generation_settings[.params].temperature` (`modelTemperatureOf` `:335-352`). Unknown base → temperature left unset, with the note "model temperature unknown; kept the model's sampler" (`:63-64`) | — |
| range | default **2%** (`DEFAULT_TEMPERATURE_RANGE_PERCENT`, `:54`), clamped to **10%** (`MAX_TEMPERATURE_RANGE_PERCENT`, `:57`; commit 8be3f4f1f after a lead set 60% and ran at 0.595) | — |
| draw | `u = rand×2−1`; `T = round3(max(0, base × (1 + u×range/100)))` | `:286-301,387-433` |
| applied | to both `config.temperature` and `providerConfig.sampling` (seed in the bag only) | `applySpawnSampling` `:476-500` |

Not found: automatic diversification without the lead asking, or any
temperature jitter on council or compaction reviewers.

### Ours

- Template per model (`claude_hooks/model_sampling.py:165-190`), merged under
  explicit options in `_to_ollama` (`chat_client.py:857-866`).
- The llamafile client forwards only `temperature` (`chat_client.py:1148-1149`).
- No per-lane seed or temperature variation (grep for `seed` under
  `consultants/engine` finds only unrelated "boot seed" text).
- Diversity today comes from `extra_models` (different models per lane).

---

## 8. Adjacent findings (outside the slice, noticed while mapping)

- **Compaction constants have drifted from the fork.**
  - Fork: `COMPACTION_TRIGGER_RATIO = 0.9` (`compaction-shared.ts:26`),
    `MIN_OUTPUT_ROOM_TOKENS = 8000` (`:328`, which is where the agents' retry
    ladder stops halving), and newest-3 turns at full size with older outliers
    discounted to the 2nd-largest (`OUTPUT_ROOM_RECENT_TURNS = 3`,
    `resolveObservedOutputTokens` `:377-412`). The fork also sizes output room
    from the **context footprint** of the turns, not the billed output (`:367-376`).
  - Ours: 0.8 (`budget.py:131`), `MIN_OUTPUT_ROOM_TOKENS = 2048` (`:104`), a
    plain max over 12 turns (`:159-183`), and the billed `_output_tokens` /
    `metrics.output_tokens` (`:175-176`).
- **Cold-start reserve.** The fork's declared room uses
  `resolveDefaultMaxOutputTokens(ctx)` when no cap is declared (`:449-451`).
  Ours uses 32,768.
- **Auxiliary slot discipline.** In the fork, auxiliary calls wait (up to
  120 s) for the conversation's local slot rather than racing it
  (`gateway.ts:270,745-797`). Our council has no such ordering; it overlaps with
  `ollama_slots`, which is FIFO per scope.
- **Reasoning history.** The fork decides per model whether prior thinking is
  replayed (`reasoningHistory` auto/all/last/none, with a measured re-injection
  probe), and the estimator counts only what is sent. Our `token_calib` counts
  every `thinking` key present in messages. Whether our council messages carry
  `thinking` back to Ollama was not traced in this slice.
- **Our `/get-advice` assumption** that cloud tags 500 on an explicit `num_ctx`
  (`get_advice/cli.py:243`) contradicts the fork, which sends `num_ctx` to cloud
  models on every request. Worth re-testing.
- **Timeout model.** The fork's health-probe watchdog (interval, not deadline)
  is a candidate for our stall monitor. That belongs to the runtime-control
  slice.

---

## 9. Gap table

Effort: S < ½ day, M 1-2 days, L > 2 days. Risk is to existing council
behaviour.

| # | Fork has (cite) | We have (cite) | Port how | Effort | Risk |
|---|---|---|---|---|---|
| 1 | Window = Modelfile `num_ctx` → (cloud) recommendation `context_length` → trained ctx → 32,768 (`ollama.ts:102-115,241-256`) | Trained `*.context_length` for all models (`chat_client.py:157-182`) | Parse `parameters.num_ctx` in `_probe_supports_think`; use the trained ctx only when `ollama_slots.is_cloud()`; else `parameters.num_ctx`, else `None` | S | Low (all roles are cloud today) |
| 2 | Same number sent as `options.num_ctx` and used for budget (`provider-option-rules.ts:529`) | Engine never sends `num_ctx`; `RoleConfig.ctx_max` unused (`config.py:185`) | Wire `ctx_max` (when explicit) to `options.num_ctx` and to `budget.plan`'s ctx; for local models send the resolved window so budget and load agree | S | Med: a changed `num_ctx` reloads the model; keep it stable per model |
| 3 | Auto cap `min(0.75×ctx, 96k, model max, user max)` (`output-budget.ts:36-102`) | Flat 32,768 (`budget.py:74`) | Add `resolve_output_budget(ctx, model_max, user_max)` in `budget.py`; replace `MAX_OUTPUT_TOKENS` as the default ask; keep 32,768 when ctx is unknown (fork: 32,000) | S | Med: longer worst-case turns (96k); the stall/deadline caps must cover them |
| 4 | Learned ceiling from refusal text, retry once, per (provider, model) (`gateway.ts:125-203,963-996`) | None | In `ChatClient.chat`/`chat_streamed`, regex the 4xx body on the three patterns; cache in a module dict; re-send with `num_predict = ceiling` only when the cap was ours | S | Low |
| 5 | Reserve `max(1024, ⌈0.12×est⌉)`; overflow keeps the request's own cap (`gateway.ts:448-464,582-633`) | Flat 2,048; overflow sends 4,096 (`budget.py:83,325-340`) | Swap `WINDOW_MARGIN_TOKENS` for the proportional reserve; on overflow keep `ask` (still flag `needs_compaction`) | S | Low-Med |
| 6 | `# Output Budget` system section with the thinking share (`output-budget.ts:137-205`) | None | Append it in `_plan_and_compact` once per call (idempotent on the heading); include the level's budget when known | S | Low; prompt change, so bench it |
| 7 | Calibration guards: `tokens>ctx` drop, ratio<1.2 drops the count too, residual<20% no ratio (`tokens.ts:399-456`) | Count kept unconditionally (`token_calib.py:299-315`) | Add a `context_window` param; reorder exactly as the fork does; keep per-model keys | S | Low (latent today) |
| 8 | Anchored estimate on the last counted request (`tokens.ts:279-346`) | Plain estimate every time (`token_calib.py:383-393`) | Store `request_chars` and `request_reasoning_chars` with the count; add `anchored_request_tokens(model, chars, reasoning)`; use it in `budget.plan`. Anchor per **(model, lane/session)**: the fork anchors per session, and our x-tier lanes share a model but not a transcript | M | Med: a wrong anchor key gives lane-crossed estimates |
| 9 | Tools counted in the estimate and in calibration chars (`tokens.ts:786-813`) | Messages only (`budget.py:145-156`; `council.py:1057`) | Pass `tools=payload["tools"]` into `estimate_request_tokens` from `_loop_payload_budgeter` and into `_observe_usage` | S | Low |
| 10 | Every conversation turn calibrates; 0.8 reasoning share (`gateway.ts:52,1054-1116`) | Single-shot only; 0.6 (`council.py:1066,1219`) | Call `_observe_usage` after each `chat_fn` in `agent_loop` (via a `LoopConfig.on_response` hook); move the threshold to 0.8 | S | Low |
| 11 | Unnamed level → bare `think:true` (keeps the Modelfile budget) (`provider-option-rules.ts:486-528`) | Always a level from `DEFAULT_THINK_BY_ROLE` (`config.py:230-245`) | Keep role levels (they are deliberate), but add `think = "auto"`/`True` as an allowed per-role value that means "model decides", documented with the table at `:497-501` | S | Low |
| 12 | Think-unsupported: never degrade to *absent* `think` for a reasoning model | 400 → strip `think` (`chat_client.py:505-529`) | On a 400 naming `think` for a level, retry once with `think:true` before stripping; strip only when the capability probe says no `thinking` | S | Low |
| 13 | Level fractions + `xhigh→max` + integer counts; `/api/show` read-back of `think_budget_tokens` (`llamacpp-sampling.ts:61-144`; `ollama.ts:381-450`) | Levels low/medium/high only; no read-back (`config.py:858-865`) | Accept `max`/`minimal`/`xhigh`/int in `_merge_role`; add `probe_think_budget(model, level, num_predict, num_ctx)` to `ChatClient` (POST `/api/show` with think + options) | S | Low; cloud may not answer, which yields `None` |
| 14 | Capped-thinking compared against the **thinking allowance**; message configured, then the Modelfile's (`session-budget.ts:116-167`) | Compared against the whole `num_predict`; council never passes `thinking_budget_message` (`runner.py:162-168`) | Pass `thinking_budget_tokens` (#13) and `chat_client.thinking_budget_message(model)` into `LoopConfig` at `council.py:1612,2410`, and into the `_single_shot` truncation path | S | Low |
| 15 | Recommendations: cloud `context_length`, `max_output_tokens`, `required_plan`, thinking values (`ollama-account.ts:174-219,313-336`) | None | Add `ollama_account.recommendations(base_url)` (GET, no auth, TTL like `_TAGS_TTL_S`). Use `context_length` in #1 and `max_output_tokens` as `model_max` in #3 (the fork leaves it display-only; we can go further). Validate the role level against `thinking_values` at config load (warn) | M | Low |
| 16 | `/api/me` via the local server, with `signin_url` | Direct signed `ollama.com/api/me` (`ollama_slots.py:129-149`) | Optional: try `POST <base>/api/me` first (no `cryptography` dependency, works on hosts without the key), fall back to direct | S | Low |
| 17 | Rate-limit middleware honouring `Retry-After` (≤30 s, 4 attempts, 2 s default) (`retry-rate-limit.ts:67-223`) | Own backoff, ignores `Retry-After` (`chat_client.py:540-552`) | In both 429 branches, read `e.headers.get("Retry-After")` (seconds or HTTP-date); sleep `min(asked, retry_max_delay_s)`; still release the slot | S | Low |
| 18 | Empty-response retry (3×, 250 ms) (`retry-empty-response.ts:98-102`) | None in `ChatClient`/council (caliber proxy has one) | Retry when `done_reason=="stop"` with no content and no tool calls, ≤ 2 times; keep the accepted attempt's `prompt_eval_count` separate for calibration (9d7cf0b6f) | S | Low |
| 19 | Per-spawn seed/temperature diversification: seed+i, random seed < 2^31, ±2% (max 10%) around the model's temperature (`spawn-sampling.ts:54-60,223-433`) | None | Add `[runtime] lane_sampling = {seed = "offset"\|"random"\|off, temperature_range_pct = 2}`; in the x-tier fan-out set `options.seed`/`temperature` per lane; base = explicit option, then model template (`model_sampling.sampling_for`), then `/api/show` `parameters.temperature`, else leave unset. Configurable in both the skill menu and the CLI | M | Med: changes answers; needs an M12-style parity cohort with it off |
| 20 | `parameters.temperature` read from `/api/show` (`ollama.ts:298-314`) | Not read | Harvest it in the same probe (#1); needed by #19 | S | Low |
| 21 | Sampler allow-list incl. `num_keep`, `num_gpu`, `stop`, `think_budget`, `think_budget_message` (`ollama.ts:917-934`) | Allow-list lacks them (`model_sampling.py:53-55`) | Extend `FIELDS`; `think_budget_message` then works per model template | S | Low |
| 22 | llama.cpp: `reasoning_budget_tokens`, full sampler, `reasoning_budget_message` (`llamacpp-sampling.ts:153-222`) | llamafile client: `max_tokens` + `temperature` only (`chat_client.py:1131-1158`) | In `_to_openai` map the sampler with the llama.cpp names, apply `sampling_for`, compute `reasoning_budget_tokens` from `think` via the fraction table | S | Low |
| 23 | opencoti provider: `/props` features, `session_id`, `num_ctx`/`num_ctx_min` booking, `X-Context-Window`, window-refusal negotiation, keepalive (`opencoti.ts:399-1052`; `polykv.ts:805-1112`) | None | New `OpencotiChatClient` (OpenAI-compatible) with a `/props` probe, a stable `session_id` per lane, `X-Context-Window` as the authoritative ctx for `budget.plan`, `Retry-After` handling; PolyKV pools later (another slice) | L | Med |
| 24 | xOllama as a plain provider: `/api/xollama` features, top-level `session_id`, `placement`, `X-Context-Window`, Bearer key (`xollama.ts:7-37,105-172,1130-1469`) | None | Small extension of `ChatClient`: if `/api/xollama` answers, add `session_id` per lane; if `context_window_v1`, read `X-Context-Window` into `context_length`; never move a window ask into `options.num_ctx` | M | Low-Med |
| 25 | Per-endpoint concurrency `parallelSessions` (1..64, default 1) (`agent-slots.ts:22-140`) | Account-wide cloud slots from the plan + `local_limit` (`ollama_slots.py:61-201`) | Nothing to port; **ours is stronger**. Optionally add a per-base-URL override for non-Ollama endpoints | — | — |
| 26 | Compaction: trigger 0.9, output-room floor 8,000, recent-3 rule, footprint-based (`compaction-shared.ts:26,297-412`) | 0.8, 2,048, plain max, billed output (`budget.py:104,131,159-183`) | Belongs to the compaction slice; listed so it is not lost | M | Med |

---

## 10. Suggested order

1. #7, #9, #10: calibration correctness, small and latent-bug-closing.
2. #1, #2, #20, #21: one `/api/show` probe that learns everything.
3. #3, #4, #5, #6: output budget, behind a config switch, benched before the
   default flips. The xhigh deadline (90 min) and stall thresholds need checking
   against 96k turns.
4. #11-#14: thinking, with the level read-back where the server answers.
5. #17, #18: transport hardening.
6. #15, #16: account metadata.
7. #19: lane diversification (opt-in, parity cohort first).
8. #22-#24: llama.cpp / xOllama / opencoti clients, when a local engine route is
   wanted.

Process note: per this task's read-only constraint, `.wolf/anatomy.md` and
`.wolf/memory.md` were **not** updated for this new file.
