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
messages are immutable, addressed, ordered, and acknowledged.

**Non-goal:** this is not chat. It is asynchronous delivery of a fact or
a request between working sessions, with an audit trail.

---

## Addressing

Two forms, both required:

| form | example | resolves to |
|---|---|---|
| **alias** | `opencoti` | any session working in that project |
| **session id** | `77aee209-0dfe…` | one specific session |

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

```
session_id, alias, host, cwd, started_at, last_seen
```

`last_seen` is what makes "is anyone working in opencoti right now?"
answerable, and it drives expiry of dead sessions from the registry
(not of their messages).

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
    from_alias   TEXT NOT NULL,
    from_session TEXT,
    to_alias     TEXT,            -- exactly one of to_alias / to_session
    to_session   TEXT,
    subject      TEXT NOT NULL,
    body         TEXT NOT NULL,
    priority     SMALLINT NOT NULL DEFAULT 0,
    delivered_at TIMESTAMPTZ,     -- when a hook first announced it
    delivered_to TEXT,            -- which session_id saw it
    acked_at     TIMESTAMPTZ,     -- explicit ack, not mere delivery
    expires_at   TIMESTAMPTZ NOT NULL DEFAULT now() + INTERVAL '30 days',
    CONSTRAINT one_recipient CHECK (
        (to_alias IS NULL) <> (to_session IS NULL))
);
CREATE INDEX ON session_messages (to_alias)   WHERE acked_at IS NULL;
CREATE INDEX ON session_messages (to_session) WHERE acked_at IS NULL;
```

`sqlite_vec` hosts get the same table in their `.db`; the two are not
synchronised, which is honest — a host without the shared Postgres
simply has a local mailbox.

### Delivered ≠ acked

Delivery marks that a session was *told*. Ack marks that a session
*acted*. Deleting on read loses the message if the session dies between
the two, which is exactly when a message matters most. Unacked messages
are re-announced on the next session start; `expires_at` stops that
being forever.

---

## Hook integration (the part with the concurrency risk)

**The hook does not read message bodies.** It runs one cheap existence
query and injects an instruction:

```markdown
## Messages

You have **2 unread messages** addressed to `claude-hooks`
(from `opencoti`, newest 4 minutes ago).

Retrieve them with `mcp__pgvector__mailbox-read`, then acknowledge with
`mcp__pgvector__mailbox-ack` once you have acted on them.
```

Three reasons to announce rather than inject:

1. a body can be long — today's handover is 12 KB, and pasting that into
   every turn's context is exactly the token drain we avoid elsewhere;
2. the model decides *when* to read, so a message does not derail the
   turn in progress;
3. the read is then attributable — `delivered_to` records which session
   pulled it, via a tool call we can see.

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
  consume. The mailbox query gets a short timeout of its own (~1 s) and
  fails silent-open: no messages announced beats a delayed prompt.
- **Read-only where possible.** The existence check is a single indexed
  `SELECT count(*)`; the only write on the hook path is the
  `delivered_at` stamp, which can be deferred to the `mailbox-read` tool
  call instead. Preferred: **hooks never write.**

---

## MCP tools (no skill, per your requirement)

Added to the existing system-wide pgvector MCP server, so they are
present in every client with no skill load and no slash command:

| tool | purpose |
|---|---|
| `mailbox-send` | `to` (alias or session id), `subject`, `body`, optional `priority` |
| `mailbox-list` | unread/unacked summaries for this session's alias + id |
| `mailbox-read` | full bodies by id; stamps `delivered_at` |
| `mailbox-ack` | mark handled; takes ids and an optional note |
| `mailbox-sessions` | who is registered, with `last_seen` — "is opencoti live?" |

`mailbox-send` resolves `to` as a session id when it looks like a UUID,
otherwise as an alias, and **reports which it chose** in its result so a
typo'd alias is visible rather than silently creating a mailbox nobody
reads.

---

## Open questions for review

1. **Should a message be able to target a host?** (`claude-hooks@pandorum`)
   Currently alias spans hosts. Useful for "run this on Windows".
2. **Priority semantics.** Is `priority` just ordering, or should a high
   priority message be injected *in full* at SessionStart rather than
   announced?
3. **Expiry default.** 30 days proposed. Long enough that a paused
   project still gets its message; short enough not to accumulate.
4. **Does the sender want delivery receipts?** The data is there
   (`delivered_to`, `acked_at`); the question is whether to surface a
   "your message to opencoti was acked" notice back to the sender.

---

## Rollout

1. Table + `MailboxStore` on the existing pgvector connection (lock-
   reusing), plus the sqlite_vec variant.
2. Five MCP tools, with the same byte-identical output formatting both
   MCP servers already share via `claude_hooks/mcp_format.py`.
3. Session registry write at `SessionStart` / refresh at
   `UserPromptSubmit`.
4. Announcement block in the two recall hooks, behind
   `hooks.mailbox.enabled` (default **off** until proven).
5. Tests: addressing resolution, alias-with-no-session, delivered-vs-
   acked, expiry, and a concurrency test that runs a mailbox check
   against the same connection as a parallel recall.
6. Only then: retire `/shared/dev/handover/`.
