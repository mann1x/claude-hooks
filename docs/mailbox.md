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

A session's alias is, in order:

1. **its name** — what `/rename` set in Claude Code;
2. otherwise the alias the session id already holds;
3. otherwise `.claude-hooks/mailbox.toml` for the project:

   ```toml
   alias = "osync"
   ```

4. otherwise the name of the project directory.

So messaging works with nothing set up, and `/rename opencoti-mac` is all
it takes to give a session its own address.

**The name is checked on every mailbox operation** — each hook turn, each
`mailbox-*` tool call and the status-line badge — so a `/rename` takes
effect at the next one, without a restart. Claude Code sends no hook
event for `/rename`; it records the name in the transcript as a
`custom-title` record, which is read from the transcript's tail. When
there is none — the transcript does not exist until the first message,
and on 2.1.278–2.1.280 xollama's three `/rename` were never persisted
([anthropics/claude-code#99200](https://github.com/anthropics/claude-code/issues/99200))
— the last `/rename <name>` for the session in `~/.claude/history.jsonl`
is used. The `agent-name` record is ignored: without a rename it carries
the *generated* title.

A rename moves the session's unread mail with it, but only mail that
arrived while the session held the old alias; older mail there was
someone else's. The session is told in one line:
`Mailbox: renamed opencoti@solidpc → opencoti-mac@solidpc; 1 unread
message moved with it.`

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

### A session's alias belongs to its session id

Without a name, the alias is decided once, when a session id first
registers: `.claude-hooks/mailbox.toml`, otherwise the name of the
**project root** (`CLAUDE_PROJECT_DIR`, the directory the session was
started in — `run.py` copies it into the event), and the event's `cwd`
only if even that is missing. After that every lookup goes through the
session id's registration, never a directory.

The default used to come from `cwd`, which is wherever the session last
cd'd to, and a session id registers afresh after `/clear`. So xollama
registered as `v0.34.4-xollama.1` and opencoti as `llamafile`, and their
badges and nudges counted an inbox nobody writes to.

The MCP tools take the session id from `CLAUDE_CODE_SESSION_ID`, which
Claude Code exports to MCP children. After `/clear` that variable still
names the old session, because the MCP child outlives it; when that id
has no registration, the tools adopt the newest registration of the same
Claude Code process, which is the current session.

### Two sessions in one directory

A name is **never taken from a session that is still running.** The
second live session started in `opencoti` registers as `opencoti-2`, the
third as `opencoti-3`, and the first keeps `opencoti`, so mail sent to
`opencoti` keeps reaching the session that was there first. The new one
is told at `SessionStart`: `Mailbox: you are opencoti-2@solidpc —
opencoti belongs to another live session on solidpc.` The same holds for
names: two sessions both renamed `opencoti-mac` get `opencoti-mac` and
`opencoti-mac-2`.

Whether the holder is running is decided by its **Claude Code process**,
recorded in the registry as pid + start time (a pid alone is reused; the
start time proves it is the same process). `run.py` finds it by walking
up from the hook to the nearest `claude` / `claude.exe` (or the node
process running `@anthropic-ai/claude-code`), via `/proc` on Linux and a
Toolhelp snapshot on Windows. The holder's alias is taken over only when:

- its process has ended — a crash, or a session closed without
  `SessionEnd`;
- it is this same process — `/clear` starts a new session id in the same
  Claude Code, which inherits its alias;
- it is an older row with no process recorded and was last seen more than
  two hours ago.

Before this, registering simply replaced the other row for `alias@host`:
a second session in the same directory evicted the first one, showed its
mail in its own badge, and the two took the row in turns.

`CLAUDE_HOOKS_CLIENT_PID` overrides the process walk (`0` = unknown).
The cloud relay is unchanged: a cloud session asks for an alias by name
and gets it.

### One registration per `alias@host`

Enforced by a unique index, not only by the code that writes it.
`session_id` is the primary key, so a client that restarted under a new
id used to leave the old row behind — observed live as five
`xollama@solidpc` rows in one directory, four of them an hour stale
behind the one doing the work.

Registering replaces a row for the same `alias@host` whose session has
ended, and
the schema step deduplicates what an upgrade finds, keeping the most
recently seen row. Delivery already collapses to distinct
`(alias, host)` pairs, so duplicates had stopped double-sending; what
they corrupted was everything *readable* — `mailbox-sessions` showing a
crowd where there is one session, the `SessionStart` collision note
warning about peers that are the same session, and every liveness
decision answered from whichever row was found first, usually a dead
one.

Two sessions running at once never share a row: the second gets a
numbered alias (see "Two sessions in one directory").

---

## The eight tools

They are added to the existing memory MCP servers rather than shipped as
a skill, so they are present in every client with no skill load and no
slash command. A skill the model has to remember to invoke is the same
failure the hooks exist to fix.

| tool | does |
|---|---|
| `mailbox-send` | send to an alias, `alias@host`, `alias*`, or a session id |
| `mailbox-list` | subjects addressed to this session, one page at a time — no bodies |
| `mailbox-read` | full bodies by id; **marks them read** |
| `mailbox-ack` | attach a short note the sender will see |
| `mailbox-edit` | change an unread message you sent |
| `mailbox-cancel` | withdraw an unread message you sent |
| `mailbox-sent` | your outbox, paged, with read state and any note |
| `mailbox-sessions` | who is registered: alias, host, OS, last seen |

Tool names are prefixed by the server, e.g.
`mcp__pgvector__mailbox-send`.

### Listings are pages

A session that has used the mailbox for weeks can hold hundreds of
messages, and a listing that returned all of them was a wall of text to
scroll past. `mailbox-list` and `mailbox-sent` return **one page**
(20 by default, `limit` up to 100) with the total it was cut from, and
end with the exact call for the next page:

```
11–20 of 36 unread message(s) for me@solidpc (matching 'misc') — page 2 of 4:
  #412 [unread] 'subject' — from beta@solidpc, 3 hours ago
  …
More: mailbox-list {"query": "misc", "limit": 10, "page": 3} for the next page, or narrow it with query / since / until.
```

| argument | narrows to |
|---|---|
| `page` | 1-based page |
| `limit` | page size, default 20, at most 100 |
| `query` | keywords, **all** of which must appear (case-insensitive) in subject, body or sender (`to` for `mailbox-sent`); `"quotes"` keep a phrase; `%` and `_` are literal |
| `from` / `to` | sender (list) or recipient (sent) alias; `@host` is ignored |
| `since` / `until` | an age (`30m`, `12h`, `3d`, `2w` — that long ago), a date (`2026-09-27`; as `until` it includes the whole day) or `2026-09-27T14:30` (host-local unless it carries `Z` / `+02:00`) |
| `order` | `newest` / `oldest` |
| `include_read` | list only: history as well as unread |

Unread mail is a queue, so the default order is most urgent then oldest
first; a listing with `include_read` is history, newest first. `id`
breaks timestamp ties, so pages never overlap or skip. Filtering and
paging run in SQL (`LOWER() LIKE … ESCAPE`, portable across Postgres and
SQLite), so a large mailbox is never fetched to be trimmed.

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
| `SessionStart` | registers this session and says which alias it has; announces unread mail; warns if the alias collides with another host |
| `UserPromptSubmit` | announces mail that arrived since the last turn |
| `Stop` | announces mail that arrived *during* the turn |

**`Stop` also nudges the model, once.** Its announcement is a
`systemMessage`, which you see and the model does not, so mail that
arrived during a long turn used to wait for the next prompt. When unread
mail is waiting, Stop returns `decision: block` with a reason listing
the messages (id, subject, sender, age — still no body) and telling the
session to read them with `mailbox-read`, act or reply, and finish.
One nudge, not a loop:

- never when the stop is itself the continuation of a block
  (`stop_hook_active`);
- never twice for the same message in the same session — nudged ids are
  kept in `~/.claude/claude-hooks-mailbox/nudged-<session>.json`
  (`CLAUDE_HOOKS_MAILBOX_STATE_DIR` overrides; files unused for 7 days
  are pruned). A session that leaves a message unread is not asked
  again; a new message gets its own nudge.

`hooks.mailbox.stop_nudge: false` turns it off (the visible notice
stays). Both Stop paths nudge: the normal one and a repo whose
`.claude-hooks-disable` keeps `mailbox`.

**No hook ever injects a message body.** Not for high priority, not for
a short one. The announcement carries four fields — subject, sender,
time, priority — because that is enough to decide *whether to interrupt
what I am doing*, which is the only decision it exists to support. A
body in the prompt is an interruption whether or not it turned out to be
urgent; the handover that triggered all of this was 12 KB.

Receipts are the one thing shown inline, which is consistent rather than
an exception: an ack is short by construction and has already been
delivered in full, so fetching it would cost more than it saves.

**Announcements are capped at 10 messages** (`filters.ANNOUNCE_MAX`) —
the `## Messages` block, its receipts and the Stop nudge. Beyond that
they give the count and point at `mailbox-list`; a session holding a
backlog would otherwise have all of it injected into every prompt. The
nudge still claims the whole batch, so it stays one nudge per batch;
receipts beyond the cap are announced on the following turns. The
status-line badge uses a `COUNT(*)` rather than fetching the rows.

When there is nothing to say the block is **absent**, not empty. A
section that appears every turn saying "no messages" trains the reader
to skip the heading, which costs the real announcements their
visibility.

---

## Retention

| thing | default | why |
|---|---|---|
| messages | 180 days | `DEFAULT_EXPIRY_DAYS` |
| identical resend blocked | 10 minutes, or while unread | `DEFAULT_DEDUP_WINDOW_SECONDS` |
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

`last_seen` is refreshed on **every announcement and every tool call**,
not only at `SessionStart`. Without the announcement refresh, a session
held open longer than `registry_days` was swept away while someone was
actively using it, and the next sender was told the alias did not exist.

The per-tool-call refresh closes the other half. Announcements ride the
hook path, which fires once per turn — a good approximation of activity
only if turns are short. A session that spent fifteen minutes inside a
single turn checking, reading and sending mail moved the timestamp
exactly once, at the start, and then read as idle for a quarter of an
hour while it was the busiest thing in the registry. Using the mailbox
is the strongest evidence a session is alive, and it was the one signal
not recorded. It costs one indexed UPDATE, and it soft-fails: looking
stale is a smaller problem than a mailbox that refuses to work.

Tool calls refresh **by alias**, because Claude Code does not export
`CLAUDE_SESSION_ID` to an MCP child — verified on three live stdio
servers, none of which had it. Keying the refresh on the session id
would therefore have been dead code on exactly the path that carries the
mail. `(alias, host)` works instead only because the unique index above
makes it identify one row or none; with five rows to choose from,
refreshing "this alias's registration" was a guess. A tool call never
*creates* a registration — a session that cannot state its id cannot be
cleaned up later — so an unregistered alias stays unregistered until its
`SessionStart`. One imprecision: the alias comes from the server
process's own directory, so the shared `--http` server refreshes the
alias of the directory it was started in rather than the caller's. That
derivation predates this refresh, and it can only ever be wrong about
who is *alive*, never about delivery.

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

## Spawned runs never touch the mailbox

A `claude -p` / SDK run started in a project directory would take the
project's alias. Caliber's pre-commit refresh does exactly that, and
until 2026-09-28 those runs registered as the project (evicting the real
session, then deleting the row at exit), got its announcements and Stop
nudge, and **read its mail** — `read_by` empty, the real session never
told. Now the hooks do not run at all in such a run
(`claude_hooks/session_kind.py`, checked in `run.py`), and in an MCP
child of one `mailbox-read` / `mailbox-ack` refuse and nothing is
registered; `mailbox-send` and the listings still work.

## Cloud sessions

A Claude cloud session cannot reach the pgvector MCP. In the desktop app
it can read and write a linked local folder, and the **relay** turns that
folder into a mailbox client: the session writes requests as files, the
daemon runs them through the same `MailboxTools` dispatch the MCP uses,
and writes the answers back. Code: `claude_hooks/mailbox/relay.py` and
`watch.py`. The session's own guide is
[`claude_hooks/mailbox/cloud/MAILBOX.md`](../claude_hooks/mailbox/cloud/MAILBOX.md),
installed into the folder as `MAILBOX.md`.

**Addressing.** Tell the cloud session which alias to take ("take the
mailbox alias osync"). It sends a `mailbox-alias` request and is then
**`osync@cloud`** to every other session. The registry key behind it is
`cloud-osync`; nobody needs it. A later cloud session that takes the same
alias takes the address over, mail included.

**The semaphore rule.** Nothing a remote session writes is read until
its semaphore says so. Each request is `requests/<id>.json` plus
`requests/<id>.sem` (`{"op", "status", "bytes"?}`), written semaphore
`writing` → payload → semaphore `ready`. The daemon ignores a payload
with no semaphore, an unparseable semaphore, `writing`, and a `ready`
whose optional `bytes` does not match the file on disk (still syncing).
It reads a request only when all of that holds, then deletes payload and
semaphore and runs it. `cancelled` deletes both and does nothing. A
request stuck past `writing_timeout_seconds` (1 h) goes to `rejected/`
with a reason. The daemon's own files — `replies/<id>.md`, `INBOX.md`,
`status.json` — are written the same way, semaphore last.

**Cost.** The thread is blocked in the kernel on file events while idle:
inotify on Linux (which also sees Samba writes, since `smbd` writes the
local file), `ReadDirectoryChangesW` on Windows, and a 30 s mtime poll
only where neither works. It watches only the folders a session writes
to, so its own writes don't wake it. A request is handled after a 1 s
settle (so the three-file write lands in one pass). Only past `burst`
(5) passes that handled requests within one `interval_seconds` (30,
also the floor) does the relay batch what arrives into one pass — so a
session working step by step is answered at once, and a flood is
capped. The inbox refresh and a pass that found only a `writing`
semaphore don't count. The database is opened
on first use and queried — one inbox page per session, `INBOX.md`
rewritten only when it changes — every interval only while a cloud
session has made a request in the last `live_hours` (12). Session
folders idle for `archive_days` (30) move to `archive/`.

**Setup.** None needed: the relay is part of the mailbox. Wherever
`hooks.mailbox.enabled` is true — Linux, Windows or macOS — the relay is
on and its folder is **`~/claude-mailbox`** (`C:\Users\<you>\claude-mailbox`
on Windows). Link that folder in the desktop app. It has to be a local
folder: the app cannot link a network share. To move it or turn it off:

```json
"hooks": {"mailbox": {"enabled": true, "cloud_relay": {
  "root": "/shared/dev/mailbox"
}}}
```

(`"enabled": false` in `cloud_relay` turns the relay off on that host.)
Each host relays its own folder, and every relay registers its sessions
as `<alias>@cloud`, so a cloud session is reachable at the same address
whichever machine's desktop app it runs in.

`install.py` creates the folder and installs `MAILBOX.md` (also in
non-interactive runs); `scripts/deploy.py` keeps both current, and `verify_deploy.py` fails on a stale copy. The
daemon's systemd unit is sandboxed (`ProtectSystem=strict`, write access
to `~/.claude` only), so deploy and install also write
`/etc/systemd/system/claude-hooks-daemon.service.d/mailbox-relay.conf`
granting the folder — as spelled *and* resolved, because the mount
namespace is built from the literal path and `/shared/dev` is a symlink.
Without it the relay reads requests and can never answer; it probes the
folder at start and refuses to run, logging this fix. Config
is read when the daemon starts — restart it (deploy does) to apply a
change. Run the relay on **one** host per folder.

| key | default | |
|---|---|---|
| `root` | `~/claude-mailbox` | the folder; must be local |
| `host` | `cloud` | the host part of every cloud address |
| `interval_seconds` | 30 | batching window + inbox refresh; floor 30 |
| `burst` | 5 | passes per window before batching starts; floor 1 |
| `writing_timeout_seconds` | 3600 | stuck requests → `rejected/` |
| `live_hours` | 12 | inbox kept current this long after a request |
| `archive_days` | 30 | idle session folders → `archive/` |
| `aliases` | any | optional allow-list |
| `watcher` | `auto` | `inotify` / `windows` / `poll` |

```bash
python -m claude_hooks.mailbox.relay status                # folder, instructions, sessions
python -m claude_hooks.mailbox.relay install-instructions  # copy MAILBOX.md by hand
```

**Limits.** A cloud session runs no hooks: it gets no mail notice, no
Stop nudge and no badge, and learns about mail only by reading
`INBOX.md`. Answers take up to about 30 s.

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
