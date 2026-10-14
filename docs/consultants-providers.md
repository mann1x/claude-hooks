# Consultants providers

How the council's chat clients learn what a backend can do, talk to it,
and record what each call cost. Plan: [`PLAN-council-cerebriline.md`](PLAN-council-cerebriline.md)
Milestone A. Status: the Ollama (local and cloud) half is built; xOllama
and opencoti probes are implemented against fixtures and wait on live
endpoints.

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

## Sampler fields

`config/model-sampling.json` templates may also set `num_keep`, `stop`,
`think_budget` and `think_budget_message` (see
[`model-sampling.md`](model-sampling.md)).
