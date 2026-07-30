# Embedder-served coordination lock — design

Status: **proposal** for `opencoti-llamafile`. Consumer: `claude-hooks`
(`claude_hooks/embedder_gate.py`, currently Layer 2 advisory
backpressure).

## 1. Why the embedder is the right place for this

claude-hooks stores memories from several hosts into one backend. Two
things must be serialised:

1. **Dedup correctness.** `store()` does a similarity recall and *then*
   writes. Two concurrent writers each finish their recall before either
   writes, neither sees the other, and both store. Observed live
   (solidpc, 2026-07-25): five near-identical pairs at cos 0.9986–0.9996.
2. **Embedder capacity.** Every store costs two embeds. A CPU embedder
   has hard limits (~76 tok/s at 5 KB on the reference Ryzen 5600G), so
   uncoordinated stores starve interactive recall, which runs under a
   hook timeout.

A lock in the *memory backend* (e.g. a Postgres advisory lock) would
only serve the `pgvector` provider and leave `sqlite_vec`, `qdrant` and
`memory_kg` unprotected. **Every backend funnels through the embedder**,
so the embedder is the one component that can coordinate all of them.
That is the whole argument for putting the lock here.

## 2. Non-goals

- **Not a general-purpose lock service.** One namespace, short leases,
  no reentrancy, no fairness guarantees beyond FIFO-ish.
- **Not access control.** `/embedding` must keep serving *everyone*,
  lock or no lock. If lock handling has a bug, embedding must still
  work. The lock is a coordination hint between cooperating clients.
- **Not durable.** Locks live in memory and vanish on restart. That is
  correct: a restart means every in-flight client is already broken.

## 3. The core problem: leases without client heartbeats

The obvious lease design needs the holder to refresh periodically. The
claude-hooks client can't easily do that: it makes one blocking HTTP
call to `/embedding` that can run **68 s** (measured, 16 KB payload).

> Aside: a Python daemon thread *could* heartbeat — `urllib` releases the
> GIL during socket I/O — so it isn't strictly impossible. But it adds a
> thread per store and fails in exactly the confusing ways you'd expect.
> The design below makes it unnecessary.

**Key insight: the server already knows the holder is alive, because it
is actively serving that holder's request.**

So define the lease as:

> A lease expires `ttl_ms` after the holder's **last activity**, where
> activity is either a lock operation *or* a request bearing the lock
> token. **A request currently in flight counts as continuous activity** —
> the lease cannot expire while the server is executing a request for
> that token.

This gives:

- No client heartbeat. Ever.
- A **short** TTL (15–30 s) even though a single embed may take 68 s,
  because the in-flight request holds the lease open by itself.
- The stale window is only "time since the holder's last request
  finished", not "time since it acquired".

### What the end of a request does — and does *not* — do

A common misreading: "the request finished, so the lock is released."
It is not. Finishing a request **stops in-flight renewal and starts the
TTL clock**; the lease still has `ttl_ms` to live.

That gap is load-bearing. A claude-hooks store holds the lock across the
whole dedup-then-write cycle:

```
acquire → embed(content) → SELECT top-k + compare + INSERT → release
```

If the lease ended with the embed request, another host could take the
lock before the INSERT — exactly the dedup race this exists to close. So
`ttl_ms` must exceed the holder's *post-request* gap, and nothing more.

> Until 2026-07-25 this chain had **two** embeds — `content[:500]` for the
> dedup search, then the full content for the write — and therefore two
> gaps. The vector is now computed once and spent twice
> (`Provider.embed_for_store` / `recall_vec` / `store(vec=)`), which
> removes an embed, widens dedup from 500 characters to the full text,
> and leaves a single gap to size the TTL against.

Consequently:

- **Normal path:** explicit `release` in a `finally` frees the key
  immediately. TTL is never reached.
- **Crash path:** TTL frees it ~`ttl_ms` after the last activity.
- **Hang path:** `max_hold_ms` frees it regardless.

### Detecting a dead peer mid-embed

Worth building for deliberately. During a long CPU-bound embed the
server is computing, not touching the socket, so it usually cannot
notice a dead client until it tries to **write the response**. If a
client dies 1 s into a 68 s embed, the in-flight flag persists for the
remaining 67 s and only then does the 20 s TTL start — the key is held
~87 s, not ~20 s.

Bounded and safe (and `max_hold_ms` still backstops it), but slower than
the TTL alone suggests. Two acceptable resolutions:

1. **Accept it.** Worst case is `embed_duration + ttl_ms`.
2. **Poll peer liveness between micro-batches** and clear the in-flight
   flag as soon as the connection is gone, bringing mid-embed death back
   down to ~`ttl_ms`. llama.cpp already has the connection-closed check
   shape for this.

### Measured: what the gap actually is

Do not size `ttl_ms` from intuition — it is directly measurable, and the
first guess in this document was wrong by three orders of magnitude.

Method (solidpc, 2026-07-25): a second llamafile on port 38093, CPU-only
and loopback-bound, so the production embedder on 38092 is neither
disturbed nor conflated; three saturating embed workers; box at load ~8
of 12 threads; 200 samples per cell; `nice` levels **interleaved** across
three rounds to control for drift.

| gap | idle p99 | saturated p99 | worst observed |
|---|---|---|---|
| HNSW SELECT + difflib compare + json | 8.6 ms | 9.1 ms | 13.4 ms |
| INSERT + commit | 1.5 ms | 1.7 ms | 14.4 ms |

For scale, on the same isolated instance: 500 chars = **742 ms**,
5000 chars = **12.7 s**. A 5 KB store therefore holds the lock ~13 s, of
which the lock-critical gap is ~10 ms — **0.07% of the hold**.

**No measurable priority inversion.** `nice 0` gap p99 averaged 9.08 ms
across rounds, `nice 10` averaged 9.16 ms, against a within-`nice 0`
spread of 8.51–9.90 ms. The gap is dominated by the *Postgres backend
process* doing the HNSW search, which the client's nice value does not
touch; the client-side part (difflib, ~1.7 ms) is too small to matter.
`store_async._deprioritise()` is therefore irrelevant to TTL sizing — do
not change it on this account.

Reproduce with `scripts/bench_store_gaps.py` and
`scripts/bench_embed_latency.py`; both carry the method in their
docstrings, including the prompt-cache and tokenisation traps that make
naive embedder benchmarks wrong by 100×.

## 4. API

All endpoints served **from the HTTP thread pool**, never via the
inference task queue (see §7).

```
POST /lock/acquire
  { "key": "claude-hooks:store", "ttl_ms": 2000, "wait_ms": 0 }
  200 { "token": "<opaque>", "fence": 41, "ttl_ms": 2000 }
  409 { "held": true, "retry_after_ms": 800, "holder_idle_ms": 1200,
        "holder_in_flight": false }

POST /lock/release
  { "token": "<opaque>" }
  200 { "released": true }
  200 { "released": false, "reason": "expired" }     # idempotent, NOT an error

POST /lock/renew                                     # optional, rarely needed
  { "token": "<opaque>", "ttl_ms": 20000 }
  200 { "ttl_ms": 20000 } | 409 { "reason": "expired" }

GET  /lock/status?key=claude-hooks:store
  200 { "held": true, "holder_idle_ms": 1200, "fence": 41,
        "holder_in_flight": false, "retry_after_ms": 800 }
```

### `retry_after_ms` and `holder_in_flight`

`retry_after_ms` is the server's answer to "how long until this could
possibly be free", derived as `max(0, ttl_ms - holder_idle_ms)`. It saves
every waiter from guessing a poll interval, and with a 2 s TTL a blind
poll would otherwise be either wasteful or too slow.

`holder_in_flight` is the necessary companion. While the holder has a
request executing, the lease **cannot** expire, so `holder_idle_ms` is 0
and `retry_after_ms` pins to the full `ttl_ms` — which would have a
waiter poll every 2 s for the entire 150 s of a worst-case embed, ~75
pointless round-trips. The flag lets a waiter separate the two states it
actually cares about:

- `holder_in_flight: true` — holder is provably alive and working. Back
  off hard (say 2–5 s, or straight to the client deadline); the lease
  will not lapse while this is true.
- `holder_in_flight: false` — the lease is now running down.
  `retry_after_ms` is meaningful; sleep exactly that and re-try.

Both fields are cheap: they are read off the same in-memory lock record
that `/lock/acquire` already consults, and served from the HTTP thread
pool, so they stay instant while the inference loop is saturated.

And on the work endpoint:

```
POST /embedding
  Header: X-Lock-Token: <opaque>      # optional
```

Presence of the header does two things and **nothing else**: it marks
the request in-flight against that lease, and it refreshes the lease on
completion. An absent, unknown, or expired token is **ignored** — the
embedding still runs. This is what keeps a lock bug from taking down
embedding.

### Capability discovery

Advertise in `/props`, which is already instant:

```json
{ "features": ["lock_v1", "slots_nonblocking_v1"] }
```

Clients check once and fall back cleanly. Avoids probing `/lock/status`
just to learn whether locks exist.

## 5. Deadlock and stale-lock analysis

This is the part you flagged, so here is every failure mode and what
covers it.

| # | Scenario | Covered by | Outcome |
|---|----------|-----------|---------|
| 1 | Holder crashes between acquire and first embed | TTL (no activity) | Released after `ttl_ms` (~20 s) |
| 2 | Holder crashes **mid-embed** | Socket close detection → request no longer in flight → TTL starts | Released ~`ttl_ms` after disconnect |
| 3 | Holder hangs, process alive, socket open | **`max_hold_ms` absolute cap** | Force-released, fence bumped |
| 4 | Network partition | Server-side socket timeout → as #2 | Released |
| 5 | Holder never calls release (bug) | TTL | Released |
| 6 | Server restart | In-memory state cleared | All locks gone |
| 7 | Holder resumes after its lease expired | **Fence token** | Its stale ops are identifiable |
| 8 | Client waits forever for the lock | **Client-side deadline** (§8) | Proceeds ungated |

Two mandatory backstops:

- **`max_hold_ms`** (suggest 600 000 = 10 min). Even with in-flight
  renewal, no lease may be held longer than this in total. Covers the
  hung-but-connected holder (#3), which in-flight renewal would
  otherwise keep alive forever. This is the single most important
  safety valve — without it, "in-flight counts as activity" is itself a
  deadlock source.
- **Server-side TTL cap** (suggest 60 000). Reject/clamp absurd
  `ttl_ms` from clients so one bad caller can't wedge the key.

### Fence tokens

Monotonic `uint64` per key, incremented on every successful acquire.
Returned by `acquire`, echoed in `status`. Purpose: when a lease is
force-expired and someone else acquires, the old holder is
distinguishable.

For *this* consumer fencing is **nice-to-have, not load-bearing** — the
protected resource is the memory DB, not llamafile state, so a stale
holder proceeding causes at worst a duplicate memory, which the client
already tolerates. Include it because it's ~10 lines and makes incidents
debuggable, but don't gate the design on it.

### Clocks

All durations are **relative milliseconds**, computed server-side from a
monotonic clock. Never accept an absolute timestamp from a client, and
never compare across hosts — pandorum and solidpc do not share a clock.

## 6. Why not a server-side blocking acquire

`wait_ms > 0` is tempting but occupies an HTTP worker for the duration.
With a small pool, a few waiters can starve `/health`. Recommendation:

- Support `wait_ms` but **cap it low** (≤ 2 000 ms).
- Prefer clients poll with jittered backoff.
- Return `retry_after_ms` so clients back off intelligently rather than
  hammering.

## 7. De-queueing `/slots` and `/metrics`

Your other change, and it's independently worth doing. Measured on
solidpc during a 29 s embed:

| endpoint | idle | during embed |
|---|---|---|
| `/health` | 0.00 s | 0.00 s |
| `/props` | 0.00 s | 0.00 s |
| `/slots` | 0.00 s | **5.6 – 11.7 s** |
| `/metrics` | 0.00 s | **8.6 s** |

`/health` and `/props` are answered by HTTP threads; `/slots` and
`/metrics` queue a task into the single-consumer inference loop. Note
this also proves **`--threads-http` is not the problem** — the pool is
healthy at full CPU load. Don't raise it.

Suggested fix: have the inference loop publish slot state into a
snapshot the HTTP threads can read without entering the queue.

- Per-slot atomics, or one `seqlock`-style versioned struct updated at
  slot state transitions (start / finish / free).
- Readers accept mild staleness — it's monitoring, not consensus.
- Keep the field names identical so existing clients don't break.

**Compatibility note for claude-hooks:** the current
`embedder_gate.probe()` already handles both worlds — a *fast* answer is
parsed for `is_processing`, a *stall* is interpreted as busy. So the
moment `/slots` stops blocking, the client silently gets more accurate,
cheaper signal with **no client change required**. Don't feel obliged to
ship both changes together.

## 8. claude-hooks client integration

Rough shape, layered on what already exists:

```python
# 1. Capability check (once per process, from /props — instant)
if "lock_v1" in features:
    token = acquire("claude-hooks:store", ttl_ms=20000, deadline_s=60)
    try:
        if token:                      # else: proceed ungated
            embed(..., headers={"X-Lock-Token": token})
        dedup_and_store(...)
    finally:
        release(token)                 # idempotent, best-effort
else:
    wait_for_capacity(...)             # existing advisory backpressure
```

Rules the client must keep, inherited from the current gate:

1. **Bounded wait, then proceed anyway.** A delayed store is fine; a
   dropped memory is the bug this whole subsystem exists to prevent.
   Never let lock unavailability skip a store.
2. **Release in `finally`**, and treat "already expired" as success.
3. **Background store path only.** Interactive recall must never wait on
   the lock.
4. **Local `store_lock` stays.** It's free, strict, and closes the
   same-host race even when the embedder is old or the lock endpoint is
   down. The embedder lock is the cross-host layer above it.

## 9. Test plan

Server-side, the cases worth automating:

- acquire → release → re-acquire succeeds
- acquire → let TTL lapse with no activity → key is free
- acquire → keep a long request in flight → lease survives past `ttl_ms`
- acquire → kill the client mid-request → key frees within ~`ttl_ms`
- acquire → hold beyond `max_hold_ms` → force-released, fence bumped
- release with an unknown/expired token → 200, never 5xx
- `ttl_ms` above the server cap → clamped, not rejected
- `/embedding` with an unknown token → embeds normally
- **`/slots`, `/health`, `/props`, `/lock/*` all stay fast while a long
  embed runs** — this is the regression that matters most, and it's the
  one that would silently come back

## 10. Suggested defaults

| knob | value | why |
|---|---|---|
| `ttl_ms` | 2 000 | ~140× the measured saturated p99 gap (9.1 ms) and ~140× the worst single observation (14.4 ms). Margin is sized for GC pauses and page-in stalls, not for the DB work |
| server TTL cap | 10 000 | One bad client can't wedge the key |
| `max_hold_ms` | 600 000 | Backstop for hung-but-connected holders (#3). `max_chars` is 30 000 and a full-size embed runs 150 s+, so this must stay generous |
| `wait_ms` cap | 2 000 | Protects the HTTP pool |
| client deadline | 60 000 | Then store ungated |

Why the TTL is aggressive rather than conservative: its cost is paid by
*innocent* waiters on **every** unreleased lock, while its benefit
applies only in the rare crash. The failure modes are asymmetric —

- **too long** → a guaranteed stall of up to `ttl_ms` for every waiter
  each time a holder dies, out of a 65–90 s hook budget. Fails *hard*.
- **too short** → the lease lapses mid-hold and two holders run
  concurrently. That is precisely the pre-lock status quo, whose worst
  outcome is one duplicate memory — which the client tolerates and which
  the same-host `store_lock` still prevents. Fails *soft*.

Err short.
