# Ollama connection slots

Ollama Cloud limits **concurrent connections per account**: free 1,
pro 3, max 10. The limit applies to the account, whichever relay the
calls pass through (eleven2go's Ollama, the solidpc proxy, ollama.com).
Over the limit, calls fail or stall; nothing queues them server-side.
Before 2026-09-23 the only guards were the council lane cap and a
convention of at most 2 concurrent bench calls, and neither accounted
for the other.

`claude_hooks/ollama_slots.py` now holds a slot for every HTTP attempt
`ChatClient` makes (`chat` and `chat_streamed`). That covers the
consultants engine, get-advice and every benchmark.

| | cloud | local |
|---|---|---|
| scope | one per host, for the account | one per Ollama server (`local-<host>_<port>`) |
| detected by | `remote_host` in `/api/tags`; `:cloud` / `-cloud` name as fallback | everything else |
| limit | plan connections − `reserve_for_hooks` (pro: 3 − 1 = 2) | `local_limit` (2) |

- **The hooks keep a connection.** The recall hooks (HyDE, reflect) run
  in their own short-lived processes and must not queue behind a
  benchmark, so they bypass the limiter. The limiter never hands out
  the connection reserved for them.
- **Cross-process.** A slot is an OS file lock in
  `~/.claude/ollama-slots/<scope>/slot-N.lock`. A process that dies
  releases its slot.
- **Order.** Waiters in one process are served first come, first served.
  Between processes, the first to poll after a slot frees up gets it.
- **Retries** release the slot during backoff.
- **Stalls.** A streamed call starts the consultants stall clock only when
  it is admitted (`StallController.restart_clock`), so time spent queued
  is never cancelled as a silent model. A call cancelled while queued
  raises `CancelledByOrchestrator`, just as it would mid-stream.
- **Fail-open.** A locking error, or a wait longer than `max_wait_s`
  (1800 s), lets the call through with a warning.

## The plan

The plan is read from `POST ollama.com/api/me`, signed with
`~/.ollama/id_ed25519` (needs `cryptography`), and cached for a day in
`~/.claude/ollama-plan.json`. That response contains personal data;
only `Plan` is read, and only `{plan, at}` is stored. If there is no
key, no library or no network, the plan falls back to `pro`.

## Two hosts, one account

Each host counts only its own calls. When solidpc and pandorum both run
benchmarks or consultations on the same account, split the budget:

```json
"ollama_slots": {"cloud_limit": 1}
```

on one of them. `CLAUDE_HOOKS_OLLAMA_CLOUD_SLOTS=N` sets the limit for a
single process, and `CLAUDE_HOOKS_OLLAMA_SLOTS_DISABLE=1` turns the
limiter off (the test suite does this).
