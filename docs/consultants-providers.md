# Consultants providers

How the council's chat clients learn what a backend can do, talk to it,
and record what each call cost. Plan: [`PLAN-council-cerebriline.md`](PLAN-council-cerebriline.md)
Milestone A. Status: built for Ollama (local and cloud) and opencoti
(verified against a live opencoti with PolyKV, 2026-10-14). The xOllama
probe and client path are built and tested against fixtures; they wait
on a live endpoint.

## Endpoints: using opencoti, llama.cpp or xOllama for a role

Named servers live in the consultants config. There are no host
defaults: an endpoint exists only where a user configured it.

```toml
[endpoints.bs2]
url = "http://192.168.178.2:8240"
backend = "opencoti"        # opencoti | llamacpp | xollama | ollama
# model = "..."             # served model when a reference names none
# api_key = "..."           # bearer key, if the server wants one
# window = "dynamic"        # PolyKV window mode, see below
```

```bash
claude-consultants config set-endpoint bs2 --url http://192.168.178.2:8240 --backend opencoti
claude-consultants config set-role researcher --model @bs2
claude-consultants config remove-endpoint bs2
```

- A role's model is `@name` (the endpoint's served model, or the first
  id its `/v1/models` lists) or `@name/<model>`. `@` never occurs in an
  Ollama tag, so references can't collide with tags.
- `opencoti` / `llamacpp` go through `OpenAICompatChatClient`
  (`/v1/chat/completions`, SSE streaming). `xollama` / `ollama` go
  through the native `ChatClient` at that URL.
- A role whose primary and extra models span several backends gets a
  `ModelRoutingChatClient`: fan-out lanes and fallbacks reuse the role's
  client and only change `model`, so each call is routed by its model.
  A role whose models share one backend keeps the plain client.
- `config show` lists endpoints with `api_key_set`, never the key. The
  `/consultants config` menu has an **Endpoints** entry (Subflow J).

### opencoti translation

| ours | opencoti wire |
|---|---|
| `options.num_predict` | `max_tokens` (clamped to a learned ceiling) |
| `options.num_keep` | `n_keep` |
| `options.think_budget_message` | `reasoning_budget_message` |
| sampler fields | same names (`temperature`, `top_k`, `min_p`, `seed`, `stop`, …) |
| `think: false` | `reasoning_budget_tokens: 0`; levels wait on Milestone B3 |
| reply `reasoning_content` | kept on the message (the council reads it) |
| `timings.cache_n` | `timings.cached_prompt_tokens` |
| `X-Context-Window` | `timings.granted_window` |

### One system turn on llama.cpp wires

llama.cpp chat templates (opencoti, llamafile) accept one `system` turn,
first. The council sends several: grounding, structure map, anchors and
the role prompt lead every grounded role, and compaction markers come
later. `single_system_messages()` joins the leading run into one system
turn. A later system turn is sent as a user note (`[system note]`),
merged into an adjacent user turn so roles still alternate. Nothing is
dropped. Ollama-native requests are unchanged. The first full council on
opencoti failed on this ("System message must be at the beginning").

### PolyKV window mode (`window`)

One PolyKV connection carries many sessions, one per agent (council
lane). Each session is bounded by the engine's `session_ctx_max`: the
model's window (262,144 on bs2), or more with DCA. The KV pool
(`kv_pool_cells`, 2,097,152 on bs2) is shared across sessions and is not
any one session's window.

- **`dynamic`** (default, and the only mode today): no fixed context.
  The council sends no `num_ctx`, so each agent request is its own
  allocation. The engine asks for `session_ctx_max`, settles no lower
  than the request's peak, and releases it at completion. Every lane is
  budgeted against `session_ctx_max`. `X-Context-Window` is that one
  request's settlement (83 on a 21-token call), so it is never read as a
  window (`ChatClient.granted_window` keeps a grant only from a request
  that stated `num_ctx`). A full pool answers 429 with `Retry-After`,
  which the transport waits out.
- **`assured`**: a window booked per agent session (`num_ctx` /
  `num_ctx_min`, gated on `elastic_guaranteed_alloc_v1` /
  `ctx_min_negotiation_v1`, the grant re-sent on every later request, the
  session closed when its work ends). It needs Milestone F's session
  manager, so `set-endpoint --window assured` is refused until then.

`/props` `opencoti.dca` (`enabled`, `yarn_factor`) is recorded on the
capability record (`dca_enabled`, `dca_yarn_factor`). It is never used to
compute a window; `session_ctx_max` stays the authority. Observed on bs2:
with DCA off it reported 262,144; after the 2026-10-14 restart with DCA
on it reports 2,097,152, the whole pool, so the budget follows without a
code change. The output cap is then the 96k ceiling. One PolyKV session
can carry up to 32 workers; using that is Milestone F (the plan's F3).

### PolyKV client and placement (plan F1)

`claude_hooks/polykv/client.py` is the engine's control plane, stdlib
only. It is ported from opencoti's `opencoti_langgraph/client.py` (payload
builders, error mapping, `Capacity`) and covers the rest of the surface:
- owner / `shared` / `unowned` pools, copy-on-write `branch_pos`;
- `/kv`, capacity with the 0338 window fields, admission policy;
- session close (body form for ids the path cannot carry, `found` read
  back), resize, slot erase;
- `/apply-template` (`continue_pool` too), `/tokenize`, `boot_id`,
  `/metrics`, `/polykv/sampling`.

It speaks to the engine directly (`PolykvClient.direct(root)`) or through
xOllama's `/api/engine` envelope (`PolykvClient.via_xollama(base, model)`),
where the engine's own status inside the envelope is the one raised. Pool
ids are strings, because pool 0 is real.

A chat request asks for a placement with one internal payload key,
`placement` (`claude_hooks/polykv/wire.py`):
- **opencoti `/v1/chat/completions`:** top-level `session_id`,
  `pool_id`, `continue_pool`, `num_ctx` / `num_ctx_min`, `cache_prompt`
  and `chat_template_kwargs`; on a stream also `stream_options.keepalive`
  and `return_progress`.
- **xOllama native `/api/chat`:** `session_id` and
  `placement{pool_id, num_ctx, num_ctx_min}`, never `options.num_ctx`.

Without the key, both bodies are byte-identical to before. The reply's
placement is recorded under `timings["polykv"]`:
- `X-Context-Window`, `X-OpenCoti-Boot-Id`, and the `opencoti` block
  (`n_pool_shared`, `pool_match`, `pool_len`, `pool_unknown`);
- on xOllama, which sends no block, the cached prompt count stands in
  for the share.

A pool named with nothing shared is recorded as a warning rather than
passing as a success. Prefill `prompt_progress` frames and `: keepalive`
comments count as stream liveness.

Live gate: `scripts/polykv_probe.py --xollama URL --model M` (or
`--direct URL`). It opens one owner, builds a two-layer tree cut from
`/apply-template` and checks it is a byte prefix of the full render, runs
two workers, then releases everything and audits. On 2026-10-24, on
eleven2go with `mannix/omnimerge-v4-mtp:IQ2_M` (the only plain model
there with `client_pools`), both workers got 2840 cached tokens of a
2840-token tree, the owner used 2865 of 32768 cells, and nothing leaked.

## Capabilities: `claude_hooks/provider_caps.py`

One `ProviderCaps` record per `(base_url, model)`, cached for 10 minutes.
`ChatClient.caps(model)` builds it from the `/api/show` answer the client
already fetched, so it costs no extra round trip for that.

| field | source | meaning |
|---|---|---|
| `backend` | `GET /api/xollama` (xOllama answers, stock Ollama 404s); opencoti `/props` features | `ollama`, `xollama`, `opencoti`, `llamacpp` |
| `cloud`, `remote_host`, `remote_model` | `/api/tags` `remote_host` | cloud model; the `:cloud` name rule only when the catalog can't be read |
| `declared_num_ctx` | Modelfile `parameters.num_ctx` (opencoti: `n_ctx`) | the window a local model loads with |
| `trained_ctx` | `model_info["<arch>.context_length"]` | what the weights were trained for: a ceiling, not the running window |
| `context_length`, `max_output_tokens` | model recommendations | the window and output maximum the cloud service grants |
| `thinking_values`, `thinking_default` | model recommendations | accepted thinking levels (`false`/`true` → `off`/`on`) |
| `required_plan` | model recommendations | plan the model needs |
| `temperature` | Modelfile / opencoti default sampler | base for sampling diversity (Milestone B) |
| `capabilities` (`thinking`, `tools`) | `/api/show` | `None` when the server lists none |
| `features`, `boot_id` | `/api/xollama`, `/props`, `/health` | feature flags; a new `boot_id` drops the record |

Every probe is soft. A field nobody answered stays `None`, which callers
must treat as unknown, never as a small default. A server that could not
be reached is not cached; a definite answer (a 404 included) is.
`build_info` / versions are never branched on.

## Account: `claude_hooks/ollama_account.py`

- `POST <server>/api/me`: the local server signs it, so no key is needed.
  Only `plan` and `signed_in` are kept. `ollama_slots` now asks the
  server a cloud call went through first and falls back to the signed
  direct call.
- `GET <server>/api/experimental/model-recommendations`: cached an hour.
  Matched by `remote_model` before the tag's own name, then by stem, so
  a re-templated cloud tag (`glm-5.3-flash-tpl2:latest`) finds its entry.
- `claude-consultants account [--model M ...] [--ollama-url U]` prints
  the plan and each cloud model's window, max output, thinking values and
  `plan_ok`. `/consultants account` runs it, and the role-model picker
  checks a cloud pick against it.

## Transport: `ChatClient` (`claude_hooks/get_advice/chat_client.py`)

`chat` and `chat_streamed` share one retry loop (`_call`). On top of the
15-attempt budget, the outage wait and the `think` strip:

- **`Retry-After`** on 429/503 (seconds or HTTP date) sets the wait, up
  to 30 s. Without it, the exponential backoff applies as before.
- **Empty reply** (no content, no tool call, no reasoning, `done_reason`
  `stop`): asked again up to 2 times, 0.25 s apart, without spending a
  retry. A reasoning-only reply or a cut one is not empty.
- **Learned output ceiling**: a refusal naming the provider's maximum
  (Ollama Cloud / Z.ai, Anthropic, OpenAI wording) is retried once at
  that value. The value is remembered per server and model and applied
  before later calls (`learned_output_ceiling()`).
- **Fault classes**: a call that fails for good raises `ChatFault`, a
  `RuntimeError` with an unchanged message, carrying `fault_class`:
  `transport`, `refusal`, `model_missing` (never retried; the role's
  fallback chain moves on), `context_overflow`, `bad_request`.
- `api_key=` sends `Authorization: Bearer` (ollama.com direct, a keyed
  xOllama). A base URL ending in `/v1` or `/api` is reduced to the root.

## Timings (plan A5)

Every successful call returns `timings` beside `usage`, also on
`client.last_timings`. The recorder stores it in `response_json` and
mirrors it on the `llm_call` runtime event.

| key | meaning |
|---|---|
| `queue_ms` | waiting for a connection slot (`ollama_slots`) |
| `ttft_ms` | streamed calls: send to first content, reasoning or tool-call chunk |
| `wall_ms` | the accepted attempt, send to last byte |
| `load_ms`, `prompt_eval_ms`, `eval_ms`, `total_ms` | Ollama's own durations |
| `prefill_tok_s`, `decode_tok_s` | from evaluated tokens only |
| `granted_window` | `X-Context-Window`, when an opencoti or xOllama engine sends it |
| `attempts`, `empty_retries` | requests sent for this call |

## Budgets: output cap and window (plan B1/B2)

`consultants/engine/budget.py`. Every role call is planned by `plan()`.

**Window** (`resolve_window`), first that answers:

1. a window the engine granted to a request that stated one;
2. a pinned `num_ctx`;
3. per backend:
   - opencoti / llama.cpp: `/props` `opencoti.kv.session_ctx_max`, then `n_ctx`;
   - cloud Ollama: the recommendations window, then the trained context;
   - local Ollama: the Modelfile `num_ctx`, then the trained context — sent as `num_ctx`, so the model loads with it (pin `num_ctx` for less; a 256k KV cache may not fit a small GPU);
   - local xOllama: the Modelfile `num_ctx`, then the window the model is loaded with (`GET /api/ps` `context_length`, cached 60 s), then the trained context, then the default; never sent;
   - ollama.com used directly (`[endpoints.X] url = "https://ollama.com"`, `/api` or `/v1`) is cloud by its host: the recommendations window, then the trained context, nothing sent; there the bare name (`gemma4:31b`) is the cloud model — the record ollama.com returns for it is the one a local server returns for `gemma4:31b-cloud` (BF16, 262,144). A `/v1` endpoint without `/props` reads `/api/show` at the same root;
   - the trained context is `/api/show` `model_info["<arch>.context_length"]`, which Ollama answers for a cloud model that is not pulled (no pull happens; checked 2026-10-19 on xOllama 0.40.3 and eleven2go 0.40.0 for gemma4:31b, minimax-m3, gpt-oss:120b); a retired model is a 410, an unknown one a 404;
4. 32,768.

Local Ollama is sent the resolved window as `options.num_ctx`, with the
same value on every call, so the model loads once. Nothing else is sent
one: cloud ignores it, xOllama reloads on it, and opencoti's window
modes are Milestone F's.

On bs2 (opencoti) the KV pool is 2,097,152 cells. Without DCA one
session could take at most 262,144 (`session_ctx_max` =
`largest_admissible`); with DCA on (2026-10-14 restart) it reports the
whole pool. Calls are budgeted against whatever it reports.

**Output cap**: `min(0.75 × window, 96,000, the model's max)`. With no
window it is `min(32,000, …)`. The model's max comes from the cloud
recommendations, then a ceiling a refusal named.

- Beside the prompt the planner keeps `max(1,024, ⌈0.12 × estimate⌉)`
  free for estimate error.
- A prompt that leaves less than 4,096 tokens **keeps its cap** and is
  flagged for compaction, instead of dropping to 4,096.
- The role's cap is stated once in its system prompt (`# Output Budget`,
  the fork's wording). The stated number is the role's resolved cap, not
  the room one call has left, so the prompt and its prefix cache stay
  the same from call to call.

**Hard cap follows the cap.** The per-call hard caps
(`PRODUCTION_HARD_CAPS_BY_MODEL`, 300–900 s) were measured on calls
capped at 32,768 tokens. Measured decode across 140 `transcript.db`
files (2026-10-14) was 5–30 tok/s at p10 and 15–110 tok/s at p50, so a
96,000-token turn needs 15–100 min. A hard cap is never retried, so
without a change a long turn that used to be cut and continued would be
killed instead. The researcher's tuned cap is therefore scaled by
`num_predict / 32,768` (`stall_chat.scaled_hard_cap`). An operator's
`per_lane_hard_s` is used as set. The run deadlines (20 / 60 / 90 min)
are unchanged: a long single turn replaces the continuations that used
to carry the same output.

**Knobs**: shipped defaults, then `[budget]`, then `[role.X.budget]`,
then `[role.X.models."<model>"]`, most specific last. Keys:

- `output_ceiling`
- `output_window_share`
- `reserve_min`
- `reserve_share`
- `num_ctx` (`"model"` or tokens)
- `announce`

```bash
claude-consultants config set-budget --output-ceiling 64000
claude-consultants config set-budget --role researcher --model @bs2 --num-ctx 131072
claude-consultants config set-budget --role researcher --clear num_ctx
```

A legacy explicit `ctx_max` pins that role's window. The run's record
carries only the keys that were set (`runtime_control.budget`), so a
default run seeds nothing. The menu entry is **Output cap & window**
(Subflow K).

## Thinking per backend (plan B3)

A role's `think` is fixed per role and per (role, model), and never
derived from effort. Precedence: `[role.X.models."<m>"] think` >
`[role.X] think` > the model template's `think`
(`config/model-sampling.json`, a non-option field that is never sent) >
`DEFAULT_THINK_BY_ROLE`. No template ships a `think` yet; one is added
when a per-model value has been measured.

Values:

- `false` / `off`;
- `true` / `auto` / `on`: bare `think: true`, so the model's own budget
  applies;
- a level: `minimal` / `low` / `medium` / `high` / `max`, with `xhigh`
  meaning `max`;
- a token count.

```bash
claude-consultants config set-think critic high
claude-consultants config set-think researcher 8192 --model @bs2
claude-consultants config set-think researcher --model @bs2 --clear
```

| backend | level | token count |
|---|---|---|
| Ollama (cloud / local) | sent only if the model's `thinking_values` (recommendations) accept it, else `think: true`; with no list the level is sent as is | `think: true` + `options.think_budget` |
| opencoti / llama.cpp | `reasoning_budget_tokens` = share × `min(num_predict, window)`: max 4/5, high 1/2, medium 1/4, low 1/8, minimal 1/16 | `reasoning_budget_tokens` |
| xOllama / think-budget builds | as Ollama; the enforced budget is read back from `think_budget_tokens` on the reply (plain and streamed) and, before the call, from `POST /api/show` with the level and limits (`ChatClient.think_budget`; never asked of a cloud model) | as Ollama |

- A level the model refuses (a 4xx naming `think`) is retried once as
  `think: true`. Only a refused `think: true` strips the field.
- `timings.think_budget_tokens` records the enforced budget, with
  `think_budget_source` `read_back` or `sent`.
- Capped-thinking detection compares the think against that allowance,
  and against `num_predict` only when none is known.
- The budget message the detector looks for is the template's
  `think_budget_message`, else the model's own. No default text is
  invented.
- When the allowance is known before the call (opencoti, or a token
  count), the `# Output Budget` section states it.

Measured on 2026-10-14:

- xOllama (solidpc `:11433`, eleven2go `:22434`) reads back
  `minimal` → 500, `high` → 4,000 and `3000` → 3,000 at
  `num_predict` 8,000, plain and streamed;

- gemma4:31b-cloud accepts only `off` / `on`, so its roles' levels are
  sent as `true`;
- deepseek-v4.1-flash accepts `low` / `high` / `max`;
- glm-5.3:cloud publishes no list;
- opencoti `minimal` at `num_predict` 4,000 was sent 250 tokens and
  reasoned about 843 characters.

## Sampling diversity (plan B4)

`consultants/engine/sampling_diversity.py`. The same model often runs as
several instances in one run: fan-out lanes, critic and meta-critic, a
synthesizer on the researcher's model. Each instance gets its own
sampler, as in the fork's `spawn-sampling.ts`. An instance is one role
turn or one lane; its tool-loop iterations and its summary call reuse
the instance's values.

- **`seed`**: `"random"` (default) draws from `[0, 2³¹)`. Same-model
  instances in one run never share one: a per-run allocator, keyed by
  `runtime_control.sampling_run`, redraws on a collision. An integer `s`
  gives the i-th same-model instance `s + i`. `"off"` sends none.
- **`temperature`**: the base is an explicit setting, else the model
  template, else the Modelfile / `/props` temperature. With none known it
  is not sent.
- **`temperature_range_pct`** (default 2, clamped to 10):
  `T = base × (1 + u × pct/100)`, `u ∈ [-1, 1]`, 3 decimals, `≥ 0`.
- With `seed = "off"` and `temperature_range_pct = 0` nothing is added
  to a request (the parity cohort).
- An option a caller set explicitly wins.
- The realized values are in `timings.sampler` (mirrored on the
  `llm_call` event) and in `claude-consultants show <sid>` as `samplers`.

Layers: `[sampling]` → `[role.X.sampling]` → `[role.X.models."<m>"]`.

```bash
claude-consultants config set-sampling --temperature-range-pct 5
claude-consultants config set-sampling --role critic --seed off
```

Live 2026-10-14: three researcher instances on opencoti (`/props`
temperature 1.0) got seeds 1761893359 / 1629654338 / 2145521065 and
temperatures 0.985 / 1.001 / 0.983, and gave three different answers to
one prompt.

## Sampler fields

`config/model-sampling.json` templates may also set `num_keep`, `stop`,
`think_budget` and `think_budget_message` (see
[`model-sampling.md`](model-sampling.md)).

## Ollama Cloud direct (no local Ollama)

Ollama serves its cloud models directly
([docs.ollama.com/cloud](https://docs.ollama.com/cloud)): `https://ollama.com/api/chat`
(or the OpenAI-compatible `/v1`), `Authorization: Bearer <key>`, models
named as `https://ollama.com/api/tags` lists them — `gemma4:31b`, not
`gemma4:31b-cloud` (there the bare name is the cloud model; locally that
name is the downloadable quant).

**The key.** Put it in a file (first line; `chmod 600`, or a user-only ACL
on Windows) and name it in `config/claude-hooks.json`:

```json
"ollama": { "url": "...", "api_key_file": "~/.claude/ollama-api-key" }
```

With no file set, `OLLAMA_API_KEY` is used. A systemd unit or a scheduled
task never reads a shell profile, so the file is what reaches the engine.
The key is sent only to ollama.com. Check it without printing it:

```bash
python -m claude_hooks.ollama_upstream --explain   # URL + where the key comes from
python scripts/verify_deploy.py                     # "ollama cloud key — …, accepted (plan pro)"
```

**The council's default (`[cloud] route = "direct"`).** Every Ollama Cloud
model a role, lane, fallback, coder route or the distiller names
(`kimi-k2.6:cloud`, `gemma4:31b-cloud`) goes to ollama.com itself, sent
under its bare name (`kimi-k2.6`, `gemma4:31b`; `bare_cloud_name()`), with
the host's key — so a cloud council holds no host's Ollama. The config
keeps the cloud names: sampling templates (`config/model-sampling.json`),
stall thresholds and coder routes go on matching them; only the wire
carries the bare name (`ChatClient._real()` with `cloud_direct`). Neither
Ollama nor ollama.com declares sampling parameters for these models, so
the templates are the only source either way. `cc.client_endpoints(cfg)`
adds the route as a `cloud_models` endpoint (`ollama-cloud`) to what the
role clients route by; a local model in the same role stays on
`ollama.url` (`ModelRoutingChatClient`).

```bash
claude-consultants config set-cloud local              # the host's Ollama relays them
claude-consultants config set-cloud direct --key-file ~/.claude/other-key
```

**Other ways.** The whole engine (`ollama.url = "https://ollama.com"`)
or per role through an endpoint:

```bash
claude-consultants config set-endpoint cloud --url https://ollama.com --backend ollama
claude-consultants config set-role researcher --model @cloud/gemma4:31b
```

An endpoint may name its own `--key-file`; without one it uses the host's.
Windows come from ollama.com's public `/api/show` (trained context) and its
model recommendations; every call takes an account-wide cloud connection
slot, and the plan is read from `ollama.com/api/me` with the key.
