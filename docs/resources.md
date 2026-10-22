# Resource claims

One place where every session on every host says what machine time it is
using: hosts, GPUs, pods, build lanes, the Ollama Cloud account. A session
claims before it uses, waits in a queue when something is busy, and is
woken the moment it is free. Nobody mails anybody "before" and "after".

Code: `claude_hooks/resources/`. CLI: `claude-hooks-res`. MCP tools:
`resource-*` on both memory servers. Tests: `tests/test_resources.py`,
`tests/test_resources_hooks.py`.

## Names

Resources are `/`-separated names; the first segment is the root, which for
a machine is its host name (as `CLAUDE_HOOKS_HOST` / the mailbox spells it):

| name | meaning |
|---|---|
| `solidpc` | the host as a whole (CPU, disks, RAM) |
| `solidpc/gpu0` | the 3090 on solidpc |
| `bs2/gpu0`, `bs2/gpu1`, `bs2/build` | bs2's GPUs, its build lane |
| `eleven2go`, `eleven2go/gpu0` | the Windows bench box |
| `ollama-cloud` | the Ollama Pro account (capacity 2: three connections, the hooks keep one) |
| `pod/<name>` | a rented pod |

Nothing has to be declared. `claude-hooks-res define NAME --capacity N -d
TEXT` adds a capacity (for shared units) and a description.

## Modes

| mode | grants | blocks |
|---|---|---|
| `shared` (`s`) | use alongside others, up to the capacity in units | `exclusive` / `quiet` on the same resource, `exclusive` above it |
| `exclusive` (`x`, default) | sole use of the resource and everything under it | any other claim on it, under it, or `exclusive` above it |
| `quiet` (`q`) | sole use of the resource's whole host | every other claim on that host |

So: `solidpc/gpu0` exclusive leaves `solidpc/gpu1` and a shared `solidpc`
free; `solidpc/gpu0:quiet` is "the GPU, and nobody else does anything on
this host while I measure". A quiet host also stops background work —
the claudemem reindex skips its turn — and every other session that runs
a command there is told so (below).

Several resources in one command are **one claim**, granted all at once
or not at all (`claude-hooks-res wait solidpc/gpu0 solidpc:quiet`): no
half-held claims, and no two sessions each holding what the other needs.

The queue is first come, first served: a claim is granted only when it
conflicts neither with what is held nor with an earlier claim still
waiting, so a stream of small shared claims cannot starve an exclusive one.
Two claims of the same session conflict too (one job per resource); a
claim blocked only by its own session says so.

## Using it from a session

```bash
claude-hooks-res status                                   # who holds what, who waits
claude-hooks-res acquire bs2/gpu0 -r "op-101 gate" --ttl 3h  # now, or exit 1 naming the holder
claude-hooks-res wait solidpc/gpu0 -r "KLD run"           # book, block until granted
claude-hooks-res run solidpc/gpu0 -r ppl -- ./llama-perplexity …   # hold exactly for the command
claude-hooks-res release solidpc/gpu0                     # or --claim ID, or --all
```

**Waiting** is a background command: run `claude-hooks-res wait …` with
`run_in_background`; it exits with `GRANTED <id>` the moment the claim is
granted, and that exit is the session's notification. It prints `QUEUED`
with the position and the holder first. Interrupted or timed out
(`--timeout 2h`), it cancels its booking unless `--keep`.

The MCP tools do the same without a shell: `resource-acquire` (with
`queue: true` it books and returns the `wait --claim <id>` command),
`resource-release`, `resource-status`, `resource-check`, `resource-define`,
`resource-events`.

For scripts: `claude-hooks-res check SPEC…` (exit 0 when grantable now,
books nothing) and `claude-hooks-res quiet-check [HOST]` (exit 1 while
another session keeps the host quiet).

Exit codes: 0 granted / done, 1 busy / timed out / refused, 2 usage,
3 queued (`acquire --queue`).

## How long a claim lives

A claim follows a process on its own host:

- from a session (Bash, MCP, hooks) it follows the session's **Claude Code
  process**: released at SessionEnd, or the moment that process is found
  gone;
- under `run`, it follows the wrapper: released when the command exits,
  even if the session ended first.

Other hosts cannot see that process, so they trust a **lease**
(`--lease`, default 30 min) that the holder's host renews from its hooks
(once a minute at most), its waiters, `run` wrappers, and the hook
daemon's maintenance thread (every 5 min). A host that goes dark has its
claims dropped when the lease lapses. `--ttl` ends a claim regardless.

Every grant, release, expiry and holder exit is logged
(`claude-hooks-res events`), 30 days.

## What the hooks do

- **SessionStart**: one line on how to claim, plus what is held and queued.
- **UserPromptSubmit / Stop**: renew this host's claims (throttled).
- **SessionEnd**: release the session's claims, except `run` jobs.
- **PreToolUse (Bash)**: a command that reaches a host or GPU another
  session holds exclusively — named by `ssh`/`scp`/`rsync` or
  `CUDA_VISIBLE_DEVICES` — or any command on a host another session keeps
  quiet gets a note naming the holder, the reason, and the `wait` command.
  Once per claim per session; never a denial.

`hooks.resources.enabled: false` in `config/claude-hooks.json` turns the
hooks and the MCP tools off. In a project with `.claude-hooks-disable`
they run only with `keep: resources`.

## Where it is stored

`resource_claims`, `resource_events` and `resource_defs` in the pgvector
memory database when the host has one — shared by every host on it, so
this is cross-host — else in the sqlite_vec store's file, else
`~/.claude/claude-hooks-resources.db` (this host only).
`$CLAUDE_HOOKS_RESOURCES_DB` (a path or a `postgresql://` DSN) overrides.
Every decision runs in one transaction under a Postgres advisory lock (or
SQLite `BEGIN IMMEDIATE`), and a change sends `NOTIFY
claude_hooks_resources`; waiters `LISTEN` on it, so a release wakes them
within one round trip (measured ~30 ms). Without Postgres, waiters poll
every 0.5 s. `scripts/verify_deploy.py` (`check_resources`) reads the
store and says which kind it is.
