# 03 — PolyKV: full API map, Cerebriline coverage, and a council design

Research date 2026-10-10. Read-only survey; nothing in any repo was changed
except this file.

Sources read (and cited as `file:line` below):

- **opencoti** `/shared/dev/opencoti` (branch `dev`): `docs/features/polykv_api.md`,
  `polykv_integration_guide.md`, `polykv_fanout.md`, `post_c7_polykv_agent_program.md`,
  `polykv_api_research.md` (skimmed, design-space survey only), `elastic_parallel.md` §A4,
  `server_liveness.md`, `read_routes_audit.md`, `rolling_kv*.md`, `advanced_kv.md`,
  `mem_ledger.md`, `introspection.md`, `model_catalog.md`, `llamafile_supervisor.md`,
  `docs/decisions/0001-lazy-slot-context.md`, `docs/llamafile-flags.md`,
  `docs/llamafile-usage.md`, `docs/releases/c12-release-plan.md`;
  `packages/opencoti-llamafile/src/polykv-client.ts`, `packages/opencoti-tiers/src/compaction-polykv.ts`;
  `vendors/patches/llamafile/*polykv*` + the session/window patches (0337, 0338, 0345, 0362, 0394,
  0396, 0406, 0412, 0422, 0453, 0533, 0546) and `vendors/patches/llamafile/README.md`;
  **`opencoti/langgraph/`** (the `opencoti_langgraph` Python package, tests, benches).
- **xOllama** `/shared/dev/xollama` @ `8a6a71c7e`: `server/routes_engine.go`, `llm/engine_introspect.go`,
  `llm/engine_session.go`, `api/xollama_placement.go`, `api/xollama_identity.go`, `api/types.go`,
  `types/xollama/config.go`, `docs/xollama/sessions.mdx`, `council-integration.mdx`, `model-settings.mdx`.
- **Cerebriline** `/shared/dev/cline` @ `400b75535`: `sdk/packages/llms/src/providers/vendors/{polykv,
  polykv-lead,polykv-swarm,opencoti,xollama,xollama-engine,llamacpp-sampling}.ts`,
  `sdk/packages/core/src/extensions/context/{polykv-session,kv-pressure,continuation-compaction}.ts`,
  `sdk/packages/core/src/extensions/tools/team/spawn-sampling.ts`, and the living integration record
  `/shared/dev/docs/cerebriline-polykv-integration.md` (2662 lines, updated 2026-10-10).
- **Ours** `/srv/dev-disk-by-label-opt/dev/claude-hooks`: `consultants/engine/council.py`,
  `consultants/engine/graph.py`, `consultants/server/runner.py`, `consultants/server/app.py`,
  `claude_hooks/get_advice/chat_client.py`, `claude_hooks/caliber_proxy/prompt.py`.

The vendored engine C++ source (`server-context.cpp`) was **not** read directly. The tree that the
docs cite (`/shared/dev/opencoti/vendors/sources/...`) was not located within the search budget, so
every engine claim below comes from the engine's own design docs, patch headers and patch READMEs,
which quote the source. Where those disagree, the newer patch wins and the disagreement is noted.

---

## 1. The complete PolyKV API surface

"PolyKV" names three layers that a client sees together. All three are opt-in, and the engine
advertises each behaviour a client must branch on as a `/props.features` flag
(`elastic_parallel.md:956-962`, rule adopted 2026-09-22):

1. **Guaranteed windows (A4 "elastic slots")**: every completion is admitted against a unified cell
   pool. A `session_id` + `num_ctx` books a window for the session.
2. **Pools**: immutable, first-class shared-prefix KV objects arranged in a tree (`/polykv/*`).
3. **Admission/telemetry**: per-pool tps floor, settle windows, `/capacity`, `/kv`, `/elastic`, `/polykv/tps`.

### 1.1 Discovery

| what | how | notes |
|---|---|---|
| capability flags | `GET /props` → `features[]` | Branch on the flag, never on the version (`polykv_integration_guide.md:10-12`). Also `release`, `artifact` (build id), `opencoti.kv` headline (`elastic_parallel.md:1040-1042`). |
| pools enabled | `GET /props` → `opencoti.polykv.pools_enabled`, plus static `max_pools`, `tps_ewma_tau_s`, `tps_gap_reset_s`, `tps_stream_default_interval_ms` | `/props` carries static config only, never live state (`polykv_api.md:284-289`). With `--polykv-max-pools 0` (the default) every `/polykv/pools*` route errors/404s (`polykv_api.md:440-442`, `llamafile-flags.md:124`). |
| boot identity | `GET /health` → `{status, boot_id, started_at}`; same fields on `/props`; header `X-OpenCoti-Boot-Id` on every completion | `boot_id_v1`. Pool ids restart from 0 after a restart, so a cached `pool_id` must be paired with the `boot_id` it was created under (`server_liveness.md:60-72`, client checklist `:108-113`). |

**Feature flags seen in the sources** (union of `polykv_integration_guide.md:172-187`,
`elastic_parallel.md:1049-1052`, `server_liveness.md:86`, the 0394/0396/0399/0403/0406/0422/0453
README sections, and Cerebriline's table `polykv.ts:872-970`):

`attach_in_response_v1`, `capacity_readonly_v1`, `kv_status_v1`, `elastic_guaranteed_alloc_v1`,
`ctx_min_negotiation_v1`, `session_close_v1`, `session_close_body_v1`, `session_close_keep_running_v1`,
`polykv_subpools_v1`, `elastic_rw_v1`, `elastic_hold_rw_v1`, `polykv_rebase_v1`,
`polykv_private_window_v1`, `polykv_shared_root_v1`, `elastic_drain_v1`, `elastic_drain_k_v1`,
`pool_match_in_response_v1`, `pool_unknown_in_response_v1`, `tps_real_pool_id_v1`,
`settle_waived_header_v1`, `polykv_sampling_route_v1`, `pool_delete_alias_v1`, `tps_snapshot_v1`,
`session_pressure_v1`, `stream_keepalive_v1`, `tcp_keepalive_v1`, `boot_id_v1`, `kv_pressure_v1`,
`kv_resize_v1`, `kv_resize_deferred_v1`, `kv_resize_live_v1`, `kv_observable_v1`, `kv_recyclable_v1`,
`read_routes_snapshot_v1`, `pool_unowned_v1`, `pool_continue_v1`, `slots_nonblocking_v1`, `lock_v1`.

### 1.2 Boot flags a client has to know about

| flag | default | effect for a client |
|---|---|---|
| `--polykv-max-pools N` | 0 = off | Enables `/polykv/*`. Since 0337 it is a **per-owner-session** pool limit (`polykv_integration_guide.md:125`, `elastic_parallel.md:1028-1031`). Cerebriline hard-codes 8 (`polykv-swarm.ts:128`). |
| `--polykv-pool-seqs` | `max(--polykv-max-pools, --max-parallel)`, cap 256 | Shared seq-id reservoir for all pools. On iSWA each seq costs one SWA window of VRAM at boot (`elastic_parallel.md:1029-1031`). Exhaustion → "pool seq-id reservoir exhausted". |
| `--kv-unified` | off | Zero-copy prefix sharing. Without it shares are physical copies (patch 0145, `polykv_api.md:171-189`). Required by `--max-parallel`. Rebase needs unified KV. |
| `--parallel N` / `--max-parallel M` | | Elastic slots: N live at boot, grows to M (`polykv_integration_guide.md:150-170`). |
| `--max-parallel-tps-floor F` | 0 | Elastic growth/drain floor, runtime-settable via `POST /elastic`. |
| `--elastic-alloc-ttl S` | 300 | Idle TTL of a session window. `-1` = only on close, `0` = per request (`elastic_parallel.md:1023-1026`). |
| `--admission-poolless off\|warn\|enforced` | enforced | KV-cell admission for every request, pooled or not (`llamafile-flags.md:123`). |
| `--polykv-adm-on-error admit\|deny` | admit | What the enforced pool gate does when its own capacity check fails (`llamafile-flags.md:125`). |
| `--sse-ping-interval` | 10 s (upstream 30) | Keepalive period, not advertised on `/props` (`server_liveness.md:31-33`, Cerebriline `opencoti-liveness.ts:35-44` per doc `:1300-1306`). |
| `--tcp-keepalive-idle N` | 60 | Server-side only (`server_liveness.md:46-53`). |
| `--context-shift` | | Refused for pool-attached slots (P6a guard). Pooled sessions never shift (`polykv_api.md:635-654`). |

Per-slot context is `-c / (--parallel + --polykv-max-pools)` on a split cache. Under `--kv-unified`
the slot n_ctx is the full `-c` and admission divides it (`polykv_api.md:927-934`, `:650-654`).

### 1.3 Completion request fields (`/completion`, `/v1/chat/completions`)

| field | meaning | source |
|---|---|---|
| `session_id` | Slot affinity, plus the identity a window is booked under. Empty is never inserted into the affinity map. A **live** session's next request is a *continuation*: it books nothing, and a different `num_ctx` is ignored. | `polykv_integration_guide.md:67-73`, `elastic_parallel.md:1017-1021`, 0340 header |
| `num_ctx` (top level or `options.num_ctx`) | The window to book. Unstated means a per-request window. Above the model max it is **clamped and reported**, never refused. | `elastic_parallel.md:1002`, `:994` |
| `num_ctx_min` | Atomic negotiation floor. A new session that doesn't fit whole gets the largest 256-aligned window in `[num_ctx_min, num_ctx]`. A continuation ignores it. | 0338, `elastic_parallel.md:1067-1072` |
| `pool_id` | Attach to a pool. **0 is a valid id**; "no pool" is `-1` or absent. | `polykv_api.md:248-251`, `polykv_integration_guide.md:46-47` |
| `continue_pool: true` (+ `pool_id`) | The prompt is the pool's stored token ids ⊕ the *new* messages rendered as a continuation. The server prepends `[user ".", assistant SENTINEL]`, renders, and keeps what follows the last sentinel. `cache_n == len(pool)` by construction. The pool must end where an assistant reply ends (a `from_session` pool of a finished turn does). mtmd refused. Also accepted on `/apply-template`. | 0406, `patches/README.md:4166-4185`; `pool_continue_v1` |
| `n_predict` / `max_tokens` | The generation bound. It sizes the per-request window: bounded → `peak + 1`. Unbounded → slot maximum, negotiated down, floor `prompt + 1 + 256` (0533). | `patches/README.md:6020-6040` |
| `overcommit: true` | Skips the **enforced** pool gate for this request (logged). | `polykv_api.md:846-848`, `llamafile-flags.md:237` |
| `stream_options.keepalive: true` | Opens the SSE stream at once. Every silent period gets `: keepalive queued` / `: keepalive prefill n/N` / `: keepalive generating k`. First-result errors arrive in-stream under HTTP 200. | `server_liveness.md:12-44`; `stream_keepalive_v1` |
| `return_progress: true` (streaming) | `prompt_progress` frames during prefill, which is liveness before the first token (bug-2280). | `opencoti_langgraph/chat_model.py:78-87,232-236` |
| `cache_prompt` | Pair with `session_id` (stock llama.cpp field). | `llamafile-flags.md:233` |
| `seed`, `temperature`, … | Stock llama.cpp samplers, sent top level (Cerebriline `llamacpp-sampling.ts:30-44`). | |
| `shared_pool_slot`, `shared_prefix_n_tokens` | Legacy SharedKVPool attach. `pool_id` takes precedence, and an explicit `shared_prefix_n_tokens` only **caps** auto-P. | `polykv_api.md:209`, `llamafile-flags.md:234-235` |
| `chat_template_kwargs` | Must match between the pool render and the request (Qwen3 `enable_thinking`, Gemma `<\|think\|>`), or auto-P collapses to about 4 tokens. | `polykv_api.md:632-633`; `bench/package_courier/agent_app.py:741-748` |

### 1.4 Completion response

- **`opencoti` block**, present only when the request named a pool (T12):
  `{pool_id, n_pool_shared, pool_match, pool_len, pool_unknown}`.
  - `n_pool_shared: 0` with a pool named means the attach degraded to a full reprocess.
  - `pool_match < pool_len` means the request is not a prefix of the pool, and the server WARN log
    names the diverging token (0516 adds a hash/shape diagnostic).
  - `pool_unknown: true` (with `pool_match: -1`) means this process does not hold that id. It is
    never a refusal (`polykv_api.md:365-368`, `polykv_integration_guide.md:145-146`,
    `server_liveness.md:74-81`).
- **`timings`**: `cache_n`, `prompt_n`, `predicted_n`, `predicted_per_second`, `draft_n*`. The
  streaming final chunk has `timings` but no `usage` (`chat_model.py:90-103`).
- **Headers**:
  - `X-Context-Window: <granted>` on every admitted response. Absent means no guaranteed window.
  - `X-Context-Largest-Admissible` and `Retry-After: 2` on a window 429.
  - `X-OpenCoti-Boot-Id`.
  - `X-Sessions-Remaining`, only on an enforced+warn pool. It is omitted, not 0, when unknown.
  - `X-PolyKV-Settle-Waived: <ms>`.
  - `X-KV-Pool-Saturated` (poolless warn).
  - `X-Session-TPS`.
  - Sources: `llamafile-flags.md:240-248`, `elastic_parallel.md:1007-1011`.
- **Errors**:
  - 429 `rate_limit_error`: a window or admission refusal. The body carries `largest_admissible`, and
    with `kv_pressure_v1` also `error.pressure`.
  - 429 `session allocation full (worker of …)`: the worker's owner window is full. This is a queue:
    wait `Retry-After`, because waiting workers hold nothing.
  - 400 `exceed_context_size_error`, naming private vs shareable token counts under
    `polykv_private_window_v1`.
  - `truncated: true` stop at the window, or `session allocation full … compact the session` when
    the tree fills the window.
  - `error_kind: "evicted_kv_full"`: partial eviction, which is a bug per the user's ruling.
  - Sources: `polykv_integration_guide.md:84-91`, `cerebriline-polykv-integration.md:2078-2084`.

### 1.5 Admission and window semantics (`num_ctx` optional vs assured)

What a request is charged (`polykv_integration_guide.md:65-73`, as amended by 0345 and 0533):

| request | charged |
|---|---|
| new `session_id` + `num_ctx` | The whole window, held for the session until close or TTL (the "assured" mode). Refused whole rather than shrunk, unless `num_ctx_min`. |
| new `session_id`, no `num_ctx` | A **per-request** window, released at completion. Since 0345 it negotiates down to the largest that fits, floor = the request's peak. Since 0533: bounded → `peak + 1`; unbounded → slot max, negotiated down, floor `prompt + 257`. |
| no `session_id`, no `num_ctx` | Same as above, per-request. |
| `session_id` of a live allocation | Nothing (continuation). Always admitted. `num_ctx`/`num_ctx_min` are ignored. |
| `pool_id` of a pool **owned** by a live allocation | A *worker*: `max(1, prompt + n_predict − shared)` against the **owner's** free window. Books nothing server-wide. The worker sends no `num_ctx`. |
| `pool_id` of an **unowned** pool | Not a worker: the attacher books its own window (`num_ctx`, or per-request). The pool is charged once server-wide (`unowned_pools[]`). |

Further window rules:

- **Private window** (`polykv_private_window_v1`): `num_ctx` counts only private tokens. An attached
  pool rides above it, capped at `session_ctx_max`, so N leads on root P cost `N·W + P`
  (`polykv_integration_guide.md:82-88`, `:250-255`).
- **Resume rule**: re-send the granted window as both `num_ctx` and `num_ctx_min`
  (`polykv_integration_guide.md:79-80`).
- **Running priority** (0411, `README.md` 0411 section): a session with affinity, an allocation, or a
  turn launched within `OPENCOTI_ADM_RUNNING_TTL_S` (1800 s) passes every policy arm. Only the physical
  arms apply to it.
- **Two admission layers** (`polykv_api.md:369-379`):
  - per-pool policy (tps floor, settle, prefill backlog);
  - the per-request KV-cell gate (`--admission-poolless`, atomic reservation since c7, deterministic
    refusal counts; `bench/courier-admission/README.md:58-71`).

### 1.6 Pools (`/polykv/pools*`)

**Create**: `POST /polykv/pools`.

- Body: `{prompt | tokens | from_session | from_slot, session_id?, pin?, ephemeral?, expect_len?,
  shared?, unowned?}` (`polykv_api.md:207`, `polykv_integration_guide.md:108-118`, 0406).
- `prompt` is tokenized server-side with BOS + specials, exactly as a completion prompt.
  `/apply-template` output has no BOS, so the two agree.
- `from_session` makes a zero-copy `seq_cp` snapshot of that session's resident tokens. It must run
  **at end of turn before any other task** (bug-2219 residency contract, `polykv_fanout.md:57-66`).
  - A busy source slot defers the op; it does not 409.
  - Since 0340 a stale affinity no longer snapshots a foreign slot; a dropped session gives 400
    `unknown session_id (no slot affinity)`.
- `session_id` = the **owner**. It must be a live allocation, or the pool is unowned and the response
  carries a `warning` (0352).
- `shared: true` = find-or-create of an **unowned** root by exact tokens + the same `ephemeral`
  (returns `reused: true`). `shared` with any owner is a 400.
- `unowned: true` = no owner even for `from_session` of a booked session. Every attacher books its own
  window. `unowned` + `session_id` is a 400 (0406).
- `ephemeral` defaults: true for `from_session`/`from_slot`, false for `prompt`/`tokens`. An ephemeral
  pool is swept after it has been attached ≥1 time, is an unpinned leaf, and has had no processing
  attach for 60 s (`polykv_api.md:225-230`). A never-attached pool is not swept.
- Warnings: a pool adding < 64 tokens is created with a `warning`.

**Fork**: `POST /polykv/pools/{id}/fork`.

- Body: `{branch_pos?, prompt|tokens|from_session|from_slot, session_id?, pin?, ephemeral?, unowned?}`.
- `prompt`/`tokens` is the child's **full** `[0, L)`, checked token-exact (`std::equal`) against the
  parent over `[0, branch_pos)`. A mismatch is a **400** `contiguous-prefix contract violation`.
- `branch_pos` defaults to `parent.L` (extend). `branch_pos < parent.L` is a COW branch
  (`polykv_api.md:208-223`).
- The owner defaults to the parent's.
- End pool prompts at a newline. A trailing space merges across the boundary (`polykv_api.md:234-238`).

**Attach**: per request `pool_id`.

- auto-P = longest exact token match between prompt and pool. An explicit `shared_prefix_n_tokens`
  only caps it.
- Rebase (0352, `polykv_rebase_v1`): when the slot's own cache already covers the pool, the slot drops
  its `[0,P)` and shares the pool's. This needs unified KV, a full-pool match and an attention-only
  model. Otherwise the slot is "kept private (…)" (`polykv_integration_guide.md:130-140`).
- **Hybrid/recurrent** (Nemotron-H, LFM2, Qwen3.5/3.6 hybrids) share only on an exact full-state
  boundary (P = pool end). Any partial share is refused by design with a full reprocess, and forks
  must extend at `D == parent.L` (`polykv_api.md:239-243`, `:597-600`).

**Retention**:

- `POST /polykv/pools/{id}/pin` | `/unpin`.
- `POST /polykv/pools/{id}/release`, plus the alias `DELETE /polykv/pools/{id}` (`pool_delete_alias_v1`).
  Release is leaves-first: a parent with children refuses (`polykv_api.md:590-593`).
- Closing the owner session releases every pool it owns.

**Policy**:

- `POST /polykv/pools/{id}/admission` `{target_tps_per_session, mode: advisory|enforced,
  on_saturation: reject|warn, guarantee_min_sessions (1), settle_tokens (48), settle_max_ms (5000),
  prefill_max_slots (0)}`. `queue` → 400 (`polykv_api.md:321-328`). Defaults are from 0139.
- Gate order: guaranteed minimum → measurement settling → prefill backlog → kv headroom exhausted →
  projected mean tps below floor (`polykv_api.md:914-916`).

**Sampler placement**: `POST /polykv/sampling` `{mode: device|cpu|auto}` is server-wide and
observe-only. The legacy `/polykv/pools/{id}/sampling` 400s on an unknown id (`elastic_parallel.md:1047`,
`polykv_api.md:988-1050`).

**Pool JSON** (`GET /polykv/pools` → `{pools[], pools_max, seq_ids_used, tree_depth}`; `GET
/polykv/pools/{id}`): `pool_id, seq_id, parent, branch_pos, own_len, children, prefix_len, prefix_hash`
(FNV-1a identity only, not integrity), `pinned, ephemeral, attached_ever, created_ts, last_access_ts`
(epoch since c8), `source_session, orphaned_pin, owner`, `admission{}`. These are served from a
snapshot with `snapshot{age_ms, seq}`; `?live=1` forces the task path (`polykv_api.md:269-281`,
`read_routes_audit.md:21-39`). `OP_LIST` of one pool can also return token ids internally
(`with_tokens`, 0406).

**Pool create/fork errors**:

| error | meaning |
|---|---|
| 400 | contract violation; `shared` + owner; `unowned` + `session_id`; unknown `from_session` |
| 503 "no room in the KV cache for the pool prefill: needs N cells, F of T free" | Since 0412: before giving up, the engine first drops the owner's idle private slots and retries once. |
| sub-pool limit | "session sub-pool limit reached (N per slot)" |
| reservoir exhausted | "pool seq-id reservoir exhausted" |

### 1.7 Sessions (windows)

- **Close**: three equivalent routes, `POST /sessions/{id}/close`, `DELETE /sessions/{id}`, and
  `POST /sessions/close {"session_id"}` (the body form, for ids containing `/`).
  - Reply: `{session_id, found, cells, released, deferred, kv_dropped, running_kept}`.
  - With work still running the release is deferred until it drains.
  - The session's slot affinity is dropped at once.
  - Closing releases every owned pool.
  - Since 0362 it also drops the slot's KV sequences (`keep_kv: true` keeps them).
  - Since 0453, `keep_running: true` (or `?keep_running=1`) keeps the running marker, so the next turn
    is not a floor-gated new admission (`README.md:5196-5201`).
- **Resize**: `POST /kv/sessions/{id}/resize {num_ctx}`. Aliases: `/sessions/{id}/resize` and the body
  form `POST /sessions/resize {session_id, num_ctx}` (0394, `README.md:4086-4106`).
  - Success: 200 `{ok, window, window_new, cells, cells_new, cells_delta, used, sequences}`.
  - Refusals:
    - 404 `session_not_found`;
    - 400 `invalid_num_ctx` / `above_session_ctx_max`;
    - 409 `per_request_session` / `session_closing` / `session_busy` / `used_exceeds_window`;
    - 429 with `largest_admissible` + `pressure`.
  - `deferred: true` → 202 `{deferred, resize_pending, …}`, applied at the session's idle moment
    (0396, `README.md:4427-4446`).
  - Since 0422 (`kv_resize_live_v1`) a busy owner's resize applies live when `used + inflight` still
    fits. A shrink that cannot fit answers 409 with `inflight` + `smallest_window_now`
    (`README.md:4626-4636`).
- **Slot erase**: `POST /slots/{id}?action=erase` drops a slot's KV without closing the session.
  Cerebriline uses it after compaction (`cerebriline-polykv-integration.md:554-564`). It was 501
  without `--slot-save-path` on c7 (`:1601`), and 0362 says "erase is a pure KV drop and needs no save
  path".
- **TTL**: `--elastic-alloc-ttl` (300 s). Only a *finished* request of the owner resets the clock, so
  an owner whose workers are all waiting or in long tool calls lapses (Cerebriline
  `polykv-swarm.ts:895-903`, hence its 60 s owner keepalive).
- **Leaked reservations**: a request cancelled while queued used to leak its booking until a 600 s
  backstop. This is fixed in 0546, which also gives queued streams keepalives under load
  (`README.md:5801-5823`).

### 1.8 Status, pressure and telemetry

**`GET /kv`** (snapshot; `?live=1` = task). Top-level fields (`polykv_integration_guide.md:92-99`,
`elastic_parallel.md:1035-1039`):

- `kv_pool_cells`, `session_ctx_max`, `guaranteed`;
- `cells_total / allocated / charged / reserved / free / used`, `largest_admissible`;
- `swa{… cells_recyclable, cells_phys_free}`;
- `slots_live / slots_max`, `pools_max_per_slot`, `pool_seqs / pool_seqs_used`, `alloc_ttl_s`;
- `allocations[]`, `unowned_pools[]`, `refusals`, `pressure{…}`.

Each `allocations[]` row: `session_id, key, window, requested, cells, used, free, pressure` (raw
used/window), `pools, active, pending, per_request, closing, idle_s, resize_pending,
resize_pending_s, resize_pending_reason`.

`pressure` (0394) is `{window_s 60, refused_60s, refused_peak_max_60s, refused_needed_max_60s,
refused_min_needed_min_60s, last_refusal_at, last_refusal_age_s, refusals_total, idle_resident}`
(`README.md:4085-4091`). A worker refusal records base need 0, so `refused_needed_max_60s > 0` is the
signal for global pressure (`cerebriline-polykv-integration.md:1971-1974`).

**`GET /polykv/pools/{id}/capacity?expected_tokens=N&fold=1`** is per pool; there is no global
capacity route. Since c8 a plain GET is read-only, and only the admission decider passes `?fold=1`.
Field groups (`polykv_api.md:329-346`; the full dataclass is `opencoti_langgraph/client.py:70-171`;
TS twin `polykv-client.ts` `PolykvCapacity`):

| group | fields |
|---|---|
| verdict | `pool_id, can_admit, reason, folded` |
| projection | `projected_mean_tps_if_admitted, mean_active_tps, n_active_sessions, headroom_sessions` |
| KV | `kv_headroom_pct, kv_cells_free, kv_cells_total, marginal_tokens_mean, compaction_pressure` (0.70→1.0 ramp), `expected_tokens` |
| P7 settled admission | `settling, settle_remaining_ms, guaranteed, n_warming, n_pool_sessions, projected_mean_tps_model/_measured, drop_per_admit_ewma` (reporting only), `known_session, projected_idle_estimate` |
| P8 prefill tax | `n_prefilling, prefill_backlog_tokens, n_soon_sessions, projection_bias_ewma` |
| elastic | `slots_live, slots_max, slots_free, vram_free_mib, elastic_reason` |
| SWA | `swa_active, swa_cells_*, swa_seq_budget, swa_window, swa_capacity_sessions` |
| sampler | `sampling{…}` |
| 0338 window view | `pressure, window_cells, pressure_scope (session\|unallocated\|cache), owner`. On an owned pool the `kv_*` fields are the owner's **window**. |

The c12 plan records an open defect: at low floors (1–20 tps) admission over-admits until per-agent
decode collapses (`c12-release-plan.md:49`, `:1071`).

**`GET /polykv/tps`** is SSE. `?once=1` or `?stream=0` returns one JSON:
`{ts, aggregate_tps, mean_active_tps, n_warming, sessions:[{session_id, pool_id, tps_ewma, ctx_used,
ctx_total, ctx_headroom_tokens, alloc_key}]}` (`polykv_api.md:303-305`, `:645-648`,
`elastic_parallel.md:1045-1046`). `pool_id` is correct only since c8 (T1).

**Other routes**:

- `/slots`: per-slot `opencoti{tps_ewma, n_decoded_cum, session_id, n_pool_shared, alloc_key,
  sampling_*}`. `len(/slots)` is the ceiling, not occupancy.
- `/metrics`: three unlabelled gauges `opencoti_polykv_mean_active_gen_tps`, `…_active_sessions`,
  `…_tokens_decoded_cum` (`polykv_api.md:306-311`).
- `GET /elastic`: `{enabled, slots_live, slots_max, slots_floor, tps_floor, tps_floor_boot,
  vram_reserve_mib, hold_ticks, tick_ms, reason, grows, shrinks, drain_to}`.
- `POST /elastic {tps_floor | hold_ticks}` (`polykv_integration_guide.md:153-168`).
- `/tokenize` defaults to `add_special=false`, while serving tokenizes with `add_special=true,
  parse_special=true` (`polykv_api.md:252-262`).
- `/apply-template` renders messages + tools with the server's template, without a BOS.

### 1.9 Lifecycle invariants that bite

1. **Pools are immutable.** "Extending" always means a fork. Compaction is a prompt rewrite, never an
   in-place KV op (`polykv_api.md:116-131`, `:711-732`). Two protocols exist:
   - **compact-by-refork** (P6c): fork the stable ancestor with summary + tail, migrate, release the
     old subtree. Implemented as `compact_by_refork`;
   - **compaction-as-continuation** (Cerebriline §11 i/j): the next section's design uses this one.
2. **Ephemeral disappearance is silent** (T11). Pin anything that must survive more than 60 s idle,
   or re-create it. Detect loss via `opencoti.n_pool_shared` / `pool_unknown`.
3. **Restart**: `boot_id` changes and pool ids restart at 0. A stale id may name *another* prefix
   (`server_liveness.md:62-66`, Cerebriline `cerebriline-polykv-integration.md:698-713`).
4. **Per-session values must stay out of the system turn.** Tools render after the system text, so a
   date or cwd there kills sharing below it (`polykv_integration_guide.md:48-50`, `:262-274`).
5. **One engine session id per agent**, never a borrowed one (`polykv_integration_guide.md:23-26`).
6. **Abort abandoned streams.** A non-aborted stream keeps decoding into a booking
   (`cerebriline-polykv-integration.md:126-129`, `:1342-1351`).
7. **Batch-shape non-determinism**: greedy output depends on server load and cache mode, on PolyKV's
   only code path (bug-3556/3586, `c12-release-plan.md:270-281`). `--swa-full` restores bit-exactness
   on Gemma.

### 1.10 PolyKV through xOllama

- **Detect**: `GET /api/xollama` → `{version, features[]}`, 404 on stock Ollama. The relevant features
  are `client_placement_v1`, `context_window_v1`, `chat_render_v1`, `api_key_v1`
  (`api/xollama_identity.go:30-87`, `server/identity.go:36-38`). Per model, `POST /api/show` →
  `xollama.council` (a council model, which owns its pools and is **out of scope**) and the model
  config `xollama.session.{affinity, pool, max_pools, client_pools}` (`types/xollama/config.go:200-226`,
  `api/types.go:950-956`).
- **Client pools need seats**: `session.client_pools > 0` (env `XOLLAMA_POLYKV_CLIENT_POOLS`, default
  0). Without seats the engine refuses client creates (`types/xollama/config.go:217-226`,
  `model-settings.mdx:80`).
  - Sizing: a lead with a booked window holds 2; each swarm tree holds 3.
  - xOllama's own automatic pooling (`session.pool`, off by default) builds a pool from a prefix two
    conversations were observed to share, ≥ 256 tokens, held until unload (`sessions.mdx` "How a pool
    comes to exist").
- **Passthrough**: `GET|POST|DELETE /api/engine?model=M&endpoint=<route>` (`server/routes_engine.go:81-177`).
  - The response is wrapped as `{model, engine, method, endpoint, status, body|text}`. The engine's
    own status is inside, and a 404 is information. SSE (`polykv/tps`) streams through.
  - Other query params are forwarded (`?live=1`, `?once=1`).
  - Timeouts are 30 s for reads and 10 min for control.
  - The route table, `llm/engine_introspect.go:69-109`, is exactly:
    - reads: `props, slots, metrics, health, models, lora-adapters, kv, elastic, polykv/pools,
      polykv/pools/{id}, polykv/pools/{id}/capacity, polykv/tps, gpu/peers, lock/status`;
    - prompt building: `POST tokenize, detokenize, apply-template`;
    - control: `POST props, lora-adapters, slots/{id}, polykv/pools, polykv/pools/{id}/{action},
      DELETE polykv/pools/{id}, POST polykv/sampling, elastic, sessions/close, sessions/{id}/close,
      DELETE sessions/{id}, POST sessions/resize, sessions/{id}/resize, kv/sessions/{id}/resize,
      lock/acquire|release|renew`.
  - IDs must match `^[A-Za-z0-9_~-][A-Za-z0-9._~-]{0,255}$`; anything else uses the body forms.
  - Inference is **not** proxied.
- **Chat fields** (native `/api/chat` only; nothing in `openai/*.go` carries them):
  - top-level `session_id` (else xOllama derives `xo-<sha256(model, system msgs, tool names, first
    user msg)>`, `llm/engine_session.go:81-111`);
  - `placement{pool_id, num_ctx, num_ctx_min}` (`api/xollama_placement.go:13-20`,
    `llm/engine_council.go:44-48`).
  - **Never** `options.num_ctx`: it sizes the model load, and changing it reloads the runner
    (`sessions.mdx` "The session's window").
  - A 429 with `X-Context-Largest-Admissible` comes back at once for a request that stated
    `placement.num_ctx` or `placement.pool_id`. Otherwise Ollama's queue applies (up to 2 min, then 503).
- **Rendering**: xOllama may render the chat itself (a Modelfile TEMPLATE or a renderer). Cerebriline
  therefore cuts pool prefixes from `/api/chat {_debug_render_only}` → `_debug_info.rendered_template`
  (`chat_render_v1`), sent with the chat's own `options`, rather than from the engine's
  `/apply-template` (`cerebriline-polykv-integration.md:2351-2358`).

---

## 2. Coverage matrix: what Cerebriline uses

Legend: **Y** = used; **P** = partial or gated; **N** = not used. "Where" points at Cerebriline code or
at its record (`CPI` = `/shared/dev/docs/cerebriline-polykv-integration.md`).

| surface | Cerebriline | where / note |
|---|---|---|
| `/props.features` gating | Y | `polykv.ts:872-975` (`OPENCOTI_FEATURES`, `hasOpencotiFeature`) |
| `boot_id` (`/health`, `/props`, header) | Y | restart generations, `CPI:705-732` |
| `pools_enabled` probe | Y | `probePolykvEnabled` `polykv.ts:1140` |
| create `prompt` | Y | layers cut from `/apply-template` at `SENTINEL` (`polykv-swarm.ts:553`, `CPI:389-406`) |
| create `tokens` | N | type exists (`polykv.ts:360-370`); tokens are never built client-side |
| create `from_session` | Y | continuation compaction freezes P (`CPI:2141-2142`) |
| create `from_slot` | N | type only (`polykv.ts:369-370`) |
| `expect_len` | N | type only |
| `session_id` (owner) on create/fork | Y | swarm owners and lead sub-pools |
| `shared: true` | Y | lead root find-or-create (`CPI:1181-1189`) |
| `unowned: true` | P | wired in types (`polykv.ts:39`), recorded as "not used yet" (`CPI:2243`) |
| `ephemeral` | Y | shared root `ephemeral: true` |
| `pin` / `unpin` | Y | pinned layers; `pinPrefix` read since `7f7a1f4d2` (`CPI:2456-2459`) |
| fork `branch_pos` | Y | P' at `min(writer cache_n, len P)` (`continuation-compaction.ts:935`) |
| COW branch (D < L) | N | only extends; branches only via `branch_pos` = writer cache_n |
| release / DELETE alias | Y / N | POST release; DELETE not used |
| `/polykv/pools` listing (`orphaned_pin`, owner) | Y | verify root, reclaim (`CPI:714-726`, `:414-420`) |
| `/capacity` (`can_admit`, `headroom_sessions`) | Y | lead admission controller (`CPI:1542-1551`) |
| `/capacity ?fold=1` | P | supported, folds only when deciding (`polykv.ts:473-482`) |
| `/capacity ?expected_tokens` | Y | `polykv.ts:1344` |
| `/capacity` `pressure` (`session_pressure_v1`) | Y | compaction trigger fallback (`CPI:1287-1292`) |
| `POST …/admission` | Y | swarm pools get the node's policy (`CPI:1558-1562`). Not posted on lead pools (`CPI:1641-1646`). |
| `POST /polykv/sampling` | N | 0 hits |
| `/polykv/tps` | P | read for the status strip (`polykv.ts:63`) |
| `/elastic` GET | P | status only (`polykv.ts:893`); never POSTs a floor |
| `/metrics` | N | |
| `/kv` allocations, `session_ctx_max`, `largest_admissible` | Y | owner sizing, room moves (`CPI:367-371`, `:449-451`) |
| `/kv.pressure` (global) | Y | `kv-pressure.ts`, `opencoti-kv-pressure.ts:116,251` |
| `idle_resident`, SWA `cells_phys_free` | Y | `kv-pressure.ts:127-168` |
| `?live=1` | N | the snapshot is enough (`CPI:2095-2097`) |
| `num_ctx` / `num_ctx_min` | Y | lead (when `dynamicContextSize`), owners, unpooled agents (`CPI:1799-1809`) |
| resume rule (grant = num_ctx = num_ctx_min) | Y | `CPI:1233-1237` |
| private window (W − P) | Y | `CPI:1196-1200` |
| `pool_id` on workers | Y | `opencoti.ts:1506-1512` worker fetch |
| `continue_pool` | Y | critics/retrospective/synth on P' (`continuePoolBody`, `opencoti.ts:2393`) |
| `overcommit` | P | xOllama settings panel exposes it (`config.ts:453`, `CPI:2428-2431`) |
| `stream_options.keepalive` + `sse_ping_interval: 10` | Y | `opencoti-liveness.ts`; dead after `3×ping+5 s` = 35 s, `/health` asked first (`CPI:1300-1333`, `:2035-2050`) |
| `return_progress` | N | 0 hits; the keepalive phase comments replace it |
| `opencoti.n_pool_shared` | Y | summed per agent (4.100.235, `CPI:7-13`) |
| `pool_match` / `pool_len` | Y | warn on a mismatch |
| `pool_unknown` | Y | rebuild one chain or the root |
| `X-Context-Window` | Y | grant recording |
| `X-Context-Largest-Admissible` | Y (body or header) | one retry at it (`CPI:1224-1229`) |
| `X-Sessions-Remaining` | N | |
| `X-PolyKV-Settle-Waived` | P | parsed (`polykv.ts:877`) |
| session close (path/body) | Y | body form for `~` ids; `/ \ ? # %` → `~` (`CPI:492`) |
| `keep_running` | Y | writer-booking close (`polykv.ts:1272`) |
| `keep_kv` | N | |
| resize immediate / deferred / live | Y / Y / Y | `kv-pressure.ts:665` `shrinkForKvPressure`; owner grow on a worker 429 (`CPI:1952-1983`) |
| `/slots/{id}?action=erase` | Y | after compaction (`polykv.ts:454`) |
| `/tokenize` | Y | xOllama side calls (`CPI:2498-2500`) |
| `/apply-template` | Y | layers; on xOllama `_debug_render_only` instead |
| `evicted_kv_full` | Y | classed `kv_evicted` (`CPI:2078-2084`) |
| `seed` / `temperature` per agent | Y | `spawn-sampling.ts` (§4) |
| `compact_by_refork` | N | replaced by continuation compaction ("no re-root fork", `CPI:62`) |
| COW fork for divergent toolsets | N | |
| fan-out/digest/reducer (FS-H1..H3) | N | |

Cerebriline's own open gaps (`CPI:1636-1699`, `:2101-2103`):

- `n_pool_shared 0` with a pool named is not warned.
- The policy is not posted on lead pools.
- `releaseAllPolykvSwarms` has no production caller.
- `releasePolykvAgent` ignores `found`.
- An owner can't exceed `session_ctx_max` (mail 711, `CPI:2052-2062`).

**What "better than Cerebriline" can mean for us** concretely:

1. Use `unowned`, COW branches and `from_session` + `continue_pool` everywhere a turn continues.
2. Post an admission policy on every pool we read capacity from.
3. Warn on `n_pool_shared == 0`.
4. Make release provably complete (check `found`, a final `/kv` + `/polykv/pools` audit per run).
5. Persist pool state across our own engine restarts, which Cerebriline does not need.
6. Exploit the council's natural tree, described in §5.

---

## 3. `opencoti_langgraph`: what we can reuse

**Location and pins.** The package is at `/shared/dev/opencoti/opencoti/langgraph/opencoti_langgraph/`,
installed editable in the `opencoti` conda env. Version pins (`pyproject.toml:80-84`):

| env | langchain-core | langgraph | httpx |
|---|---|---|---|
| package requires | ≥ 1.4.9 | ≥ 1.2.9 | ≥ 0.28 |
| `claude-hooks-consultants` (measured today) | 1.4.0 | 1.2.0 | 0.28.1 |

So importing it there would force a langchain/langgraph upgrade. **Our engine does not use
`BaseChatModel`**: a grep finds `langchain_core` only in `consultants/engine/events.py`, and every role
talks through `ChatClient.chat(payload)`.

**Tests**: 61 offline (`tests/test_client.py` 16, `test_chat_model.py` 25, `test_graph.py` 20).
Live gates: `.opencoti/p5-langgraph-gate.sh` and `.opencoti/fsh3-fanout-gate.py` (`README.md:53-64`).

| piece | file:line | reuse verdict |
|---|---|---|
| `PolykvClient` / `AsyncPolykvClient` | `client.py:306-553` | **Port, don't import.** It is httpx-only and the shapes are right, but it covers only the c8 surface: no `session_id` (owner) / `shared` / `unowned` on create/fork, no `/kv`, `/sessions/*close`, resize, `/apply-template`, `boot_id`, `continue_pool`, or the xOllama envelope. Copy the payload builders (`_create_body` :187, `_fork_body` :216, `_admission_body` :243) and the error mapping (`_raise_for_response` :282) into a stdlib/urllib client that matches our `chat_client.py` style. |
| `Capacity` dataclass | `client.py:70-171` | Reuse verbatim. Add the 0338 window fields (`pressure, window_cells, pressure_scope, owner`), which it lacks. |
| `OpencotiChatModel` | `chat_model.py:214-700` | Don't adopt; port its techniques into `LlamafileAgentChatClient`: |
| | | • `_StallWatch` (two budgets: first-token vs idle) `:106-138` |
| | | • `_is_live` counting `prompt_progress` `:78-87` |
| | | • keepalive expiry 2 s below the engine's 5 s httplib idle, to avoid the RemoteProtocolError race `:259-270` |
| | | • one retry on `RemoteProtocolError` before the first chunk `:595-601` |
| | | • usage derived from `timings` on the streaming arm `:90-103` |
| | | • `with_pool()` `:695-700` |
| `make_capacity_gate` | `graph.py:94-159` | The pattern is reusable (fold only in the decider). We'd run it inside our Send router, not as a node. |
| `make_compaction_node` / `compact_by_refork` | `graph.py:162-208`, `client.py:422-449` | Reference only. We prefer continuation compaction (§5.7), which Cerebriline measured as better (`CPI:2202-2207`, `:2289-2296`). |
| `make_fanout_node` / `make_reducer_node` / `make_commit_node` + `DIGEST_INSTRUCTION` / `REDUCER_INSTRUCTION` / `parse_digest` | `graph.py:51-91`, `:211-444` | **Directly relevant**: one pool snapshotted `from_session`, N workers with own session ids, and a reducer on the same pool "knowing what main already knows". This maps onto our researcher fan-out → critic/synthesizer. Keep the "preserve concrete results verbatim" reducer clause (`polykv_fanout.md:179-183`). |
| `make_pool_tools` | `tools.py:22-84` | Not needed: our council's pools are engine-managed, not model-managed. |
| bench `make_pool` (template-agnostic LCP of two `/apply-template` renders with the workers' `chat_template_kwargs`) | `bench/package_courier/agent_app.py:725-775` | Reuse the algorithm. On hybrids, cut at the template boundary with a sentinel instead (`polykv_api.md:252-262`). |
| `hysteresis.py` (spawn/shrink controller over `/capacity`) | `bench/package_courier/hysteresis.py:142-597` | Optional, for the low-floor over-admission the c12 plan records. |

---

## 4. Sampling diversity for several pooled sessions of one model

**Mechanism.** A pool shares only KV prefix cells. Sampling is per request, so diversity comes from
per-session sampler fields. Exact defaults found:

**Cerebriline** (`sdk/packages/core/src/extensions/tools/team/spawn-sampling.ts`, shipped `9078bd9f2`,
4.100.195; `CPI:314-319`):

- Absent means **no value is written**: the model's own sampler stands (`:3-9`).
- A numeric `seed` covering several agents is **offset by index**: `seed + i`, so count 3 with seed 7
  gives 7, 8, 9 (`:219-235`, `:132`).
- `seed: "random"` draws an independent seed per agent, uniform in `[0, 2^31)` (`RANDOM_SEED_LIMIT`,
  `:59-60`, `:280-281`).
- `temperature: "random"` (or any `temperature_range`) draws `base × (1 + d × range/100)` with
  `d ∈ U[-1, 1)`, rounded to 3 decimals, floored at 0 (`:286-300`, `:387-399`).
  - `DEFAULT_TEMPERATURE_RANGE_PERCENT = 2`, so a model at 1.0 draws 0.98–1.02 (`:54`).
  - `MAX_TEMPERATURE_RANGE_PERCENT = 10`, a hard clamp (`:57`). A lead once asked for 60 % and ran at
    0.595 on a model validated at 1 (`:28-31`).
- `base` comes from, in order: the connection's temperature, then the `providerConfig.sampling`
  bag, then Ollama `/api/show` Modelfile, then opencoti `/props` default generation settings.
  If none states one, the temperature is left unset with the note "model temperature unknown; kept the
  model's sampler" (`:313-360`, `:63-64`).
- The draw is made once per agent (it survives re-placement) and realised at build. What ran is
  reported in the agent's row (`:33-37`).
- On the wire: opencoti gets `seed` top level (`llamacpp-sampling.ts:40`); Ollama/xOllama get
  `options.seed`.

**opencoti**: no seed or temperature diversification exists in `opencoti_langgraph`.
`OpencotiChatModel` takes a single `temperature` (`chat_model.py:223`). The benches run every worker at
`--temperature 0.0` (`bench/package_courier/agent_app.py:942`, `bench/a2a_protocol/agent_app.py:968`)
with a fixed seed 1 in the engine (`bench/courier-admission/README.md:9-10`). Fan-out diversity there
comes from **different task suffixes**, not from sampling.

**Caveats that matter for us**:

- Greedy or low-temperature output on pooled multi-slot servers is already
  non-deterministic run to run (batch shape, KV view, MoE routing flips; bug-3556/3586,
  `c12-release-plan.md:218-281`). Seed offsets therefore guarantee *distinct* streams, not
  *reproducible* ones.
- Our shipped sampling for `glm-5.3*` / `deepseek-v4.1-flash*` is temperature 0.7
  (`config/model-sampling.json`, `.claude/rules/model-sampling.md`). The ±2 % jitter must be applied
  over the `model_sampling` resolved value, never replace it.

---

## 5. Design: PolyKV for the consultants council

### 5.1 Goals and hard constraints

- **Zero behaviour change when off or unsupported.** Ollama cloud, stock llama.cpp and Ollama without
  xOllama must get byte-identical requests ("off means off", xOllama `engine_session.go:121-133`).
- **Preserve x-tier lanes end to end** (memory: x-tier diversity is the point). PolyKV only
  de-duplicates prefill for lanes that land on the same engine+model.
- **Every new state key** is declared in `CouncilState` and carried in each `Send` dict. LangGraph
  drops undeclared keys and Sends deliver only their own keys (`graph.py:983-988` comment;
  `.claude/rules/consultants-runtime.md`).
- **Durable across our engine restart** (suspend/resume via `consultants-inflight.json` +
  `checkpoints.db`) **and across the opencoti engine restart** (`boot_id`).
- **Nothing leaks**: every run ends with zero allocations and zero non-shared pools of its own on `/kv`
  and `/polykv/pools` (Cerebriline's gate criterion, `CPI:2209-2210`).

### 5.2 Backends and modes

Per (engine root, model) the manager probes once per run. Results are cached per `boot_id`.

| backend | probe | transport for control | transport for chat |
|---|---|---|---|
| **opencoti direct** (`llamafile://label` whose binary is opencoti, or a configured opencoti URL) | `GET /props` → `features`, `opencoti.polykv.pools_enabled`, `boot_id` | `http://host:port/<route>` | `/v1/chat/completions` (`LlamafileAgentChatClient`, `chat_client.py:972-1185`) with body extras |
| **xOllama + opencoti engine** | `GET /api/xollama` (`client_placement_v1`, `context_window_v1`, `chat_render_v1`), `POST /api/show` (not a council, `session.client_pools ≥ 4`), then `/api/engine?model=M&endpoint=props` | `/api/engine?model=M&endpoint=…`, unwrap `{status, body}` | native `/api/chat` (`ChatClient`, `chat_client.py:230-460`) with top-level `session_id` + `placement{}` |
| anything else | the probe fails | none | unchanged |

Modes, chosen per run from config + probe:

| mode | needs | what is sent |
|---|---|---|
| `off` | default when the probe fails or `[polykv] enabled=false` | nothing new |
| `affinity` | `session_id` support only | a per-role-lane `session_id`, so tool-loop turns return to their warm slot |
| `pooled` (num_ctx optional) | `polykv_subpools_v1`, `pool_unowned_v1` or `shared_root_v1` | unowned pools. Every role call books its own per-request window (no `num_ctx`). No owner to keep alive and no TTL risk. The engine can refuse mid-run (429 → wait, as for any rate limit). |
| `assured` (default when `elastic_guaranteed_alloc_v1` + `ctx_min_negotiation_v1` + `session_close_v1` + `kv_status_v1`) | the above | one **owner** window per run per root. All role calls are **workers** (pool_id, own session, no num_ctx). Cells for the whole council are booked up front; a worker 429 "session allocation full" is a queue. |

### 5.3 `KvRunManager` (new, e.g. `consultants/engine/polykv.py`)

One instance per run (and follow-up), created in `run_council` / `run_follow_up` next to
`grounding_msgs` (`runner.py:190-192`, `:840-853`), handed to the chat clients through
`GraphDeps` (the same way the recorder is). It holds:

- `roots: {root_key → RootState{base_url, transport, boot_id, generation, features, owner_sid,
  owner_grant, pools{layer_key → PoolRef{pool_id: str, prefix_len, parent, pinned, generation}}}}`.
  Pool ids are held as **strings**, so pool 0 survives `if pool_id`.
- `wire_session(role, lane_idx, round, model) → str`, spelled from `[A-Za-z0-9._~-]` only (xOllama's
  id regex, Cerebriline's `~` mapping): `<sid>~own-<n>` (n process-wide, never reused),
  `<sid>~pl`, `<sid>~r<lane>`, `<sid>~te<i>`, `<sid>~cr<round>`, `<sid>~adv`, `<sid>~syn`, `<sid>~cod`,
  `<sid>~cc<n>-<part>`.
- `layer(role, messages) → pool_id | None`: build or attach the pool chain for this request (§5.4).
- `wrap_payload(role, lane, payload) → payload`: add `session_id`, `pool_id`, or `continue_pool`,
  plus sampling diversity (§5.6) and `stream_options.keepalive`. It strips `num_ctx` for workers.
- `observe(response)`: read `X-Context-Window`, `X-OpenCoti-Boot-Id`, and `opencoti{n_pool_shared,
  pool_match, pool_len, pool_unknown}`. A boot-id change or `pool_unknown` invalidates the generation
  or that chain. `n_pool_shared == 0` with a pool named is **WARN** (the gap Cerebriline still has).
  Per-role shared tokens are added to the recorder's `llm_call` rows.
- `release_all()` (idempotent, §5.10) and `snapshot()` / `restore()` for durability (§5.9).

The manager sits **beside** the chat clients, not inside `make_agent_chat_client`. That keeps the
model-routing factory unchanged; the clients just call `mgr.wrap_payload` when one is attached.

### 5.4 Shared-prefix layout: change the council's prompts so roles share a head

Today every role starts with its own system prompt:

- planner: `[PLANNER_SYSTEM, user]` (`council.py:697-701`);
- researcher: `grounding_msgs + [RESEARCHER_SYSTEM] + [user(question, plan, …)]` (`council.py:730-750`);
- critic: `[CRITIC_SYSTEM, user]` (`:770-773`);
- synthesizer: `[SYNTH_SYSTEM, user]` (`:810-813`).

So across roles the first differing token is about token 1, and nothing can be shared except the
researcher lanes' grounding. `build_grounding_messages` emits up to four **system** messages: the
addendum, structure map, anchors and extended sources (`claude_hooks/caliber_proxy/prompt.py:459-520`).

Adopt Cerebriline's "static system turn + layered user turns" shape (`CPI:326-347`), on PolyKV
backends only (other backends keep today's exact shape). Rendered through the server template, the
tree for one run on one root is:

```
P0  [system: COUNCIL_BASE (static, no cwd/date/sid)] + tools        unowned, shared:true, ephemeral
 │                                                                  (one per server+model+toolset)
 └ P1  + user "<grounding>…</grounding>" + user "QUESTION: …"        owned by <sid>~own-1, pinned
     ├ planner  = P1 + user "# Your role: planner …"                worker <sid>~pl
     └ P2  + user "PLAN: …" (built after the planner returns)        owned, pinned
         ├ P2r + user "# Your role: researcher …"                    owned, pinned (lanes share it)
         │    ├ lane i = P2r + user "Sub-research lane i: <item>"   worker <sid>~r<i>, tool loop appends
         │    └ tool_executor j = P2r + "# tool plan item j"         worker <sid>~te<j>
         ├ coder   = P2 + user "# Your role: coder …"                worker <sid>~cod
         └ P3  + user "RESEARCHER REPORTS: …" (after fan-in)         owned, pinned
              ├ critic      = P3 + "# Your role: critic …"          worker <sid>~cr<round>
              ├ adversary   = P3 + "# Your role: adversary …"       worker <sid>~adv
              └ synthesizer = P3 + critique + "# Your role: synthesizer …"  worker <sid>~syn
```

Layer construction rules:

1. Render `messages[0..d] + [user SENTINEL]` via `/apply-template`, or via `_debug_render_only` on
   xOllama, with the request's own `tools` and `chat_template_kwargs`/think fields. Cut at the
   sentinel; the cut includes the next turn's opener (`polykv-swarm.ts:553`, `CPI:389-399`).
2. Check `full_render.startswith(cut)` before creating or attaching (`polykv_integration_guide.md:34-36`).
3. Create P0 with `{prompt, shared: true, ephemeral: true}` and never release it.
4. Fork each deeper layer `{prompt: full [0, L), session_id: owner, pin: true}`.
5. A refused layer (contract 400, sub-pool limit, 503 no room) stops the chain; attach to the parent.
   Retry after 5 s, not cached forever (Cerebriline `7a77fec6c`, `CPI:405-420`).
6. **Toolsets differ by role.** Researchers are tooled; planner/critic/synth are not unless
   `[tools] all_roles`. Tools render inside the system turn, so either:
   - (a) send the **same** `tools` array to every role on a PolyKV root, with `tool_choice: "none"`
     for non-tooled roles. Simplest, one P0, but it changes prompts.
   - (b) keep two P0s (with/without tools), which the bench LCP algorithm finds automatically.
   - (c) **COW-fork** a tools-less branch at the system-text boundary (`branch_pos = D < L`,
     attention-only models).

   Recommend (b) by default and (a) behind a flag after an A/B. Cerebriline never uses COW branches,
   so (c) is an area where we can do better on attention models.
7. **Hybrid/recurrent models**: only exact full-state boundaries share. Cut at the template boundary
   with a sentinel and never by a two-sample LCP, and never COW (`polykv_api.md:252-262`, `:597-600`).
8. Grounding must stay byte-identical across a run and its follow-ups. It already is (a file-system
   read at run start); cache the rendered string in the run state.

Measured effect elsewhere (12 agents, 15k-char knowledge: each agent prefilled 33–37 tokens of
~6,133; owner used 6,625 cells for all twelve, `polykv_integration_guide.md:222-227`). For us the big
win is that P1 (grounding, often tens of thousands of tokens) is prefilled once per run instead of once
per lane, critic and synthesizer.

### 5.5 Role → session/pool mapping

| role / call | session | attaches | books | notes |
|---|---|---|---|---|
| owner open | `<sid>~own-<n>` | — | `num_ctx` = sized (§5.12), `num_ctx_min` = floor, `max_tokens: 1` | carries the real P0 system + tools (`polykv_integration_guide.md:207-208`) |
| planner | `<sid>~pl` | P1 | worker | re-entry (critic re-route) reuses the session |
| researcher lane *i* (each model of the x-tier set) | `<sid>~r<i>` | P2r | worker | Its tool loop is pure append in its own session. `compact_messages` must **pin the head turns** (Cerebriline `pinConversationHead`, `CPI:524-527`) so a compacted history still starts with P2r's exact tokens. |
| tool_executor item *j* | `<sid>~te<j>` | P2r | worker | short; close at the answer |
| coder | `<sid>~cod` | P2 | worker | sandbox unchanged |
| critic round *k* | `<sid>~cr<k>` | P3 | worker | strictness dial text goes after P3 |
| adversary / refuter | `<sid>~adv` | P3 | worker | |
| synthesizer | `<sid>~syn` | P3 | worker | fallback models on other backends are unaffected |
| truncation continuation (`_single_shot` continues a cut answer, `council.py:1155-1180`) | same session | `from_session` snapshot of the cut turn → `continue_pool: true` + `[user "continue"]` | worker | `cache_n == len(pool)` by construction (0406) |
| retrospective / compaction summaries | `<sid>~cc<n>-writer` etc. | §5.7 | worker or exact booking | |

**Every role session is closed** (`/sessions/close` body form) the moment its last call answers.
Workers book nothing, but closing drops their slot KV (0362). Sessions that may continue are kept to
run end: a researcher lane until fan-in, the critic until synthesis.

### 5.6 Fan-out pacing and diversity

- **Capacity before Send.** In `_fanout_after_planner` (`graph.py:975-1013`), when the manager is in
  `assured`/`pooled` mode, read `GET /polykv/pools/{P2r}/capacity?expected_tokens=<lane peak>&fold=1`
  once. Only this decider folds.
  - Emit all Sends as today (lanes must not be dropped), but each lane's client acquires a manager
    semaphore sized `max(1, headroom_sessions)`, re-read after a sibling finishes or after
    `settle_remaining_ms`.
  - Waiting lanes hold nothing, and the stall monitor must not count that wait. It is queued time, so
    use `first_token` budgets à la `_StallWatch`.
  - Post an advisory policy on P2r first: `POST …/admission {target_tps_per_session: <floor>,
    mode: "advisory", guarantee_min_sessions: 1, prefill_max_slots: <lanes/2>}`. Cerebriline does not
    do this on its lead pools (`CPI:1641-1646`).
- **Worker 429 "session allocation full (worker of …)"**:
  1. grow the owner by the shortfall via resize (`deferred: true`, or live with `kv_resize_live_v1`);
  2. wait `Retry-After` for 4 waits, then double to 30 s, with no deadline (Cerebriline constants,
     `polykv-swarm.ts:567-570`).

  Never read a worker refusal as global pressure (`CPI:1971-1974`).
- **Owner keepalive.** Only a finished owner request resets the TTL. While any lane is waiting or in a
  long tool call, send the owner a 1-token continuation every 60 s (`polykv-swarm.ts:895-903`), or
  open the owner with TTL semantics `-1` if the engine is ours to configure.
- **Diversity** applies when ≥2 concurrent calls of one model share a pool: x-tier lanes whose
  `models_per_lane` contain the same tag, multiple tool_executor items, a critic panel.
  - Seed: `seed = base_seed + lane_idx`, with `base_seed` drawn once per run in `[0, 2^31)` and stored
    in run state, so a resume replays the same seeds.
  - Temperature: jitter ±2 % around the `model_sampling` temperature, clamp 10 %.
  - Default **on** for pooled same-model lanes. Config `[polykv] seed_offset=true,
    temperature_jitter_pct=2`; settable from both `/consultants config` and `claude-consultants config`
    (memory: configurable = skill + CLI).
  - The planner, synthesizer and anything single-instance get no jitter.
- **Liveness.**
  - On opencoti direct, send `stream: true` + `stream_options.keepalive: true` + `sse_ping_interval: 10`
    + `return_progress: true` when `stream_keepalive_v1` is advertised.
  - Make `chat_streamed` (`claude_hooks/get_advice/chat_client.py`, per the runtime rule) count
    `: keepalive *` comments and `prompt_progress` as liveness.
  - Dead = 3 silent periods + 5 s. Ask `/health` before resending (`CPI:2046-2050`).
  - Always abort the HTTP stream on an abandoned call (Cerebriline rule 14).

### 5.7 Compaction and long tool loops

- **In-loop middle-drop** (`_loop_payload_budgeter` → `compact_messages`, `council.py:1128-1147`,
  `budget.py:388-400`). Keep it, but on PolyKV backends treat the pooled head turns as "leading" so
  they survive. The compacted request then still attaches P2r (auto-P matches the head) and only the
  rewritten tail is prefilled.
- **LLM summaries (retrospective)** should use Cerebriline's continuation compaction
  (`continuation-compaction.ts:593+`; measured 0 cells booked and 0 left behind on b124, `CPI:2286-2297`):
  1. freeze P = `POST /polykv/pools {from_session: <lane wire id>, pin: true}` at end of turn;
  2. writer = the session's next turn (`tool_choice: "none"`, same system/tools);
  3. P' = fork P `{from_session, branch_pos: min(writer cache_n, len P), pin: true}`;
  4. critics/synth on P' with `continue_pool: true`;
  5. release leaves first;
  6. erase the slot (`/slots/{id}?action=erase`) instead of closing a held session, or close with
     `keep_running: true` (0453), so the next turn is not floor-gated.

  Constants to mirror: `BOOKING_ALIGN 256`, `BOOKING_CHARS_PER_TOKEN 3`, `BOOKING_SLACK_TOKENS 512`,
  `WRITER_INSTRUCTION_ROOM_TOKENS 3072`, `REFUSAL_BACKOFF_MS [2,4,8,15,30]s`
  (`continuation-compaction.ts:99-126`).
- **Pressure triggers**: compact when `/kv` row `pressure ≥ 0.85` (`POLYKV_COMPACTION_PRESSURE`,
  `polykv-session.ts:68`), read at most every 5 s (`:424`). Global pressure is `refused_needed_max_60s
  > 0` within `window_s`. Shrink after compaction with `kv-pressure.ts` rules (`KV_SHRINK_FILL 0.6`,
  `KV_GROW_AT 0.75`, `KV_SHRINK_MIN_CELLS 4096`, `KV_SHRINK_MIN_SHARE 0.1`, `kv-pressure.ts:70-103`).
  For a short-lived council this matters only at `max` effort.

### 5.8 Follow-ups (`runner.py:748-853`)

- Phase-5 follow-ups extend the parent's per-role threads (`prior_messages_by_role` for researcher and
  synthesizer).
- With PolyKV: when the follow-up starts on the same root with an unchanged `boot_id`, and the
  parent's owner is still live (`/kv` row), the follow-up **adopts** the parent's owner and P1. P1 is
  reused only if grounding + question are identical; otherwise fork P1′ from P0.
- A parent researcher/synth thread is continued with `continue_pool: true` on a `from_session`
  snapshot taken at the parent's end of turn (§5.10 keeps those snapshots for the hold).
- **Hold**: at parent settle with `consultancy_status == ready_to_review`, keep the owner + P1/P2/P3
  pinned for `followup_hold_s` (default = engine `alloc_ttl_s`, 300 s, read from `/kv`), so the
  review loop's auto-follow-up (cap `max_followups`, default 4) lands warm.
  - Release on `accept`, on hold expiry, or when a follow-up adopts and later settles.
  - A follow-up that arrives after the hold rebuilds lazily from P0, at the cost of one P1 prefill.
- Follow-up wire session ids use the follow-up's own sid. Never reuse the parent's (rule 1:
  one engine session per agent).

### 5.9 Durable resume after an engine restart

**Our consultants engine restarts** (`suspend_running_councils` / `resume_inflight_councils`,
`app.py:1392-1430`, `:1477-1495`; runner path `_drive_council_stream(stream(None))`):

- Persist `KvRunManager.snapshot()` to `<cwd>/.claude-hooks/consultants/<sid>/polykv.json`, keyed
  under the run. Also store a short form in the in-flight record (`consultants/server/inflight.py`).
  Contents: roots, `boot_id`, generation, owner sid + grant, pool refs, `base_seed`, open wire
  sessions.
  - Don't put it in `CouncilState`. It is runtime, not reasoning, and Sends would have to carry it.
    The manager is a dependency, like the recorder.
- On suspend, abort in-flight streams (the node gate raises `RunSuspended`) but **do not close the
  owner**. A quick restart then finds it live; the engine TTL is the crash net.
- On resume (`runner_input["resume"]`):
  1. `restore()` from disk;
  2. per root, compare `GET /health.boot_id`;
  3. check `/kv` for the owner row and `/polykv/pools` for each pool with the recorded `prefix_len`
     and parent (Cerebriline `verifyPolykvRoot`, `CPI:714-726`).
  - Anything missing is rebuilt lazily on the next call. If `boot_id` differs, bump the generation and
    drop everything without releasing.
  - The checkpoint re-runs the interrupted node: its worker session gets a fresh suffix `~r<i>.<gen>`,
    so a half-finished slot is not mistaken for a continuation.
  - Close stale wire sessions whose ids appear in `/kv` but not in the new plan.

**The opencoti engine restarts mid-run**:

- A transport fault or a missing `X-Context-Window` makes the root *suspect*.
- A `boot_id` change (header or `/health`) invalidates the generation. Pool ids from an ended
  generation are never sent again.
- `pool_unknown: true` with the same `boot_id` rebuilds only that chain.
- A turn naming a stale pool is never refused, just fully reprocessed. So correctness holds, and the
  manager only has to stop the reprocess repeating.
- Infra faults never fail a role: wait on `/health` with backoff to 30 s, with no deadline, inside the
  existing stall monitor and hard deadline (Cerebriline #104, `CPI:610-664`).

### 5.10 Release on settle

Call `mgr.release_all()` in `_run_with_state`'s `finally`, before `_settle_inflight` (`app.py:1575-1593`,
`:1353-1389`), unless the §5.8 hold applies. Order:

1. close every open worker session;
2. unpin + release pools leaves-first;
3. close the owner, which releases its owned pools anyway, so step 2 is belt-and-braces for unowned
   intermediates;
4. never release a `shared: true` P0; unpin it if this process pinned it.

Check every close's `found`. List failures as `LEAKED=` in one log line, matching Cerebriline's
`dispose()` (`CPI:2181-2183`).

Then do a final audit read of `/kv` (`?live=1`, once) and `/polykv/pools`: allocations or pools still
naming `<sid>~` are WARN and the release is retried once. Write the record into the recorder so
`transcript.db` shows the shared/prefilled token totals per role.

When a run is **suspended** (not finished), skip release (§5.9). The in-flight record keeps the snapshot.

### 5.11 Fallback

- **Probe failure**: mode `off` for that root, with byte-identical payloads. A unit test asserts
  payload equality with the manager detached.
- **Mid-run loss of capability** (e.g. a restart onto an older build that lacks a flag): drop to the
  highest mode the new `/props` supports at the next call. Never error a role.
- **Pool layer refusals**: share less, never fail (`polykv_integration_guide.md:214-215`).
- **xOllama council models**: never touched. A plain model with `client_pools < 4` gets `affinity`
  only, with the reason logged once.
- Models routed to **cloud** in the same council are unaffected. A council can mix a pooled local
  researcher lane with cloud lanes.

### 5.12 `num_ctx` modes, sized

- **`pooled` (optional)**: never send `num_ctx`. Every call is a per-request window. Since 0533 a
  bounded call books `peak + 1`, so always send an explicit `max_tokens`/`num_predict`. We already
  plan one in `_single_shot` / `_loop_payload_budgeter`.
  - Unowned pools are charged once server-wide. The attachers' per-request windows count only private
    tokens under `polykv_private_window_v1`.
  - Pros: no owner TTL and no keepalive. Cons: no guarantee, so a busy server 429s mid-run (wait it out).
- **`assured`**: one owner per run per root.
  - Owner window = `P1 + P2r + P3 + Σ_concurrent(lane private peak) + critic/synth peak` (P0 is
    unowned, so not counted).
    - Lane private peak = the role's planned context minus its shared head, from `budget.py`'s
      per-role plan.
    - Cap at `/kv.session_ctx_max`. The engine cannot book one owner above that (mail 711, `CPI:2052-2062`).
    - Above the cap, open a second owner and split lanes, as Cerebriline does.
  - `num_ctx_min` = P1 + one lane peak (the smallest useful council).
  - Record `X-Context-Window`, and re-send it as `num_ctx = num_ctx_min` on any reopen (the resume rule).
  - Workers send no `num_ctx`.
  - Grow on a worker 429 (deferred/live resize). Shrink is unnecessary for a run that ends in minutes,
    but at `max` effort (90 min deadline) apply the `kv-pressure.ts` shrink after compaction.
- **xOllama**: the same fields, inside `placement{}` on `/api/chat`, and never `options.num_ctx`.

### 5.13 Touch points (proposed, not implemented)

| file | change |
|---|---|
| `consultants/engine/polykv.py` (new) | `KvRunManager`, the stdlib client (ported from `opencoti_langgraph/client.py` + extensions), layer builder, snapshot/restore |
| `claude_hooks/get_advice/chat_client.py` | `ChatClient._to_ollama` (`:851-876`): forward top-level `session_id` and `placement` when present. `LlamafileAgentChatClient._to_openai` (`:1130-1161`): forward `session_id`, `pool_id`, `continue_pool`, `seed`, `stream_options`, `return_progress`, `chat_template_kwargs` (today it drops everything but temperature/max_tokens/tools). Port `_StallWatch` and the keepalive-expiry fix. |
| `consultants/engine/council.py` | Layered message builders behind a `pooled_layout` flag. Today: planner `:697-701`, researcher `:703-751`, critic `:754-773`, synthesizer `:776-813`, lane plan item `:2128-2143`. `compact_messages` head pinning. |
| `consultants/engine/graph.py` | Capacity read + semaphore in the fan-out router (`:975-1013`). Lane seed/jitter derived from `lane_idx` (already carried in each Send, `:1000-1004`). |
| `consultants/server/runner.py` | Build the manager beside `grounding_msgs` (`:190-192`, follow-up `:840-853`); pass it in `GraphDeps`; follow-up adoption (`:748-760`). |
| `consultants/server/app.py` | Release in `_run_with_state` finally (`:1575-1593`); snapshot on suspend (`:1392-1430`); restore on resume (`:1477-1495`). |
| config + skill + CLI | `[polykv] enabled, mode (off\|affinity\|pooled\|assured\|auto), seed_offset, temperature_jitter_pct, followup_hold_s, same_tools_all_roles`, in both `/consultants config` and `claude-consultants config` |
| tests | Off-path byte identity. Feature-gated payloads. Pool-0 handling. Layer cut / prefix check. Leak audit. Resume with the same and a changed `boot_id`. `pool_unknown` → chain rebuild. Worker-429 grow/wait. Seed offsets. A live gate on a local opencoti (Cerebriline §3.1 probe as template, `polykv_integration_guide.md:280-306`). Add a cohort-2 parity test per M12. |

### 5.14 Open questions for the user

1. Is a prompt-shape change on PolyKV backends acceptable (role instructions move from system to a
   user turn)? It changes what the model sees, so it needs a quality A/B like the 2026-05-18
   tool_executor one.
2. Option (a) vs (b) for toolsets (§5.4.6).
3. Where does opencoti run for the council? A `llamafile://` label through our daemon's
   `ChatModelManager`, which would need its launch args (`--kv-unified --parallel N --max-parallel M
   --polykv-max-pools ≥5`), or an existing opencoti/xOllama server (8240/8244/xOllama 22434)?
4. The default for the follow-up hold: the engine TTL, or until `accept`?

---

## 6. Looked for and not found

- A PolyKV `queue` admission mode: never shipped; it is a 400 (`polykv_api.md:324-327`).
- `evict`/`flush` pool actions: none (`polykv_api.md:211`).
- A global `/polykv/capacity`: none (`polykv_api.md:149`).
- Per-pool `sessions[]`, `own_kv_bytes`, `residency`: never shipped (`polykv_api.md:276-278`).
- Seed or temperature diversification in opencoti or `opencoti_langgraph`: not found. Benches use
  `--temperature 0.0` with a fixed engine seed.
- `session_id`/`placement` on xOllama's OpenAI-compatible routes: not found (`openai/*.go`); native
  `/api/chat` only.
- The vendored `server-context.cpp`: not located during this survey. Engine facts come from the docs
  and patch headers.
- Any existing PolyKV, opencoti or `pool_id` code on our side: none (grep of `consultants/`,
  `claude_hooks/get_advice/`, `claude_hooks/model_sampling.py`).
- Cerebriline use of `/polykv/sampling`, `X-Sessions-Remaining`, `keep_kv`, `?live=1`,
  `return_progress`, `/metrics` (PolyKV gauges): 0 hits in `sdk/packages/{llms,core,shared}/src`.
