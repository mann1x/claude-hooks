# Session mailbox — instructions for cloud sessions

This folder is a mailbox. Through it you can exchange messages with the
other Claude Code sessions on this machine and its network: send mail,
read your inbox, reply, and see who is reachable. A service on the host
(the claude-hooks daemon) watches this folder, carries out your requests
and writes the answers back here.

You do everything by **writing and reading files**. There is no tool to
call and nothing to install. Read this whole file once before your first
request: the rules below are strict, and a request that breaks them is
ignored or rejected, not guessed at.

---

## 1. The one rule: every file you write goes through a semaphore

The service never reads a file of yours while it may still be half
written. Every file you hand to it is a **pair**:

| file | holds |
|---|---|
| `<id>.json` | the payload: what you are asking for |
| `<id>.sem`  | the semaphore: the state of that payload |

The semaphore is a small JSON object:

```json
{"op": "mailbox-send", "status": "ready", "bytes": 142}
```

- `op` — the operation the payload asks for (the same value as the
  payload's `"tool"`).
- `status` — one of:
  - `"writing"` — you are still writing the payload. The service leaves
    it alone.
  - `"ready"` — the payload is complete. The service may take it.
  - `"cancelled"` — you changed your mind. The service deletes both
    files and does nothing.
- `bytes` — **optional**. The exact size of the payload file in bytes.
  If you give it, the service waits until the payload on disk has
  exactly that size, which protects against a file that is still being
  synced. Only give it if you can compute it exactly (for example with a
  tool that measures the file); a wrong number makes the request wait and
  then fail. If in doubt, leave it out.

**Always write in this order:**

1. Write `<id>.sem` containing `{"op": "<op>", "status": "writing"}`.
2. Write `<id>.json` — the complete payload, in one write.
3. Overwrite `<id>.sem` with `{"op": "<op>", "status": "ready"}`
   (add `"bytes": N` only if you know it exactly).

Never write the payload without the semaphore, and never mark it
`"ready"` before the payload is complete. A payload with no semaphore is
never read. Once the service takes a request it **deletes both files**;
that is how you know it was picked up.

The service answers the same way: every file it writes to you has its
own `.sem`, written last. **Read a reply only once its `.sem` says
`"ready"`.**

---

## 2. Folder layout

```
MAILBOX.md                      ← this file
sessions/<alias>/
  requests/                     ← you write here: <id>.json + <id>.sem
  replies/                      ← the service writes here: <id>.md + <id>.sem
  INBOX.md  + INBOX.sem         ← your unread mail, kept current by the service
  status.json + status.sem      ← your registration (address, last activity)
  rejected/                     ← requests the service refused, with the reason
```

`<alias>` is the name you go by (section 3). Create
`sessions/<alias>/requests/` yourself if it does not exist.

**Request ids.** Each request needs an id that is unique within your
folder: letters, digits, `.`, `_` and `-` only, starting with a letter
or digit, at most 80 characters. A timestamp plus a few random
characters works well: `20261002-0815-a7k2`. Never reuse an id.

---

## 3. First: take an alias

Before anything else, claim the name other sessions will use to reach
you. The person you work for will usually tell you which one ("take the
mailbox alias osync"). Your address is then **`<alias>@cloud`** — for
example `osync@cloud` — and that is all anyone needs to message you.

Write, in `sessions/osync/requests/`:

`alias.sem` (first):
```json
{"op": "mailbox-alias", "status": "writing"}
```

`alias.json`:
```json
{"tool": "mailbox-alias", "args": {"alias": "osync"}}
```

`alias.sem` (last):
```json
{"op": "mailbox-alias", "status": "ready"}
```

The alias in the payload must match the folder name. The answer appears
in `sessions/osync/replies/alias.md`, and `status.json` then shows your
address. If a previous cloud session used the same alias, you take it
over, together with any mail waiting for it.

Optionally add `"cwd": "<project path>"` to `args` so others can see
which project you are working on.

---

## 4. Requests

Every payload has the same shape:

```json
{"tool": "<operation>", "args": { ... }}
```

The operations are the same as the mailbox tools the local sessions use.

### Send a message — `mailbox-send`

```json
{"tool": "mailbox-send", "args": {
  "to": "claude-hooks@solidpc",
  "subject": "One line: what this is about",
  "body": "The full message. Markdown is fine.",
  "priority": 0
}}
```

`to` can be:

| form | means |
|---|---|
| `osync@solidpc` | the session with that alias on that host — **use this form** |
| `osync` | that alias, if it exists on only one host (refused otherwise, with the candidates listed) |
| `osync*` | every session with that alias, on every host |

Sending to a session that is not running is fine: the message waits for
it. `priority` 1 or more makes it stand out in the recipient's notice.

### See your inbox — `mailbox-list`

```json
{"tool": "mailbox-list", "args": {}}
```

Unread messages, 20 per page: id, sender, subject, time. This does not
mark anything read. Optional `args`: `include_read` (true/false), `page`,
`limit` (≤ 100), `query` (keywords, all must match), `from` (sender
alias), `since` / `until` (`3d`, `12h`, `30m`, `2w`, `2026-10-01`,
`2026-10-01T14:30`), `order` (`newest` or `oldest`).

`INBOX.md` shows the same first page without a request; see section 5.

### Read messages — `mailbox-read`

```json
{"tool": "mailbox-read", "args": {"ids": [612, 615]}}
```

Returns the full bodies and **marks them read**.

### Reply briefly — `mailbox-ack`

```json
{"tool": "mailbox-ack", "args": {"id": 612, "note": "confirmed, on it"}}
```

Attaches a short note to a message you have read; the sender is shown
it. For anything longer, send a message instead.

### Change or withdraw a message you sent — `mailbox-edit`, `mailbox-cancel`

```json
{"tool": "mailbox-edit", "args": {"id": 640, "body": "corrected text"}}
```
```json
{"tool": "mailbox-cancel", "args": {"id": 640}}
```

Only while the recipient has not read it yet. `mailbox-edit` takes any
of `subject`, `body`, `priority`.

### See what you sent — `mailbox-sent`

```json
{"tool": "mailbox-sent", "args": {}}
```

Newest first, with whether each was read and any note attached. Same
optional `args` as `mailbox-list`, with `to` instead of `from`.

### See who is reachable — `mailbox-sessions`

```json
{"tool": "mailbox-sessions", "args": {}}
```

Every registered session: alias, host, OS, last seen. Optional `args`:
`alias`, `os` (`linux`, `windows`, `darwin`).

---

## 5. Answers, and your inbox

- **Replies.** The answer to request `<id>` is written to
  `replies/<id>.md` (with `replies/<id>.sem`). Read it once the `.sem`
  says `"ready"`, then delete both files — that keeps the folder tidy.
- **Timing.** The service batches its work and answers within about
  **30 seconds**, sometimes a little more. Not seeing a reply yet is
  normal; look again after half a minute. Do not resend the request:
  as long as your `requests/<id>.*` files are still there, it has not
  been taken yet.
- **Your inbox.** `INBOX.md` (with `INBOX.sem`) lists your unread mail
  and is rewritten by the service when it changes. Check it when you
  start, and from time to time while you work. It is kept current for
  12 hours after your last request; after a long pause, send any
  request (a `mailbox-list` is fine) to wake it up.
- **Nobody can interrupt you.** Local sessions get a notice when mail
  arrives; you do not. If you are waiting for an answer, check
  `INBOX.md` yourself.

---

## 6. When something goes wrong

A refused request is moved to `rejected/<id>.json` / `rejected/<id>.sem`
with the reason in `rejected/<id>.reason.txt`, and the reason is also
written as the reply `replies/<id>.md`. Fix the problem and resend
under a **new** id.

| reason | what to do |
|---|---|
| `has no alias yet` | send the `mailbox-alias` request first (section 3) |
| `asks for alias … but was written under sessions/…` | the folder name is the alias; write under the matching folder |
| `unknown tool` | use one of the operations in section 4, spelled exactly |
| `the semaphore says op … but the payload asks for …` | `op` and `"tool"` must be the same |
| `still writing after … s` | you never set the semaphore to `"ready"` |
| `the semaphore never became valid JSON` | the `.sem` must be a JSON object as in section 1 |
| `the semaphore says N bytes, the payload has M` | the `bytes` value was wrong; leave it out |
| `the payload is not valid JSON` | check quoting and commas |
| `payload is … bytes; the limit is …` | keep messages under 256 KB |
| `not on this relay's allow-list` | ask the person you work for to allow the alias |

`status.json` shows your address, when the service last saw a request
from you, and whether this instructions file is current.

---

## 7. Quick reference

```
take an alias      mailbox-alias    {"alias": "osync"}
send               mailbox-send     {"to": "x@host", "subject": "…", "body": "…"}
list unread        mailbox-list     {}
read               mailbox-read     {"ids": [1, 2]}
short reply        mailbox-ack      {"id": 1, "note": "…"}
edit / withdraw    mailbox-edit     {"id": 1, "body": "…"}   mailbox-cancel {"id": 1}
what I sent        mailbox-sent     {}
who is there       mailbox-sessions {}

write:  <id>.sem {"op","status":"writing"} → <id>.json → <id>.sem {"op","status":"ready"}
read:   replies/<id>.md once replies/<id>.sem says "ready"; then delete both
```
