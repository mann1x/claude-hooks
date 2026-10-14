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
compute a window; `session_ctx_max` stays the authority. bs2 runs with
DCA off, so a DCA-extended `session_ctx_max` has not been observed yet.

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
   - local Ollama: the Modelfile `num_ctx`, **never** the trained context, which is not what the model loads with;
4. 32,768.

Local Ollama is sent the resolved window as `options.num_ctx`, with the
same value on every call, so the model loads once. Nothing else is sent
one: cloud ignores it, xOllama reloads on it, and opencoti's window
modes are Milestone F's.

On bs2 (opencoti) the 2,097,152-cell KV pool is shared by 4 slots, and
one session can take at most 262,144 (`session_ctx_max` =
`largest_admissible`), which is what calls are budgeted against.

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

## Sampler fields

`config/model-sampling.json` templates may also set `num_keep`, `stop`,
`think_budget` and `think_budget_message` (see
[`model-sampling.md`](model-sampling.md)).
