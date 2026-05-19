# M14 first real consultants ask — 2026-05-18

First live `claude-consultants consult` after the M14 default-on
flip + the three deployment commits (`48f1c4e`, `40eef61`,
`36bde82`). Goal: validate the end-to-end chain on the user's
actual pgvector under realistic x-tier multi-lane fanout.

## Run

- **SID**: `csl-2026-05-18-0937-4f4f`
- **Effort**: `xhigh` (user's config default)
- **Topology**: `council`
- **Duration**: 737.13 s (~12.3 min)
- **Status**: `completed` — but the synthesized answer is the
  refusal *"I cannot answer this question because the research
  phase failed to produce any codebase evidence or logic traces
  from `consultants/engine/store_reaper.py`."*
- **Models**:
  - planner / researcher / critic: `gemini-3-flash-preview:cloud`
  - tool_executor / synthesizer: `gemma4:31b-cloud`
- **Tokens**: 495782 prompt + 12896 completion = 508678 total

## Question

> In the M14 store reaper at `consultants/engine/store_reaper.py`,
> the critical invariant is that research originals only get
> deleted after a successful distillation write to the project
> namespace. Walk me through the exact code path that enforces
> this, and identify any edge case where it could fail.

The same shape of question that M13's 2026-05-17 smoke answered in
218 s with a 756-char structured answer with file:line citations.

## Result by layer

| Layer | Status |
|---|---|
| M14 store mechanics | ✅ Works |
| pgvector transaction safety | ⚠️ 5 abort warnings, all caught by rollback |
| Researcher → REPORT mode transition | ❌ Stuck in PLAN mode all 6+ rounds |
| Final synthesizer answer | ❌ "Cannot answer" refusal |

## What worked (M14 store layer)

- **Table auto-created** with the full v2 schema:
  `id / content / content_hash / metadata / embedding /
  created_at / expires_at` (TIMESTAMPTZ).
- **10 rows written** to `consultants_store`, all in namespace
  `["csl-2026-05-18-0937-4f4f", "research"]`. Keys follow the M8
  lane-indexed convention (L2, L4, L5, L8 — multiple lanes wrote).
- **TTL math correct**: every row has `expires_at = created_at +
  ~30 days` (clusters around 2026-06-17 07:45:09). The ±20-second
  intra-batch spread is from embedding latency between `_do_put`'s
  client-side `now` snapshot and the DB's `created_at = NOW()` on
  INSERT — a measurement artifact, not a correctness bug.
- **Reaper alive** on pgvector backend with 1-h sweep cadence
  (verified via journal: `store reaper: started (backend=pgvector,
  ttl=True, distill=True, interval=3600s)`).

## What failed (the real bug)

**Researchers stayed in PLAN mode** for every single round. Each
researcher turn emitted a `{"tool_plan": [...]}` JSON block
instead of a REPORT-shape findings string. After 6+ such rounds
the synthesizer correctly concluded "no findings available" and
refused the answer.

The diagnostic: the content of the rows in `consultants_store`
proves the researchers *did* see tool_executor output —

> *"The prior tool results gave a good overview but I need to
> verify the actual line numbers and code paths myself, since line
> numbers may be stale. Let me read the source files directly."*
> — L2-56807ca7491b

So the M13 Send-channel fix is delivering `tool_results` to the
fanned-back researcher lanes correctly. The model is *responding*
to those results by emitting another tool_plan instead of
transitioning to REPORT. This is the textbook "re-planning loop"
failure mode.

### Why this is M14-specific (not an engine regression)

The engine code in `consultants/engine/{graph,council,researcher}.py`
is **byte-identical** to the M13 baseline at commit `71edcdc`
(`git diff --stat 71edcdc..HEAD` on those files: empty). So the
graph dispatch, Send semantics, and round-filter logic are all
the same as the version that produced a 756-char structured
answer on 2026-05-17.

The single thing that changed for the researcher's prompt input
between then and now: **with M14 default-on, `recall_research`
now actually fires** (was a no-op when `store.enabled=False`).
The researcher prompt now carries a "## Recalled peer findings"
block. When recall returns empty — which it did 5 times this
session because of the aborted-transaction warnings — the block
text apparently nudges the model into "I need to plan more"
instead of "I should report what I have."

### The pgvector abort cascade

5 `pgvector hybrid vector query on consultants_store failed:
current transaction is aborted, commands ignored until end of
transaction block` warnings during the session:

```
09:37:20  (early in research, first recall against empty table)
09:44:45 / 09:44:48 / 09:44:49 / 09:44:51  (4 in 6 seconds during
                                            researcher fanout)
```

The rollback fix from commit `40eef61` *catches* each abort
(recall returns empty, the loop continues), but it does NOT
prevent the abort from happening — the abort is upstream.

Most likely root cause: a single shared `PgvectorProvider`
instance with one TCP connection serves all concurrent
researcher lanes. xhigh fanout dispatches ~3 lanes in parallel
via LangGraph Send. With no thread lock around `_conn` access,
two lanes' SQL statements can interleave on the same connection
— PostgreSQL is single-threaded per connection, so concurrent
statements race the connection's transaction state.

The 8 `created pgvector table: consultants_store (dim=1024)`
log lines suggest multiple PgvectorProvider instances were
created, but they likely each got their own conn from a
psycopg connection pool — so the race may be inside the pool's
reuse logic, not the engine.

## Action items

1. **Stop the prompt-shape regression first** —
   `consultants/engine/store.py:recall_research` returns `[]`
   when the store fails OR when there are genuinely no peer
   findings. The researcher prompt builder should NOT emit an
   empty "Recalled peer findings" block in those cases — instead
   omit the section entirely so the model's prompt looks
   identical to the M13-baseline prompt. That alone should
   restore the M13 behaviour.

2. **Fix the pgvector concurrency** — wrap `_conn` access in
   `PgvectorProvider` with a `threading.Lock` so concurrent
   recall / store calls from different fanout lanes serialize
   instead of racing. Long-term: switch to a per-call
   `psycopg.connect()` or a real `psycopg_pool.ConnectionPool`
   so the engine doesn't serialize all DB work on one connection.

3. **Clean up the 10 junk rows** in `consultants_store` for
   this session. The reaper would otherwise fire a
   distillation LLM call on them in 30 days (3+ rows from one
   sid triggers the cost gate) and write a useless distilled
   summary into the project namespace. One-line DELETE:
   `DELETE FROM consultants_store WHERE (metadata->'namespace')::jsonb ?
   'csl-2026-05-18-0937-4f4f'`.

4. **Re-run the test** with the prompt-shape fix and the
   concurrency lock, expect to see researchers transition to
   REPORT mode cleanly within 2-3 rounds.

## Diagnostic artifacts captured

- Session ID: `csl-2026-05-18-0937-4f4f` (queryable via
  `claude-consultants result csl-2026-05-18-0937-4f4f`).
- Console log: `/root/.claude/claude-hooks-consultants.log`
  (filter on the SID for this run's events).
- All 10 rows still in `consultants_store` until cleanup.
- 6+ researcher turn records captured in the session's
  `transcript.db` sidecar.

The failure is informative: M14 store mechanics work
correctly under load, but the researcher prompt's reaction to
recall-returning-empty needs the same care the M13 fix gave to
tool_results delivery. Same pattern of bug — "missing data
breaks the model's mode transition" — different channel.
