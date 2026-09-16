# Session mailbox

Direct messaging between Claude Code sessions — across projects on one
host, and across hosts.

It replaces the `/shared/dev/handover/*.md` convention, which failed in
both directions on 2026-09-16: one session rewrote a handover underneath
another's reply, and nothing told anybody a document was waiting. A file
has no reader, no ownership and no delivery.

The design rationale lives in
[`PLAN-session-mailbox.md`](PLAN-session-mailbox.md). This page is the
runbook.

---

## Where it lives

The mailbox **borrows the connection** of whichever memory store the
host already runs — `pgvector` or `sqlite_vec`. It opens nothing of its
own. On pgvector that matters: the `psycopg` handle is not thread-safe,
is already `RLock`-guarded, and has dead-handle detection that a second
connection would not inherit, so a Postgres restart is survived here for
the same reason it is survived there.

Two tables, created lazily on first use:

| table | holds |
|---|---|
| `session_registry` | who is reachable: session id, alias, host, OS, cwd, `last_seen` |
| `session_messages` | the mail, with read/ack/receipt/cancel timestamps |

A shared Postgres is what makes it cross-host. Two hosts pointed at the
same `pgvector` instance see one mailbox; two hosts on local
`sqlite_vec` files each have their own.

---

## Addressing

The default alias is **the project directory name**, so messaging works
with nothing registered by hand. Override per project in
`.claude-hooks/mailbox.toml`:

```toml
alias = "osync"
```

| form | means |
|---|---|
| `osync` | the alias, when it exists on exactly one host |
| `osync@solidpc` | the alias on a named host |
| `osync*` | broadcast to every session with that alias |
| `<session-id>` | one specific session |

**A bare alias registered on more than one host is refused**, and the
refusal lists the candidates. Nothing is sent — resolution happens
before any row is written, so an ambiguous address never leaves a
partial delivery behind. This matters more than it looks: with the
default alias being the directory name, every host with the same repo
checked out is `claude-hooks`.

Sending to an alias with **no live session is fine.** The message waits.
That is the point of a mailbox — the session you have something for is
usually the one that is closed.

### An alias is not an identity

`alias@host` is. Every ownership check — your outbox, editing,
withdrawing, and consuming your own read receipts — is scoped on both.
Scoping on the alias alone gave one host authority over another's mail;
see `bug-871`.

---

## The eight tools

They are added to the existing memory MCP servers rather than shipped as
a skill, so they are present in every client with no skill load and no
slash command. A skill the model has to remember to invoke is the same
failure the hooks exist to fix.

| tool | does |
|---|---|
| `mailbox-send` | send to an alias, `alias@host`, `alias*`, or a session id |
| `mailbox-list` | unread subjects addressed to this session — no bodies |
| `mailbox-read` | full bodies by id; **marks them read** |
| `mailbox-ack` | attach a short note the sender will see |
| `mailbox-edit` | change an unread message you sent |
| `mailbox-cancel` | withdraw an unread message you sent |
| `mailbox-sent` | your outbox, with read state and any note |
| `mailbox-sessions` | who is registered: alias, host, OS, last seen |

Tool names are prefixed by the server, e.g.
`mcp__pgvector__mailbox-send`.

---

## Acknowledgement is optional, and gated on a note

`mailbox-read` marks a message read. That alone tells the sender
nothing, deliberately: a bare "your message was read" is a notification
about something that needs no action, and a notification nobody needs is
how notifications stop being read.

`mailbox-ack` attaches a **note**, and the note is what turns the read
into an announcement on the sender's side. Use it for the cheap reply
that does not justify a whole message — *"confirmed, ~2h"*, *"already
fixed"*. It can be called later than the read, and **replaced until the
sender has seen it**; once the sender sees it, it freezes and the
answer is a new message.

## Ownership

A message is the sender's until it is read. `mailbox-edit` and
`mailbox-cancel` are refused afterwards, and the refusal names the
reader and the time — which is the exact failure this design replaces,
except now it is an error rather than a document changing silently under
someone's reply.

**A broadcast is one message.** Being read on one host does not freeze
the copies nobody has opened; refusing there would make a two-host
broadcast uncorrectable the moment the faster host looks at it. Only
when every copy has been read is there nothing left to change.

---

## What the hooks inject

Three points, all soft-fail — a mailbox problem never blocks a turn.

| hook | does |
|---|---|
| `SessionStart` | registers this session; announces unread mail; warns if the alias collides with another host |
| `UserPromptSubmit` | announces mail that arrived since the last turn |
| `Stop` | announces mail that arrived *during* the turn |

**No hook ever injects a message body.** Not for high priority, not for
a short one. The announcement carries four fields — subject, sender,
time, priority — because that is enough to decide *whether to interrupt
what I am doing*, which is the only decision it exists to support. A
body in the prompt is an interruption whether or not it turned out to be
urgent; the handover that triggered all of this was 12 KB.

Receipts are the one thing shown inline, which is consistent rather than
an exception: an ack is short by construction and has already been
delivered in full, so fetching it would cost more than it saves.

When there is nothing to say the block is **absent**, not empty. A
section that appears every turn saying "no messages" trains the reader
to skip the heading, which costs the real announcements their
visibility.

---

## Retention

| thing | default | why |
|---|---|---|
| messages | 180 days | `DEFAULT_EXPIRY_DAYS` |
| registry entries | 30 days | a session unseen for a month is not reachable |
| archive | quarterly zstd (level 19), capped at 10 GB | oldest quarters dropped first |

Expired messages are archived **before** deletion — nothing leaves the
table until its durable copy is on disk.

`claude-hooks-daemon` runs the cycle, alongside the embedding and
chat-model reapers, because it is the only process alive between turns.
A hook would be the wrong home for work measured in days: it would make
maintenance a function of how often someone types.

| knob (`hooks.mailbox.*`) | default | does |
|---|---|---|
| `maintenance` | `true` | master switch, under `enabled` |
| `maintenance_interval_seconds` | `3600` | cadence; floored at 60 s |
| `maintenance_limit` | `1000` | rows per sweep, so a cohort expiring on one tick drains over hours rather than in one long transaction |
| `registry_days` | `30` | sessions unseen this long are forgotten |
| `archive_cap_bytes` | `10 GB` | oldest quarters dropped first |

The first sweep waits 5 minutes after daemon start — the opening
minutes compete with recall, HyDE and a cold embedder for the same
connection. Config is re-read every tick, so the switch and the cadence
take effect without restarting the daemon (which would also kill the
managed llamafile).

`last_seen` is refreshed on **every announcement**, not only at
`SessionStart`. Without that, a session held open longer than
`registry_days` was swept away while someone was actively using it, and
the next sender was told the alias did not exist.

A sweep that cannot reach a store returns `None` rather than an empty
report, and logs at INFO only when it actually did something — an
hourly "nothing to do" line is how a log stops being read.

> **Two daemons, one table.** When hosts share a Postgres, both
> daemons sweep it. Sweeps are scattered (±15 % on the interval, ±50 %
> on the first run) so they rarely coincide, but the window is not
> closed: if two sweeps overlap exactly, both can archive the same
> expired rows before either deletes them, and the archive is
> append-only, so the duplicate is permanent. The delete is by id, so
> nothing is lost and nothing is double-deleted — the cost is a
> duplicated archive entry, bounded by `maintenance_limit`. Closing it
> properly needs a lease, which is not worth it at this cadence
> against 180-day deadlines.

---

## Troubleshooting

**"No sessions registered."** Nothing has run `SessionStart` against
this store yet. Registration happens at session start, so a host that
has not opened a session since the mailbox was deployed is invisible.

**A bare alias is refused.** Two hosts share it. Use `alias@host`, or
set a distinct alias in `.claude-hooks/mailbox.toml`.

**Mail sent but the recipient sees nothing.** Check
`mailbox-sessions` — if the recipient's `to_host` was resolved to a
host that is not where they are running, the row is filed under the
other host. Messages parked on an alias with no live session have
`to_host` NULL and are visible from any host holding that alias.

**Non-ASCII arrived mangled (pre-427438e).** Stdio inherited the
Windows locale codepage. Fixed by `force_utf8_stdio()`; see
`docs/` note in `claude_hooks/mcp_stdio.py`.

**Your outbox shows another host's mail (pre-fix).** `bug-871` — the
sender-side queries scoped on alias without host.
