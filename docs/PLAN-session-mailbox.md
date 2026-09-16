# PLAN — session mailbox (inter-session direct messaging)

**Status:** design, for review. No code written.
**Date:** 2026-09-16
**Replaces:** the `/shared/dev/handover/*.md` convention.

---

## Why

Handover documents do not scale, and today demonstrated both failure
modes inside two hours:

- the opencoti session wrote `2026-09-16-mcp-lsp-clangd.md`; I replied in
  a `-REPLY.md`; the original was then **rewritten underneath my reply**,
  so the reply now answers a document that no longer exists in that form
  and quotes a retracted claim;
- nothing tells a session a document is waiting. I read it because you
  told me to. A handover nobody is told about is a file.

A mailbox fixes the second problem and makes the first explicit:
messages are addressed, ordered, and owned by their sender until they are
read — a sender can correct or withdraw one, but only while the recipient
has not seen it, so nobody's reply is ever re-footed underneath them.

**Non-goal:** this is not chat. It is asynchronous delivery of a fact or
a request between working sessions, with an audit trail.

---

## Addressing

Two forms, both required:

| form | example | resolves to |
|---|---|---|
| **alias, host-qualified** | `osync@solidpc` | the session(s) with that alias on that host |
| **alias, broadcast** | `osync*` | every session with that alias, on every host |
| **alias, bare** | `osync` | *ambiguous if it spans hosts* — see below |
| **session id** | `77aee209-0dfe…` | one specific session |

### Ambiguity is refused, not guessed

The same alias legitimately exists on more than one host — `osync` runs
on both solidpc and pandorum. A bare alias is therefore under-specified
the moment a second host registers it, and silently picking one (or
silently fanning out) is the kind of plausible-looking wrong answer this
whole subsystem exists to avoid.

`mailbox-send` resolves the recipient **before** accepting the message:

* one match → send, and report which.
* several matches, bare alias → **refuse**, listing the candidates, and
  ask for either an explicit host (`osync@pandorum`) or the broadcast
  form (`osync*`). The error carries the resolved list so the caller can
  retry without another lookup.
* several matches, `alias@host` → send to that host's mailbox.
* several matches, `alias*` → one row per recipient, sharing a
  `broadcast_group` id so the sender can see them as one message.
* duplicate **session ids** across hosts (should be impossible) →
  refuse loudly; that indicates a registry bug, not a routing choice.

`mailbox-sessions` surfaces the same resolution, so the model can look
before it sends.

### OS as metadata, not as an address

Each registry row carries `os` (`linux` / `windows` / `darwin`) and
`host`. It is **not** part of the address — it is what makes "run this
on Windows" answerable: the sender queries `mailbox-sessions` for
`os=windows`, then addresses the host it finds. Keeping it out of the
address avoids inventing a second, parallel routing scheme.

Alias defaults to the **project directory name** (`claude-hooks`,
`opencoti`, `lightseek`), so "message the opencoti session" works with
nothing registered by hand. Overridable per project via
`.claude-hooks/mailbox.toml` → `alias = "..."` for the case where the
directory name is not the useful name.

Sending to an alias with no live session is **not** an error — the
message waits. That is the whole point: the opencoti session may be
closed when I have something for it.

### Session registry

A session registers itself at `SessionStart` and refreshes on each
`UserPromptSubmit`:

```sql
CREATE TABLE IF NOT EXISTS session_registry (
    session_id TEXT PRIMARY KEY,
    alias      TEXT NOT NULL,
    host       TEXT NOT NULL,
    os         TEXT NOT NULL,          -- linux | windows | darwin
    cwd        TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON session_registry (alias);
```

`last_seen` is what makes "is anyone working in opencoti right now?"
answerable, and it drives expiry of dead sessions from the registry
(not of their messages).

**Registration reports collisions.** On registering, a session counts the
other live rows sharing its alias. If there are any, `SessionStart`
appends one line to the status block:

```
Mailbox: alias `osync` is also registered on pandorum (last seen 2m ago).
Address it as osync@solidpc / osync@pandorum, or osync* for both.
```

That is the only place the collision needs stating, because it is the
only place a *sender* can be surprised by it later. A duplicate
`session_id` on two hosts is logged as an error rather than announced —
it cannot happen through normal use and means the registry is wrong.

---

## Storage

**A dedicated table, not the vector store.** Mailbox needs exact
addressing, unread state and ordering; it never needs similarity
search, and embedding a message would cost an embedder round-trip on
the send path for no benefit.

It lives in the **same Postgres** as pgvector, so it is cross-host
between solidpc and pandorum for free, and so it inherits the backup
already covering that database.

```sql
CREATE TABLE IF NOT EXISTS session_messages (
    id           BIGSERIAL PRIMARY KEY,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    edited_at    TIMESTAMPTZ,          -- set by mailbox-edit
    from_alias   TEXT NOT NULL,
    from_session TEXT,
    from_host    TEXT NOT NULL,
    to_alias     TEXT,                 -- exactly one of to_alias / to_session
    to_session   TEXT,
    to_host      TEXT,                 -- NULL = any host with that alias
    broadcast_group UUID,              -- set when one send fanned out
    subject      TEXT NOT NULL,
    body         TEXT NOT NULL,
    priority     SMALLINT NOT NULL DEFAULT 0,
    read_at      TIMESTAMPTZ,          -- when mailbox-read returned the body
    read_by      TEXT,                 -- which session_id read it
    expires_at   TIMESTAMPTZ NOT NULL DEFAULT now() + INTERVAL '180 days',
    CONSTRAINT one_recipient CHECK (
        (to_alias IS NULL) <> (to_session IS NULL))
);
CREATE INDEX ON session_messages (to_alias)   WHERE read_at IS NULL;
CREATE INDEX ON session_messages (to_session) WHERE read_at IS NULL;
CREATE INDEX ON session_messages (expires_at);
```

`sqlite_vec` hosts get the same table in their `.db`; the two are not
synchronised, which is honest — a host without the shared Postgres
simply has a local mailbox.

### No acknowledgement

An earlier draft had a separate `mailbox-ack`. It is dropped: an ack is
work the recipient has to remember to do, for a fact the sender can
already see, and an unacked-but-read message would get re-announced
forever, nagging a session about something it has handled.

**`read_at` is the only state that matters.** A session that reads a
message and has something to say back sends a message back — that reply
*is* the acknowledgement, and it carries the part the sender actually
wants (what happened, what to do next) rather than a bare receipt.

The cost is the narrow window where a session reads a message and dies
before acting. That is acceptable: the message is still in the table,
`mailbox-list --all` shows it, and the sender can see `read_by` and ask.

### Editable until read, then frozen

A sender owns a message while it is unread:

* `mailbox-edit <id>` — change `subject`, `body`, and/or `priority`,
  stamping `edited_at`. Refused once `read_at` is set.
* `mailbox-cancel <id>` — withdraw it entirely. Refused once `read_at`
  is set.

Both check sender identity, so one session cannot rewrite another's
message. Refusal names the reader and the read time so the sender knows
to send a correction instead — which is exactly the failure this whole
plan exists to prevent, except now it is an error message rather than a
document silently changing under a reply.

For a broadcast, edit/cancel act on the whole `broadcast_group`, skipping
rows already read and reporting how many were skipped.

### Expiry and the archive

`expires_at` defaults to **180 days**, long enough that a project parked
for two quarters still gets its message.

Expiry does not delete history. A daily sweep (same reaper slot as the
consultants TTL work) moves expired rows into
`~/.claude/mailbox-archive/YYYY-QN.jsonl.zst` — one JSON object per line,
**zstd at max level**, which is the right shape for this data: messages
are prose, repeat their own headers, and compress to a few percent. The
archive is append-only, so a quarter's file is written once and never
rewritten. Rows are deleted from the table only after the archive write
returns — the same "write the durable copy first" ordering the
distillation reaper already uses, for the same reason.

---

## Hook integration (the part with the concurrency risk)

**The hook never injects a message body.** Not for high priority, not for
a short one, not ever. It runs one cheap existence query and announces:

```markdown
## Messages

**2 unread** for `claude-hooks@solidpc`:

- **[high]** `LSP MCP wedges on multi-byte hover` — from `opencoti@solidpc`, 4 min ago
- `clangd 22 installed, compile DB regenerated` — from `opencoti@solidpc`, 2 h ago

Read them with `mcp__pgvector__mailbox-read` when you reach a natural
pause. Reading marks them read; reply with `mailbox-send` if the sender
needs an answer.
```

The announcement carries exactly four fields per message — **subject,
sender, timestamp, priority** — and nothing else. That is enough to
decide *whether to interrupt what I am doing*, which is the only
decision the announcement exists to support. Reasons:

1. a body can be long — today's handover is 12 KB, and pasting that into
   every turn's context is exactly the token drain we avoid elsewhere;
2. the model decides *when* to read, so a message does not derail the
   turn in progress. A body in the prompt is an interruption whether or
   not it was urgent;
3. the read is then attributable — `read_by` records which session
   pulled it, via a tool call we can see.

Priority raises a message in the announcement and prefixes it `[high]`.
It never changes *what* is announced.

### Announced at three points, including Stop

| hook | why |
|---|---|
| `SessionStart` | messages that arrived while this session was closed |
| `UserPromptSubmit` | messages that arrived between turns |
| **`Stop`** | **messages that arrived *during* a long turn** |

The `Stop` announcement is the one that closes the real gap: a turn that
runs twenty minutes is precisely when another session finishes something
worth telling us, and without it that message waits for the next prompt —
which, if you walk away, is tomorrow. The Stop hook already reads the
transcript and already has a write path, so this costs one more indexed
`SELECT`.

Stop announces only messages whose `created_at` is **after this turn's
start**, so it never repeats what `UserPromptSubmit` already showed.

### Concurrency — the constraint you flagged

This matters, and there is prior art in this repo to respect:

- **`psycopg` connections are not thread-safe.** Every public method
  touching `self._conn` is already `RLock`-guarded in the pgvector
  provider; the mailbox must reuse that provider's connection and lock
  rather than opening its own. Recall is already fanned out in parallel
  by `_parallel.py`, so a second unguarded consumer would race cursor
  state.
- **Every soft-failure path must `rollback()`.** A leaked aborted
  transaction makes the *next* caller fail with "transaction is aborted",
  which surfaces as a recall returning nothing — a memory-loss bug that
  looks like an empty store.
- **The mailbox check must be bounded and non-fatal.** `SessionStart` and
  `UserPromptSubmit` already carry a 65 s cap that the HyDE chain can
  consume, and `Stop` competes with the store path. The mailbox query
  gets a short timeout of its own (~1 s) and fails silent-open: no
  messages announced beats a delayed prompt.
- **Hooks never write.** The announcement is a single indexed `SELECT`;
  `read_at` is stamped by the `mailbox-read` tool call, and the registry
  upsert is the one write, on `SessionStart` only, not per-turn. (Per-turn
  `last_seen` refresh is deferred to the daemon, which already holds a
  long-lived connection, rather than added to the hook path.)

---

## MCP tools (no skill, per your requirement)

Added to the existing system-wide pgvector MCP server, so they are
present in every client with no skill load and no slash command:

| tool | purpose |
|---|---|
| `mailbox-send` | `to` (`alias`, `alias@host`, `alias*`, or session id), `subject`, `body`, optional `priority` |
| `mailbox-list` | unread summaries for this session's alias + id; `--all` includes read |
| `mailbox-read` | full bodies by id; stamps `read_at` / `read_by` |
| `mailbox-edit` | change `subject` / `body` / `priority` of an unread message you sent |
| `mailbox-cancel` | withdraw an unread message you sent |
| `mailbox-sessions` | who is registered: alias, host, os, `last_seen` |

`mailbox-send` resolves `to` as a session id when it looks like a UUID,
otherwise as an alias, and **reports which it chose and which host(s) it
landed on** in its result, so a typo'd alias is visible rather than
silently creating a mailbox nobody reads.

---

## Open questions for review

1. **Does the sender get a read receipt?** The data is there (`read_by`,
   `read_at`) and `mailbox-list --sent` would show it. The question is
   whether to *announce* "opencoti read your message" — which is a
   notification about something that needs no action, i.e. the thing
   `ack` was dropped for.
2. **Broadcast to a bare alias with one live session.** Right now
   `osync` with a single match just sends. Should it still require
   `osync@host` for the sake of a stable habit, or is "unambiguous means
   send" the right call?
3. **Registry expiry.** How long does a dead session stay addressable by
   id? Proposal: registry rows drop at 30 days of no `last_seen`;
   messages already sent to them survive to the 180-day expiry.
4. **Archive retention.** The quarterly `.jsonl.zst` files are tiny, so
   the default is to keep them forever. Worth a cap?

---

## Rollout

1. Table + `MailboxStore` on the existing pgvector connection (lock-
   reusing), plus the sqlite_vec variant; `session_registry` alongside.
2. Six MCP tools, with the same byte-identical output formatting both
   MCP servers already share via `claude_hooks/mcp_format.py`.
3. Session registry write at `SessionStart`, collision line in the
   status block, `last_seen` refresh via the daemon.
4. Announcement block in `SessionStart`, `UserPromptSubmit` and `Stop`,
   behind `hooks.mailbox.enabled` (default **off** until proven).
5. Expiry sweep + zstd archive writer in the existing reaper slot.
6. Tests: addressing resolution including the ambiguity refusal and the
   broadcast group; alias-with-no-session; edit/cancel refused after
   read; Stop announcing only what arrived mid-turn; expiry archiving
   before deleting; and a concurrency test that runs a mailbox check
   against the same connection as a parallel recall.
7. Only then: retire `/shared/dev/handover/`.
