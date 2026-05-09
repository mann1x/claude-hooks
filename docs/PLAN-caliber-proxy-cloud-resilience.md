# PLAN — port consultants-engine cloud resilience to the caliber grounding proxy

**Status:** scoping. Implementation deferred until the in-flight 2026-05-09
`gemma4:31b-cloud` bench finishes (modifying the proxy now would break the
running caliber init).

**Why:** `gemma4:31b-cloud`, `gemini-3-flash-preview:cloud`, and any other
`*:cloud`-tagged Ollama model occasionally returns a 5xx, an empty content
body, or hangs without the heartbeat. Today the caliber-grounding-proxy
forwards those failures straight back to caliber, which has a much shorter
and less complete retry policy than what the consultants engine evolved
through May 2026 (see commit `5626c4a`-era `chat_client.py`). For caliber
init runs against cloud models to be reliable, the proxy needs the same
treatment.

## Reference: what the consultants engine already does

`claude_hooks/get_advice/chat_client.py` (394 lines, used by `agent_loop.runner`
and the entire `consultants/engine/`) ships:

- **Retry budget**: `DEFAULT_MAX_RETRIES = 15` attempts, base delay 1.5 s,
  cap 90 s (~15 min total). Sized so a single chat call survives a multi-minute
  cloud incident without giving up.
- **Retryable status set**: `{408, 429, 500, 502, 503, 504}`.
- **Retryable 4xx body patterns**: `"Bad Gateway"` (Cloudflare wrapping a
  502), and the `reasoning_content` / `reasoning` rejection patterns where
  the model returns 400 because Ollama stripped the field.
- **`reasoning` strip-and-retry**: when the upstream returns a 400 saying
  "no field 'reasoning'", strip it from the payload, retry once outside
  the normal budget, and remember the model so future calls skip the
  field too.
- **Exponential backoff with jitter** between retries.
- **Success log on retry**: `"ollama chat: succeeded on retry %d/%d"` —
  visibility that the recovery happened.
- **Per-role retry counts** are recorded into the consultation's
  `metadata.json` (`storage.py:78`) so dashboards can see cloud flap rate.
- **Fallback model walk** (`graph.py:148`): when one model exhausts its
  retry budget, the orchestrator tries the next model in the role's
  `extra_models` list before failing the lane.
- **Synthesizer-only degraded answer** (`council.py:555`): when the
  synthesizer alone exhausts its budget, build a fallback from the
  researcher reports + critic verdict instead of crashing the whole
  consultation.

## Reference: what the caliber-grounding-proxy does today

`claude_hooks/caliber_proxy/`:

- `server.py` — HTTP listener, OpenAI-compat `/v1/chat/completions` handler.
  On upstream failure: `self._write_json(502, …)` — straight pass-through.
- `ollama.py:chat_completions()` (~50 lines, the upstream call) — `httpx.post()`,
  raises `UpstreamError(status, body)` on any 4xx/5xx; no retry, no jitter.
- `force_first_retry_*` config in `server.py:368-370` — only retries when
  the model returned **no tool_calls** on iter 0, not when it returned
  an HTTP 5xx. Different concern.
- No retry budget, no fallback model, no metadata visibility into upstream
  flap rate.

So the proxy passes upstream cloud weather straight through, and caliber's
node-side retry (10s inactivity timeout, ~3 retries) is too short to ride
out the 1–3 min cloud blips that consultants regularly survives.

## Implementation plan

### Step 1 — extract the retry pattern into a shared module

Create `claude_hooks/_chat_retry.py` (lightweight, depends only on
stdlib + `httpx`). Move the constants (`DEFAULT_MAX_RETRIES`, etc.),
`RETRYABLE_STATUS`, `RETRYABLE_BODY_PATTERNS`, and the
`with_chat_retries(callable, *, max_retries, base, cap, on_retry)`
helper from `get_advice/chat_client.py` into the new module. Keep
`chat_client.py` calling the shared helper — no behavior change.

Test: existing `tests/test_chat_client_retry.py` (or whatever file
exercises consultants retry) should still pass.

### Step 2 — wrap the proxy's upstream call

In `caliber_proxy/ollama.py:chat_completions()`, replace the bare
`httpx.post()` with `with_chat_retries(lambda: client.post(...), …)`.
Map `UpstreamError` raised inside the lambda to the retry path.

When all retries exhaust, propagate `UpstreamError(status, body)` as
today — caliber will see a 502, but only after we've tried for ~15 min.

### Step 3 — add empty-content retry

A specific cloud failure mode: Ollama returns `200 OK` with an
otherwise-valid response where `message.content == ""` and there are
no tool_calls. Today the proxy passes this through and caliber treats
it as a real (empty) reply. Add a check in `chat_completions()`:
if status 200 + empty content + empty tool_calls + finish_reason !=
`"length"`, treat as a soft failure and retry through the same budget.
Cap empty-content retries separately (5 attempts) so a model that
genuinely produces empty replies doesn't burn the whole budget.

### Step 4 — surface flap rate in the proxy log + a counter

Each `chat_completions()` call already logs the upstream status. Add:

- One INFO line per successful retry: `"upstream chat: succeeded on
  retry N/M after <reason>"` (status / body pattern / empty content).
- An in-process counter in `server.py` (`upstream_5xx_total`,
  `upstream_empty_total`, `upstream_retry_succeeded_total`) exposed
  on `/health` JSON. No persistence; resets on restart. Enough to
  build a per-bench flap-rate metric without adding sqlite.

### Step 5 — config knobs (env vars only, no settings.json)

```
CALIBER_GROUNDING_RETRY_MAX_ATTEMPTS=15      # default 15, set 0 to disable
CALIBER_GROUNDING_RETRY_BASE_DELAY_S=1.5
CALIBER_GROUNDING_RETRY_MAX_DELAY_S=90
CALIBER_GROUNDING_RETRY_ON_EMPTY=1           # 0 to disable empty-body retry
CALIBER_GROUNDING_RETRY_EMPTY_MAX=5
```

Document in `docs/caliber-proxy.md` § "Cloud-model resilience".

### Step 6 — tests

`tests/test_caliber_proxy_retry.py`:

- `test_5xx_retried`: stub `httpx.Client` returning 503 then 200; asserts
  the proxy returns 200 with body from the second attempt.
- `test_5xx_exhausts_to_502`: stub returning 503 N+1 times; asserts caller
  sees `UpstreamError(503, …)`.
- `test_empty_content_retried`: stub returning 200+empty then 200+content;
  asserts second response wins.
- `test_finish_length_not_retried`: empty content but `finish_reason="length"`
  — must NOT retry (legit truncation).
- `test_disable_via_env`: with `CALIBER_GROUNDING_RETRY_MAX_ATTEMPTS=0`,
  first failure passes through.

### Out of scope (separate ticket later)

- **Fallback model walk**. The proxy is single-model per call (caliber
  picks the model). Walking `CALIBER_FAST_MODEL` as a fallback would
  break caliber's own provider/model accounting. Defer until
  caliber-side support exists.
- **Per-role retry counts in `metadata.json`-equivalent**. The proxy
  doesn't write any persistent metadata today. Adding it would be a
  larger schema change. The /health counter is the proxy-side
  equivalent for now.
- **Synthesizer-style degraded answer**. The proxy is too low-level —
  it has no concept of "synthesizer" vs "researcher". This belongs
  in caliber if anywhere.

## Acceptance criteria

A `caliber init --auto-approve --verbose` run against
`gemma4:31b-cloud` survives a simulated 90-second `503` window from
the upstream Ollama (testable by pointing `CALIBER_GROUNDING_UPSTREAM`
at a local stub that 503s for 90s then proxies through). On the
proxy's `/health`, `upstream_5xx_total` and
`upstream_retry_succeeded_total` both increment; the visible caliber
init phase ("Generating skills…") continues without error.

## Out-of-band: bench protocol update

After Step 5 lands, add to `caliber-eval/PROTOCOL.md` § "Pitfalls":

> 6. **Cloud-tagged models flap. (resolved 2026-05-XX)** The proxy
>    now rides 5xx/empty-content blips internally with a 15-attempt /
>    ~15-min budget. Older runs that show "Model produced no output for
>    12m4s" failures are pre-resilience and not directly comparable to
>    post-resilience runs. Re-run pre-resilience labels with the new
>    proxy if you want apples-to-apples timings.
