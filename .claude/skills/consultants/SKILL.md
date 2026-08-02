---
name: consultants
description: Multi-agent council engine (v2: planner → researcher → critic → synthesizer, plus opt-in tool_executor + coder; CitationLinter verifies every path:line). Default verb is `ask <question>` (also implicit — `/consultants <question>` works). Subcommands — `ask` runs a fresh council on a question; `followup [sid] <question>` iterates on a prior consult (warm reuse of plan/research/critic); `list` shows past sessions; `show <sid>` re-reads a stored summary; `accept <sid>` marks a consultancy reviewed/done; `config [args...]` walks the role/model/effort/service-mode/followup-limit dialog. After every council answer Claude runs a review loop (mirroring /get-advice): it critiques the result and either accepts it or auto-issues a bounded follow-up (capped by `max_followups`, default 4; asks you to allow more past the cap). Use when a question benefits from independent specialist agents working in parallel — design audits, release-notes validation, complex bug triage, refactor risk analysis. For single-shot questions use /get-advice instead.
---

# /consultants — multi-agent council dispatcher

You are the dispatcher for a council of LLM specialist agents
(planner, researcher with project tools, critic, synthesizer; plus
two opt-in roles — `tool_executor` for PLAN-REPORT-split tool
execution, and `coder` for sandboxed `write_file` lanes; both
default-off as of 2026-05-18) and its supporting management
commands. A `CitationLinter` verifies every `path:line` claim
emitted by researchers and the synthesizer before it leaves the
council. The engine runs as a sibling service on
`127.0.0.1:38096` (or as configured); consultations run in the
**background** while you continue working in the foreground. See
[`docs/consultants-roles.md`](../../docs/consultants-roles.md)
for the role-by-role reference.

## ⚠️ Activation Guard — read first

This skill is prone to mis-firing after context compression because the
SKILL.md text gets reinjected as a system-reminder, which can read like
fresh instructions. **Do not act on reinjection.**

**Only execute the workflow below when ALL of these are true:**

1. The user's **current turn** explicitly invokes the skill — typing
   `/consultants`, or saying "ask the council", "run a consult on…",
   "follow up on csl-…", "show me consult <sid>", or equivalent.
2. The skill is being called for the *first time* in the current
   user request (not an echo from a `<system-reminder>` that lists
   "skills invoked EARLIER in this session").

**Do NOT execute when:**

- A system-reminder lists this skill among "skills invoked earlier" —
  that block is *context only* and explicitly tells you not to re-run.
  Past consult output is already in context; do not re-fire on the
  same question.
- The user's current message is unrelated to running a council
  (e.g. they're asking you to investigate code, fix a bug, review a
  PR, or continue prior work that did not start with `/consultants`).
  Mentioning a `csl-…` sid in passing is **not** an invocation; quoted
  output from an earlier consult is **not** an invocation.
- You only "remember" running a consult earlier in the session —
  past results are already in context; do not re-poll, do not
  re-fetch, do not start a new ask.
- You are uncertain whether the user wants a fresh council run.
  **Ask first** — one short clarifying question is cheaper than
  spinning up a 1–5 minute multi-agent run the user didn't request.

If the trigger is ambiguous, default to asking
"Do you want me to start a /consultants run on this?" instead of
silently kicking off `claude-consultants consult`.

## Verb dispatch

Parse the first whitespace-separated token of the user's args
after `/consultants`:

| First token  | Verb       | Action                                                         |
|--------------|------------|----------------------------------------------------------------|
| `ask`        | ask        | Drop the verb; run the council on the rest as the question.    |
| `followup`   | followup   | Drop the verb; run **followup** flow on the rest.              |
| `list`       | list       | Run **list** flow.                                             |
| `show`       | show       | Drop the verb; the rest is the sid.                            |
| `accept`     | accept     | Drop the verb; mark the consultancy (rest = sid) ACCEPTED.     |
| `config`     | config     | Drop the verb; run **config** flow with the rest as sub-args.  |
| anything else (incl. empty) | ask | Implicit ask: treat the **entire** arg as the question. |

Examples:

- `/consultants what's wrong with foo` → implicit **ask**
- `/consultants ask validate release notes` → explicit **ask**
- `/consultants followup csl-2026-... drill into point 3` → **followup** with explicit parent sid
- `/consultants followup what about edge case X` → **followup**, auto-pick most-recent parent
- `/consultants list --limit 20` → **list**
- `/consultants show csl-2026-05-06-1730-3f9a` → **show**
- `/consultants config` → **config** (no sub-args → walk the dialog)
- `/consultants config set-effort high` → **config** with passthrough args

---

## How a consultation works

Each `consult` starts a **council run** — one LangGraph invocation whose
per-run `status` goes `running → completed | failed`. The chain rooted at
the first `ask` (the `ask` itself + every `follow-up` chained on it) is
the **consultancy**, with its own engine-owned, persisted lifecycle:

> `in_progress` → *(council done)* `ready_to_review` → `accepted`
> *(terminal)*. An auto-issued `follow-up` under the cap bounces back to
> `in_progress`; hitting the cap (`max_followups`, default **4**) flips to
> `awaiting_approval` until the user approves more rounds. The grant size
> per approval is `allow_extra` (default **1**); both knobs are
> configurable via `/consultants config` and the CLI.

Every `status` / `result` / `follow-up` / `state` response carries the
authoritative `consultancy` block — read this, don't infer:

```json
"consultancy": {"root_sid": "csl-…", "status": "ready_to_review",
  "followup_count": 1, "max_followups": 4, "extra_granted": 0,
  "effective_cap": 4}
```

**While the run is `running`, wait for it with ONE long-lived waiter**
— never a foreground poll loop. See [Waiting for a
run](#4-wait-for-the-run--the-only-three-patterns) for the three
supported patterns and the anti-pattern they replace.

**When the council finishes**, do NOT silently present the answer — the
consultancy is in `ready_to_review`. Enter the [Review
loop](#review-loop--mirror-get-advices-discuss-until-satisfied-flow):
critique → `accept` (terminal) **or** auto-`follow-up`. At the cap
(`awaiting_approval`) stop and ask the user; on "yes" re-issue the same
follow-up with `--allow-extra 1` (the user never types the flag, the
skill carries their approval). The lifecycle survives compaction —
trust `consultancy.status` over your own memory of where you were.

---

## ask — run a fresh council consultation

A consultation takes minutes to tens of minutes (33 min is normal at
`xhigh`). Start ONE long-lived waiter (step 4), tell the user what you
kicked off, and keep them productive while it runs — never sit in a
foreground poll loop, and never re-check on your own cadence.

### 1. Read engine settings

```
claude-consultants config show
```

Parse the JSON for `topology`, `effort`, `effort_budget`,
`endpoint`, per-role `enabled` + `model`, `service.mode`. If the
endpoint is unreachable later, surface and recommend
`/consultants config` to inspect.

### 2. Craft the framing message

Include:

- **The user's question** verbatim or lightly tightened.
- **Project context** (1–3 sentences: what this project does, the
  state of the area, files / paths the council should focus on).
- **What "good" looks like** — e.g. "yes/no on whether the LR
  schedule is reasonable, plus one concrete change if you'd alter it".
- **Where to look** — concrete `path:line` or directory pointers.
  Don't paste large code blocks; the researcher has tools.

Keep framing under ~1 KB.

**Compose an adversarial focus when the stakes warrant it.** If a
*wrong-but-plausible* answer would be costly — an architecture call, a
security or correctness claim, a "is X safe to do" question — name, in
one or two sentences, the specific way the answer is most likely to be
wrong (the unstated assumption, the edge case, the claim that's true in
general but false here). You have two places to put it, depending on
timing: as a one-line "adversarial focus" note inside the framing so the
critic carries it from the start, or — for a live nudge mid-flight — via
`control --strictness adversarial --adversarial-focus "<brief>"` (see
the **Dynamic adversary** subsection of the review loop). **Skip this
entirely for list / lookup / "what does X do" questions** — there's no
plausible-but-wrong trap to set against them, and the directive only
adds noise.

### 3. Start the consultation

```
claude-consultants consult --message "<your framing>" --cwd "$(pwd)"
```

Optional `--effort low|medium|high|max|xmedium|xhigh|xmax` to
override the configured tier for this call.

**Reaching files outside cwd.** The council's tools are sandboxed
to `cwd` plus whatever Claude Code's
`permissions.additionalDirectories` (auto-discovered from
`~/.claude/settings.json` and `.claude/settings.local.json`) already
permits. When the question references files outside that union,
pass `--add-dir <path>` once per extra root (repeatable). Follow-ups
inherit the parent session's `extra_roots` and may extend them with
their own `--add-dir`:

```
claude-consultants consult --message "<framing>" --cwd "$(pwd)" \
  --add-dir /shared/dev/lightseek

claude-consultants follow-up <parent_sid> --message "<focused>" \
  --cwd "$(pwd)" --add-dir /opt/llama.cpp
```

Paths in the question may be written relative to *any* root — the file
tools try `--cwd` first, then each `--add-dir` in order — so you do not
need to rewrite `eval/scorers.py` as an absolute path before asking.
Do still pass the root: a path with no root that contains it is what
pre-flight refuses on.

Returns:

```json
{"ok": true, "sid": "csl-2026-05-06-1730-3f9a",
 "status": "running", "status_url": "/v1/consult/csl-..."}
```

Save the `sid`. Tell the user briefly you've kicked off the council
and will surface progress.

### 4. Wait for the run — the only three patterns

A council takes **minutes to tens of minutes** (33 min is normal at
`xhigh`). One Bash tool call is bounded by a timeout far shorter than
that, so the waiting has to live somewhere that outlasts a single
foreground call.

**Never do this.** It is the most common failure mode, it burns a full
tool timeout, and it never sees the run finish:

```bash
# ANTI-PATTERN. Do not write this.
for i in 1 2 3 4 5 6; do
  s=$(claude-consultants status <sid> | jq -r .status)
  case "$s" in completed*|failed*) break;; esac
  sleep 45
done
```

Six iterations at 45 s is 4.5 minutes against a 33-minute run: it
always exits still `running`, you learn nothing, and repeating it
spends the consultation's whole wall-clock on tool calls. `jq` is also
not guaranteed to be installed — the examples here use `python3 -c`.

Pick a pattern by whether the run can pause for you:

#### A. Adversary checkpoint OFF → `--wait`, backgrounded

`consult` and `follow-up` both accept `--wait`: the CLI blocks
server-side until the run is terminal, then prints the **result** JSON
(`{"ok": true, "summary_markdown": …}`), exiting 1 on failure.

Issue it as a **background** Bash call (`run_in_background: true`).
The harness notifies you when the process exits, so there is no
polling and completion cannot be missed:

```
claude-consultants consult --wait --message "<framing>" --cwd "$(pwd)"
```

`--wait-timeout <s>` bounds the client only; the run continues
server-side if the wait gives up.

#### B. Adversary checkpoint ON → wait for terminal *or* pause

`--wait` would sit through the entire checkpoint (up to
`adversary_checkpoint_timeout_s`, default 30 min) without telling you
it wants an answer. Launch without `--wait`, then run **one**
backgrounded loop that exits on either signal:

```bash
while :; do
  claude-consultants status <sid> > /tmp/st.json
  python3 -c "import json,sys; d=json.load(open('/tmp/st.json')); sys.exit(0 if d.get('status')!='running' or d.get('adversary_checkpoint_deadline_ts') or d.get('pending_tool_approvals') else 3)" && break
  sleep 20
done
claude-consultants status <sid>
```

Two fields appear in `status` **only while something is waiting on
you**, and either one means "answer me":

- `adversary_checkpoint_deadline_ts` — the engine paused before
  synthesis for an assistant-authored challenge. Write it, then
  release with `claude-consultants adversary-ack <sid>`. Details in
  [Adversarial review](#adversarial-review).
- `pending_tool_approvals` — a lane is parked on an `ask_human` tool
  call. Answer with `claude-consultants tool-ack <sid> --allow|--deny`,
  and prefer `--all-of-tool` / `--all-matching '<glob>'` so the next
  lane doesn't park on the same question. `waiters` on an entry tells
  you how many lanes one answer releases. Details in
  [Subflow I](#subflow-i--tool-surface).

Both carry the wall-clock at which the engine gives up. The adversary
checkpoint auto-resumes on timeout; a tool approval is **denied** on
timeout.

#### C. Live visibility wanted → `events --milestones`, backgrounded

```
claude-consultants events <sid> --milestones
```

**Always pass `--milestones`.** The unfiltered stream is dominated by
`llm_call` and `tool_call` records — hundreds per council, each with a
full payload — and it is unusable as a monitor: the one event that
needs an answer scrolls past inside thousands of lines. `--milestones`
keeps only state changes and renders one compact line each:

```
07:30:07 node_enter         role=researcher round=1 lane_idx=0
07:31:09 node_exit          role=researcher round=1 lane_idx=0
07:56:42 awaiting_adversary reason=adversary_checkpoint timeout_s=1800.0
08:03:35 complete           status=completed final_answer_present=True
```

`complete` is the canonical done signal; `awaiting_adversary` is the
"answer me" signal. Backgrounded, the stream's exit *is* the
completion notification. `--kinds a,b` narrows further; `--since
<event_id>` resumes after a blip or across a compaction boundary.

**In all three cases**: start the waiter, tell the user what you
kicked off, and get on with other work. Do not re-check on your own
cadence — the notification is the signal. When you do have progress
(pattern C, or the status you read on exit), surface transitions in
plain language: "Planner done; researcher mid-investigation, 3 tool
calls so far." One line per transition, no spam.

### 5. Fetch the result

```
claude-consultants result <sid>
```

Print `summary_markdown` verbatim (it's already formatted; don't
re-wrap). End with a footer:

```
sid: csl-2026-... · effort: medium · duration: 4m 12s ·
models: planner=kimi-k2.6:cloud researcher=qwen3.5:cloud
critic=kimi-k2.6:cloud synthesizer=kimi-k2.6:cloud
```

The session is permanent at
`.claude-hooks/consultants/<sid>/{summary,transcript,metadata}.*`
and can be re-read via `/consultants show <sid>`.

### 6. Review the answer — DON'T just move on

When the council finishes, the consultancy enters **`ready_to_review`**
and the answer is yours to judge — exactly like the back-and-forth you
hold with the advisor in `/get-advice`. **Enter the [Review loop](#review-loop--mirror-get-advices-discuss-until-satisfied-flow)
below** rather than silently accepting the first answer.

---

## followup — iterate on a prior consultation

The follow-up **reuses** the parent's per-role message history.
Planner / researcher / critic / synthesizer each pick up where they
left off, so this is materially different from running `ask` again
with a related question — the answer is continuous with the parent.

Use this when the user wants to push back on the synthesizer's
recommendation, drill into a specific point, or ask a related
question that depends on the parent's grounding. For a fresh
question that doesn't depend on prior context, use `ask` instead.

### 1. Resolve the parent sid

Parse the user's args. Look for a leading positional matching
`csl-<YYYY-MM-DD>-<HHMM>-<hex>`; the rest is the follow-up message.

**Explicit sid present:** use it directly. Don't second-guess.

**No sid:** auto-pick the most recent session of any status:

```
claude-consultants list --cwd "$(pwd)" --limit 10
```

Three cases by the newest entry's `status`:

- **`completed`** — use as parent. Tell the user
  "Following up on csl-..., your last completed consultation".
- **`failed`** — chaining off a failed sid is usually CHEAPER than
  chaining off its parent (researcher + critic work survived in
  transcript.db; only the synthesizer flapped). AskUserQuestion:
  (a) **Chain off the failed session (recommended)** — warm reuse;
  (b) **Chain off the failed session's `parent_sid`** — fresh
  research from the last known-good upstream; (c) **Cancel**.
  To resolve the failed session's `parent_sid`, call
  `claude-consultants result <failed_sid>` and read `metadata.parent_sid`.
- **`running`** — previous consultation hasn't finished. Offer to
  wait (poll until complete, then follow up) or cancel. Don't fire
  against a running session.

Empty list → say so plainly; suggest `/consultants <query>` to
start a fresh consultation.

### 2. Optional warmth probe (one quick poll)

```
claude-consultants list-open
```

If the parent sid is in the warm pool, follow-up resolves
sub-second; if not, it auto-reopens from disk (~5–10s). Mention so
a 10s pause doesn't look like a hang. Don't gate on it.

### 3. Fire the follow-up

```
claude-consultants follow-up <parent_sid> --message "<follow-up text>" --cwd "$(pwd)"
```

Optional `--effort ...` to override the tier for this follow-up
(defaults to parent's). Returns:

```json
{"ok": true, "sid": "csl-...-NEW", "parent_sid": "...",
 "status": "running", "status_url": "/v1/consult/csl-..."}
```

### 4. Wait + fetch (same as ask)

Wait with one of the [three patterns](#4-wait-for-the-run--the-only-three-patterns)
— `follow-up --wait` backgrounded is the default — then
`result <new_sid>`. Never a foreground `for`/`sleep` poll loop.
Print `summary_markdown` verbatim. Use an extended footer to thread
the lineage:

```
sid: csl-...-NEW · parent: csl-...-OLD · effort: medium ·
duration: 1m 47s · models: planner=... researcher=... critic=...
synthesizer=...
```

Each follow-up is itself a permanent session and can be the parent
of further follow-ups. Chains are intended for iterative refinement
on a single deep topic. After printing a follow-up's answer, **re-enter
the [Review loop](#review-loop--mirror-get-advices-discuss-until-satisfied-flow)** —
a follow-up answer is reviewed exactly like a fresh one.

---

## Review loop — mirror /get-advice's discuss-until-satisfied flow

This is the heart of the skill, and the part most easily skipped. In
`/get-advice` you keep talking to the advisor until satisfied; do the
same with the council. The **council** runs (its per-run `status` goes
`running → completed`); the **consultancy** is the whole engagement and
has its own status (`consultancy.status` in the `status` / `result` /
`state` JSON): `in_progress → ready_to_review → accepted`, or
`awaiting_approval` when the followup cap is hit.

Every `status` / `result` / `follow-up` response now carries a
`consultancy` block:

```json
"consultancy": {"root_sid": "csl-…", "status": "ready_to_review",
  "followup_count": 1, "max_followups": 4, "extra_granted": 0,
  "effective_cap": 4}
```

When a council answer arrives (`consultancy.status == "ready_to_review"`):

### 1. Review the answer critically

Read it as a skeptical engineer, not a stenographer. Look for:

- **Wrong assumptions** about the project (it claims a file/flag/API that
  doesn't exist, or contradicts how the repo actually works).
- **Gaps** — an important sub-question went unanswered, or a
  recommendation has no concrete "how".
- **Important advice worth verifying** — a `path:line` claim you can
  cheaply check, a risky suggestion that needs a second pass.

If you have no material concern, the answer is good enough → **accept**.

### 2. Decide: accept or follow up

**Satisfied →** mark it accepted (terminal) and report:

```
claude-consultants accept <sid>
```

Then present `summary_markdown` to the user as the final answer.

**Not satisfied, and `followup_count < effective_cap` →** post a
**one-line rationale** (so the user can interrupt), then auto-issue a
focused follow-up and loop back to step 1:

> Council assumes the daemon reads `~/.claude.json`, but this repo uses
> `config/claude-hooks.json` → follow-up 2/4 to re-ground that claim.

```
claude-consultants follow-up <sid> --message "<specific clarifying question>" --cwd "$(pwd)"
```

Wait for `<new_sid>` the same way you waited for the parent
([three patterns](#4-wait-for-the-run--the-only-three-patterns)),
fetch `result <new_sid>`, and
**return to step 1** on the new answer. Do this WITHOUT asking the user
each round — that's the whole point of the bounded auto-loop.

**Not satisfied, and the cap is reached →** the follow-up call returns
`{"ok": false, "reason": "followup_limit_reached", ...}` (and
`consultancy.status` is `awaiting_approval`). **STOP the auto-loop and
ask the user.** Present:

- the remaining concern(s), concretely;
- why another round is warranted (what you expect it to resolve);
- the count so far (e.g. "4/4 followups used").

If the user approves, re-issue the same follow-up with the approval
carrier — you never need the user to type a flag; their "yes" is the
trigger:

```
claude-consultants follow-up <sid> --message "<question>" --cwd "$(pwd)" --allow-extra 1
```

(`--allow-extra N` raises the cap by N for THIS consultancy only — no
config change. Bare `--allow-extra` / `--force` uses the configured
`allow_extra` default.) Then continue the loop. If the user declines,
`accept` the best answer so far and report.

### 3. Stop conditions (mirror /get-advice)

Stop the loop when **any** of:

- You accepted (`accept` succeeded) — the answer is good enough.
- The cap was reached and the user declined more rounds.
- **Diminishing returns** — two consecutive follow-ups produced no
  material improvement. Accept the best answer and note the plateau.

### 4. Compaction survival

If your context was compacted mid-loop, on re-entry **read
`claude-consultants status <root_sid>` first** and branch on
`consultancy.status`:

- `accepted` → already done; don't re-run.
- `ready_to_review` / `in_progress` → resume the loop from step 1 on the
  latest session.
- `awaiting_approval` → you were waiting on the user; re-present the
  concern and ask.

The status is engine-owned and persisted, so it's authoritative across
the compaction boundary — trust it over your own memory of where you were.

### 5. Dynamic adversary — author the challenge, react to the checkpoint

The review loop above is *post-hoc* skepticism — you challenge the answer
after it lands. The **dynamic adversary** is the same instinct moved
*earlier*: you compose a bespoke challenge and feed it into the council
while it runs, so the critic is already hunting for the weakness before
synthesis. Two mechanisms, both opt-in (Subflow G):

**Authoring template (the brief).** When you decide a question warrants
an adversary (the framing-step rule: costly if wrong, not a lookup),
write a 2–4 line brief that:

1. **Names 2–3 specific claims to attack** — the load-bearing assertions
   whose failure would sink the answer ("it assumes the daemon reads
   `~/.claude.json`"; "it treats the cache as write-through").
2. **Sets the refutation bar** — what counts as a real refutation vs.
   nitpicking ("only flag a claim if you can point at the file that
   contradicts it").

Deliver it as `control --strictness adversarial --adversarial-focus
"<brief>"` (re-shapes the next critic + meta-critic call), or — for the
strongest form — via the **adversary checkpoint**.

**Reacting to `awaiting_adversary` (the checkpoint).** When
`adversary_checkpoint` is ON, the council pauses just before synthesis
and emits an `awaiting_adversary` event carrying a `deadline_ts` and the
synthesizer's `self_confidence`. React like this:

1. **Subscribe** to the stream — `claude-consultants events <sid>` (or
   `--since <id>` to replay across a reconnect / compaction). The
   `awaiting_adversary` block is durable, so a missed SSE is recoverable.
2. **Author + inject** the brief at the role the critique should re-run
   through: `inject <sid> --role critic -m "<brief>"` to re-run the
   fanned critics against it, or `--role synthesizer` to just sharpen the
   final write-up. These checkpoint/control verbs (`events`, `inject`,
   `adversary-ack`, `resume`) address the run by **`sid`** through the
   engine — they take **no `--cwd`** (unlike `consult` / `follow-up` /
   `config`).
3. **Ack to resume** — `claude-consultants adversary-ack <sid>` (or
   `resume <sid>` during the checkpoint window, which delegates to the
   ack). The council resumes immediately with your brief in the prompt.
4. **Or do nothing** — the checkpoint **auto-proceeds at the deadline**
   (default 10 min) so a lost SSE / missed poll never hangs the run. If
   you have no challenge worth making, just let it lapse.

Don't open a checkpoint you won't staff: the pause is wall-clock the user
waits through. Author the brief *first*, then ack — not the reverse.

---

## accept — mark a consultancy accepted

```
claude-consultants accept <sid> [--note "..."] --cwd "$(pwd)"
```

Sets the consultancy (resolved to its root) to the terminal `accepted`
state. Normally the Review loop calls this for you; a user can also
invoke it directly to close out a consultancy. Idempotent.

---

## list — show past consultations

```
claude-consultants list --cwd "$(pwd)" [--limit N] [--state STATE]
```

Returns JSON. Format as a compact table or numbered list,
newest-first, including sid, status, effort, duration, and the
question preview (truncated to ~80 chars). Mention that
`/consultants show <sid>` prints any of them in full. Empty list →
say so plainly; suggest `/consultants <query>` to start.

---

## show — print a stored session

Parse the user's args after the `show` verb for one positional sid.

```
claude-consultants show <sid> --cwd "$(pwd)"
```

Reads disk only — no engine call. Print `summary_markdown` verbatim
(front-matter + synthesizer's final answer). Short footer:

```
sid: csl-... · effort: medium · duration: 4m 12s · status: completed
```

If the sid doesn't exist, the CLI returns
`{"ok": false, "error": "no summary for sid ..."}` — surface and
suggest `/consultants list`.

For the full transcript (planner + researcher tool calls + critic
verdict + synthesizer turns), point the user at
`.claude-hooks/consultants/<sid>/transcript.md` — typically too long
for chat. With `--raw`, the CLI dumps the events table from
transcript.db as one JSON object per line.

---

## config — interactive configuration

You drive the underlying `claude-consultants config <subcommand>`
CLI, which validates inputs and atomically writes the TOML file.
Never edit the file directly.

If the user passed sub-args (e.g. `/consultants config set-effort high`),
forward them verbatim:

```
claude-consultants config <subcommand> [args...] --cwd "$(pwd)"
```

Otherwise, walk the interactive dialog:

### 1. Show current state

```
claude-consultants config show --cwd "$(pwd)"
```

Render a status block. Append `, multi-model active` to the Effort
line when `extras_active` is `true`. Show `extra_models` for
researcher + critic (with `(none)` when empty); hide for planner +
synthesizer (engine doesn't fan those out). ctx_max is `auto` when
null, `<N>` when set.

**Scope banner.** Read the `active_config` block in the JSON and put
a `Config scope:` line at the top of the render. When
`active_config.scope` is `project`, say **PER-PROJECT** and show the
path — every change you make in this dialog lands in that file, not
user-global. When it's `user` but `project_file_exists` is `true`,
note the per-project file is present but dormant
(`override_user_global=off`). The CLI also prints this on stderr;
relay that one-liner verbatim if the user is scripting.

```
=== /consultants config ===
Config scope: PER-PROJECT (.claude-hooks/consultants.toml, override_user_global=on)
Service mode: smart-start (idle 30 min)   |   always-on
Endpoint: http://127.0.0.1:38096
Effort: xhigh (budget 5, multi-model active)
Topology: council
Review loop: max_followups=4, allow_extra=1

Roles:
  planner       ENABLED   model=kimi-k2.6:cloud         ctx=auto
  researcher    ENABLED   model=qwen3.5:cloud           ctx=auto
                  extra_models: kimi-k2.6:cloud, deepseek-v4-pro:cloud
  critic        ENABLED   model=deepseek-v4-pro:cloud   ctx=auto
                  extra_models: (none — only used at xmax)
  synthesizer   ENABLED   model=kimi-k2.6:cloud         ctx=auto
                (synthesizer cannot be disabled)
  coder         DISABLED  model=glm-5.1:cloud           ctx=auto
                  default → glm-5.1:cloud → kimi-k2.6:cloud
                  routes:
                    c       → glm-5.1:cloud         → deepseek-v4-pro:cloud
                    cpp     → deepseek-v4-flash:cloud → kimi-k2.6:cloud
                    csharp  → deepseek-v4-pro:cloud → kimi-k2.6:cloud
                    go      → kimi-k2.6:cloud       → deepseek-v4-pro:cloud
                    python  → glm-5.1:cloud         → kimi-k2.6:cloud
                    rust    → deepseek-v4-flash:cloud → deepseek-v4-pro:cloud
```

When rendering the "coder" sub-block, surface the
`coder.default_route` (as `default → primary → fallback`) and each
per-language entry the same way. The routes only matter when
`coder` is `ENABLED`; render the block in dim/grey when disabled
(but still show it so the user knows the routing config exists).

### 2. Top-level menu (loop until Done)

The menu has eight areas; AskUserQuestion caps at 4 options, so
present it in rounds — round 1 offers the first three plus **More…**,
and **More…** opens the next batch (ending with **Done**):

1. **Edit a role** — toggle on/off, model, ctx, extras
2. **Change service mode** — always-on or smart-start
3. **Change effort tier** — low/medium/high/max, or x-prefixed
   variants for multi-model fan-out
4. **Coder routing** — per-language model selection (primary +
   fallback) for the optional coder role
5. **Followup limit** — the review-loop cap (`max_followups`) and the
   per-approval grant size (`allow_extra`) — see Subflow F
6. **Adversary / verify budget** — the optional `adversary` role, its
   strictness, the engine-initiated adversary checkpoint, and the
   skeptic-panel `verify_budget` — see Subflow G
7. **Config scope** — choose user-global vs per-project and flip the
   per-project `override_user_global` directive — see Subflow H
8. **Tool surface** — which tools the council can reach and what each
   one needs before it runs — see Subflow I

Loop back to step 1 after each successful change; exit on **Done**.

### Subflow A — Edit a role

AskUserQuestion the role (`planner`/`researcher`/`critic`/
`synthesizer`), then the sub-action:

- **Toggle enabled** — only for non-mandatory roles. Reject
  friendlily for `synthesizer`. Warn if disabling planner AND
  researcher (validation error on next consult).
- **Change model** → `claude-consultants config list-models`,
  pick up to 3 candidates + `Other (custom)`, then
  `set-role <role> --model <chosen>`.
- **Manage extra models** — researcher + critic only. Sub-action:
  add / remove / clear / back. Forward to
  `set-role <role> --add-model|--remove-model|--clear-extras ...`.
  On the FIRST add, surface once:
  > Note: extras are only consulted at the x-prefixed effort tiers
  > (xmedium / xhigh / xmax). At low/medium/high/max the primary
  > model runs alone — the extras list is silent.
- **Pin context length** — AskUserQuestion `8192` / `32768` /
  `131072` / `Other`. Then `set-role <role> --ctx <chosen>`.
- **Clear context pin (auto)** — `set-role <role> --ctx auto`.
- **Back**.

### Subflow B — Service mode

AskUserQuestion `always-on` / `smart-start`. Then
`set-service-mode <mode>`. Surface the CLI's `follow_up` string
verbatim (typically: "Run `python install.py` to install/uninstall
the systemd unit, then restart `claude-hooks-daemon`."). This is
NOT something the skill does automatically — unit changes need
user consent.

If smart-start, offer idle timeout: `5min` / `30min (default)` /
`2h` / `Other`. Then `set-idle-timeout <seconds>`.

### Subflow C — Effort tier

Two-round dialog (AskUserQuestion caps at 4 options, 7 tiers
exist). First: family (`base` single-model | `x` multi-model).
Then: pick within family. Show the (N) budget suffix in labels.

- base: `low (1)` / `medium (3)` / `high (5)` / `max (25)`
- x:    `xmedium (3)` / `xhigh (5)` / `xmax (25)` / `back to base`

Then `set-effort <tier>`. When picking an x-tier with empty
researcher `extra_models`, surface:
> Note: x-tiers fan out the researcher across `extra_models`,
> but your researcher has none configured. Behaves like the
> corresponding base tier until you add extras via Subflow A.

Don't block the change.

### Subflow E — Coder per-language routing (task #111)

When the user picks **"4. Coder routing"** from the top-level menu:

1. **Show current state.** Run
   ```
   claude-consultants config coder list --cwd "$(pwd)"
   ```
   Render a compact view of `coder.default_route` + each
   `routes_by_language` entry. Mention if `coder` is disabled (the
   routing is configured but the role itself won't fire until
   enabled via Subflow A → `set-role coder --enabled true`).

2. **Top-level routing question.** AskUserQuestion:
   - **Edit global default route** — change the model used when a
     language has no per-language entry.
   - **Edit a per-language entry** — pick one of the existing
     entries to change its primary/fallback.
   - **Add a new language entry** — for a language not currently
     in the map (e.g. `typescript`, `java`).
   - **Remove a per-language entry** — language falls back to the
     global default after removal.

3. **For "Edit global default" / "Edit a per-language entry":**
   - Sub-question: "Change primary, fallback, or both?"
   - For each chosen field: AskUserQuestion with the candidate
     model list. Source the list via
     `claude-consultants config list-models` and rank by:
     (a) the user's existing per-role models, (b) the bench
     winners for this language (when known), (c) recent models
     by `modified_at`. Cap at 3 + `Other (custom)`.
   - Forward to:
     ```
     claude-consultants config coder set-default --primary <m> [--fallback <m>]
     claude-consultants config coder set <lang> --primary <m> [--fallback <m>]
     ```
     Pass `--fallback ""` (empty string) when the user wants to
     clear the failover on an existing entry — the CLI treats
     `None` as "keep current" and `""` as "explicit clear".

4. **For "Add a new language entry":**
   - AskUserQuestion: which language? Offer the keys from
     `LANGUAGE_BY_EXTENSION.values()` minus existing entries:
     `c`, `cpp`, `csharp`, `go`, `python`, `rust`, `typescript`,
     `javascript`, `java`, `kotlin`, `swift`, `ruby`, `php`,
     `shell`. Cap at 3 + `Other (custom slug)`.
   - Then primary/fallback as in step 3.
   - Forward to `claude-consultants config coder set <lang>
     --primary <m> --fallback <m>`.

5. **For "Remove a per-language entry":**
   - AskUserQuestion: which entry? List existing keys + `Back`.
   - Confirm with the user before sending (this is a destructive
     change to the routing) — show what the language will fall
     back to (the global default's primary+fallback). The
     operation is idempotent (no error on repeat), but the
     confirmation is for the human, not the API.
   - Forward to `claude-consultants config coder unset <lang>`.

6. **Loop back to step 1** after each successful change.

**Failover semantics primer (mention once, on the first change):**

> The coder's failover chain is `primary → fallback → tombstone`.
> On **any** primary failure (raised exception, zero files written,
> empty final message) the lane retries the same task with the
> fallback model. After both fail the lane tombstones with both
> model names recorded. The global default is **not** a third
> retry — it only fills in when a language has no per-language
> entry.

### Subflow F — Followup limit (review loop)

When the user picks **"5. Followup limit"**:

1. Show the current values from `config show` (`max_followups`,
   `allow_extra`).
2. AskUserQuestion which to change: **Max followups** / **Allow-extra
   grant** / **Back**.
   - **Max followups** — the cap on auto-issued followups before the
     skill must stop and ask you (default 4; 0 means the first followup
     already needs approval). Ask for the integer, then forward:
     `claude-consultants config set-max-followups <N>`.
   - **Allow-extra grant** — how many extra followups each over-cap
     approval adds for a consultancy (default 1, must be ≥ 1). Ask for
     the integer, then forward:
     `claude-consultants config set-allow-extra <N>`.
3. Loop back to step 1 after a successful change.

### Subflow G — Adversary / verify budget

When the user picks **"6. Adversary / verify budget"**. These four
knobs all default OFF / bounded. They fire **whenever enabled**, on
any effort tier — there is no silent effort gate (a pause the operator
turned on should pause). Because the checkpoint can add up to its
timeout in latency and the council answers strongest with the fanned
critics behind it, surface this guidance once when the current effort
is low/medium:

> Note: the adversary checkpoint pauses for an external red-team brief
> (up to its timeout) and the post-synthesis adversary role adds a
> refutation pass. Both fire on any tier once enabled, but they pay
> off most at high/max (and xhigh/xmax) on high-stakes questions where
> a wrong-but-plausible answer is costly.

1. Show current values from `config show`: `roles.adversary.enabled`,
   `adversary_strictness`, `adversary_checkpoint` (+ its timeout), and
   `verify_budget`.
2. AskUserQuestion which to change: **Adversary role** / **Adversary
   checkpoint** / **Verify budget** / **Back** (strictness is reached
   under "Adversary role").
   - **Adversary role** — a sub-question:
     - *Toggle on/off* — forward
       `claude-consultants config set-role adversary --enabled <true|false>`.
       The adversary is a post-synthesis refuter: it runs once after
       the synthesizer and annotates the answer with a `REFUTATION:`
       block (or `REFUTATION: none`). It never asks for more research.
     - *Change strictness* — AskUserQuestion `soft` / `normal` /
       `strict`, then
       `claude-consultants config set-adversary-strictness <level>`.
       `soft` flags only clear hallucinations; `strict` challenges
       every unsupported claim.
     - *Change model* — same as Subflow A's model picker, forwarding
       `set-role adversary --model <chosen>`.
   - **Adversary checkpoint** — the engine-initiated pause. When ON,
     the council pauses just before synthesis, emits an
     `awaiting_adversary` SSE event, and waits for the assistant to
     inject a bespoke adversarial brief before resuming — auto-proceeds
     after the timeout if no answer arrives (covers a lost SSE / missed
     poll). AskUserQuestion `on` / `off`; if `on`, offer the timeout
     (`5min` / `10min (default)` / `Other`). Forward:
     `claude-consultants config set-adversary-checkpoint <on|off> [--timeout <seconds>]`.
     See the **Dynamic adversary** subsection of the review loop for
     how to react to `awaiting_adversary`.
   - **Verify budget** — caps the skeptic panel the Workflow driver
     (and the review loop) runs against surviving claims.
     AskUserQuestion `minimal` (2 claims / 1 round) / `bounded`
     (3 claims, default) / `generous` (5 claims, up to the followup
     cap). Forward:
     `claude-consultants config set-verify-budget <tier>`.
3. Loop back to step 1 after a successful change.

### Subflow H — Config scope (user-global vs per-project)

A per-project file (`<cwd>/.claude-hooks/consultants.toml`) is the
active config when its `override_user_global` directive is on (default
on for a new file). When active, every `config set-*` and this dialog
write that file by default; `--user` forces user-global. Read
`active_config` from `config show` for the current state, then
AskUserQuestion:

- **Activate per-project** — make `.claude-hooks/consultants.toml` the
  active config, creating it as a full snapshot if absent. Forward:
  `claude-consultants config set-override-user-global on --cwd "$(pwd)"`.
- **Deactivate per-project** — engine + every config command fall back
  to user-global; the file is preserved (flip back on to restore it).
  Forward:
  `claude-consultants config set-override-user-global off --cwd "$(pwd)"`.
- **Show user-global** — re-render the status block from
  `config show --user` so the user can compare scopes.
- **Back**.

Relay once: a per-project file is a **full snapshot**, so the first
project-scoped write captures the entire effective config —
user-global stops "showing through" for those keys until the flag is
turned off again.

3. Loop back to step 1 after a successful change.

### After every change

Re-run `config show` and re-render the status block. Return to
step 2's menu unless the user picked Done.

### Per-project overrides — first-class scope

Per-project config is a first-class concept, not an afterthought. The
config layers defaults < user-global < per-project; a per-project file
(`<cwd>/.claude-hooks/consultants.toml`) shadows user-global key-by-key
and the engine reads it on every consult **when its
`override_user_global` directive is on** (a per-project-file-only flag,
default on for a new file).

When the active scope is per-project, `config show` and every `set-*`
already act on that file by default — you do **not** need `--project`.
Use the explicit flags only to override: `--user` acts on user-global;
`--project` / `--cwd "$(pwd)"` forces the per-project file (e.g. to
create one). Flip the directive with
`config set-override-user-global on|off --cwd "$(pwd)"` (Subflow H).
Always surface which scope a change landed in — read
`active_config.scope` from the JSON and relay the CLI's stderr notice.

---

### Subflow I — Tool surface

Read the `tools` block from `config show`:

```
Tool surface
  registry:      on
  git history:   off
  all roles:     on    (planner/critic/meta_critic/synth/adversary)
  default rung:  auto
  approval wait: 600s  (ask_human only; timeout denies)
  pinned:        (none)
```

AskUserQuestion the sub-action:

- **Toggle git history tools** →
  `config set-tools --git true|false --cwd "$(pwd)"`.

  Say what it buys, because the name undersells it:
  > Adds `git_history` — "when did this regress, and why?" — plus
  > `git_log` / `git_blame` / `git_diff` / `git_show`. All read-only.
  > `git_history` wraps `git log -L`, so it answers from the history
  > of *specific lines or a function*, not the whole file.

  It ships **off**: the tools are read-only and safe, but they add
  five schemas to every prompt on every lane, which is a
  default-behaviour change. Turning it on is the operator's call.

- **Uniform role access** →
  `config set-tools --all-roles true|false --cwd "$(pwd)"`.

  > planner, critic, meta_critic, synthesizer and adversary get the
  > same tool surface the researcher has — so a critic can `read_file`
  > a citation instead of taking the researcher's word for it, and
  > reports a `CORRECTIONS:` block when the two disagree.

  It ships **on** since 2026-08-01. It was gated off pending a
  measurement, on the theory that it changed cost and not correctness;
  the measurement said the opposite on both counts
  (`benchmarks/consultants/results/2026-08-01/`):

  - **Cheaper.** −30% prompt / −13% completion at `effort=high`, with
    non-overlapping ranges across three paired trials. The saving comes
    from the *planner*: it grounds the plan in the code, and the
    researcher then converges in ~2 fewer iterations. A tool loop
    resends its history every iteration, so the iterations removed are
    the most expensive ones.
  - **More accurate.** Against research with planted false claims, the
    tooled critic caught 100% vs 0% untooled, with no false positives.

  If an operator asks to turn it **off**, that is supported and the
  path is tested — but say what they give up, and that the cost
  argument for turning it off did not survive measurement.

- **Pin a tool's permission** → AskUserQuestion the tool, then the
  rung, then
  `config set-tools --permission <tool> <level> --cwd "$(pwd)"`.

  Explain the rungs in cost terms, not just safety terms:

  | rung | who approves | when to use it |
  |---|---|---|
  | `auto` | nobody — it just runs | reads and non-destructive work. Routing these through an approver burns tokens for nothing |
  | `ask_assistant` | Claude, which auto-approves and may escalate | writes, builds |
  | `ask_human` | you, and only you | anything that spends money |
  | `deny` | — | refused outright |

- **Change the default rung** →
  `config set-tools --default-level <level> --cwd "$(pwd)"`. This is
  the rung for a tool nothing else names. Warn before setting it above
  `auto`: it applies to *every* tool, including `grep` and
  `read_file`, so a council would pay an approval round-trip per read.

- **Change the approval deadline** →
  `config set-tools --approval-timeout <seconds> --cwd "$(pwd)"`
  (default 600). How long a parked `ask_human` call waits before it is
  **denied**. Only `ask_human` parks, so this is the spend gate.
  Denying on timeout is deliberate: absence of an approver never
  authorizes spend.

- **Clear pins** → `config set-tools --clear-permissions --cwd "$(pwd)"`.

- **Disable the registry** → `config set-tools --enabled false`. Falls
  back to the fixed six built-in tools. Offer this only as a
  troubleshooting step.

#### When a tool asks for approval

Since 2026-08-02 the two `ask_*` rungs behave differently, and the
difference is the whole design:

- **`ask_assistant` never stalls.** It auto-approves, records a
  `tool_approval_auto` event, and the lane continues. You see that the
  call happened and can tighten the rung afterwards; you are not asked
  to bless each one. Making it stall would burn a round-trip per write
  per lane for a verdict that is yes by construction.
- **`ask_human` parks the lane** and is the only rung that can. The
  lane blocks inside its tool executor — its N×M x-tier siblings keep
  running — until someone answers or the deadline passes.

**Detecting a parked call.** Two signals, same as the adversary
checkpoint:

- `status <sid>` grows a `pending_tool_approvals` array **only while a
  call is parked**. Each entry carries `request_id`, `tool`,
  `arguments`, `cwd`, `reason` and `deadline_ts`.
- The events stream emits `awaiting_tool_approval`, which
  `--milestones` always keeps.

Pattern B in [Wait for the run](#4-wait-for-the-run--the-only-three-patterns)
already exits on either signal; extend its check to
`pending_tool_approvals` when a run can reach an `ask_human` rung.

**Answering.**

```
claude-consultants tool-ack <sid> --allow
claude-consultants tool-ack <sid> --deny --reason "not worth the spend"
claude-consultants tool-ack <sid> --allow --request-id tap-3
```

`--allow` / `--deny` is required and mutually exclusive — there is no
default verdict, because guessing either way is the failure the channel
exists to prevent. Omit `--request-id` to answer the oldest pending
request, which is the common case of exactly one parked call.

**Answer the class, not the call.** A council is wide: the first live
run parked four `read_file` requests in 90 seconds, three of them the
same file from three x-tier researcher lanes. One-at-a-time answers do
not keep up, and what does not get answered is **denied** at the
deadline — so a queue you can't keep up with is a run that quietly
degrades. Two flags fix that, and you should reach for them by default
rather than after the third prompt:

```
claude-consultants tool-ack <sid> --allow --all-of-tool
claude-consultants tool-ack <sid> --allow --all-matching 'src/**'
claude-consultants tool-ack <sid> --deny --all-matching '*.env'
```

The rule is **per council** — every role and every x-tier lane inherits
it — and installing one immediately releases the parked requests it
matches, so the siblings already waiting don't sit out the deadline for
a decision that has been made. `status` shows the active rules under
`tool_approval_grants`. A later rule overrides an earlier one, so a
blanket allow can be narrowed by a specific deny without restarting.

When you put the request to the user, propose the scope with it rather
than asking four times:

> The council wants to read `src/config.py` (3 lanes are waiting on
> it). Allow **all reads under `src/**`** for this council, allow just
> this file, or deny?

Identical concurrent calls are already coalesced into one request —
`waiters: 3` on the entry tells you how many lanes one answer releases.

**Do not answer an `ask_human` yourself.** The rung exists because a
person decides; `ask_assistant` is the rung that delegates to you. Show
the user the tool, its arguments, the root it would run in, the reason
it tripped the rung, and how long until it auto-denies — then ask.

**But most calls should never reach a human.** `ask_human` is for spend
and irreversibility — renting a GPU, pushing a branch, calling a paid
API. Reads, greps, globs and sandboxed writes belong on `auto` or
`ask_assistant`, which give you the audit trail without the stall. If a
run is producing a steady stream of approval requests, the rung is
wrong, not the workflow: say so, and offer

```
claude-consultants config set-tools --permission read_file ask_assistant --cwd "$(pwd)"
```

rather than shepherding the queue for the rest of the council.

**If nobody answers**, the call is denied at the deadline and the lane
gets `error: tool 'X' was not approved`. The council finishes degraded
with the denial on the record rather than spending money nobody
approved. Say so when you relay the answer: a result produced without a
tool the council wanted is a weaker result, and the user should know
which one it was.

**If a tool is refused mid-council** you will see an
`error: tool 'X' was not approved` result in the transcript rather
than a crash — that is by design, so the model reroutes. If it happens
repeatedly, the pin is probably too strict for the question being
asked; say so rather than letting the council grind.

---

---

## Coder role (opt-in, off by default)

The **coder** role is a code-writing specialist that fires only
when (a) ``cfg.roles.coder.enabled = true`` AND (b) the planner
declares the question requires writing new code or files (it emits
``"requires_code_generation": true`` + a ``coder_tasks`` list).
For audits / analyses / decisions the role stays inert — same plan
shape as v1.

**What it does.** One ``coder`` lane per declared task; each lane
gets the plan + researcher findings + the specific task, runs a
short agent loop with a single ``write_file`` tool, and writes
files inside a per-session sandbox at
``<cwd>/.claude-hooks/consultants/<sid>/coder-out/``. The
synthesizer references the on-disk paths in its answer.

**Sandbox caps.** 50 KB per file, 1 MB total per lane, 16 files
max per lane (configurable via the ``[coder_limits]`` TOML block).
Exceeding any cap returns an inline error to the model so the next
iteration self-corrects. The path guard rejects absolute paths and
traversal — ``write_file("/etc/passwd", ...)`` and
``write_file("../escape", ...)`` both error before touching disk.

**When to suggest enabling it.** A user asking "Write me X" or
"Add a CLI flag for Y" benefits from the role. A user asking
"Explain how X works" / "Why is Y broken" / "Audit Z" does NOT —
keep the role off. The planner's gate is the safety: with the role
on but the question analytical, the planner emits no coder_tasks
and the graph routes around the lane (zero coder cost). With the
role off, the gate isn't even offered.

**Enable / disable.**

```
claude-consultants config set-role coder enabled=true
```

(or interactively via ``/consultants config`` → Edit a role →
coder → Toggle enabled.) The pre-M11b default model is the
project-global ``DEFAULT_MODEL``; users may pick a different model
per role via the same dialog.

---

## Autonomous control (in-flight consultation)

The v2 engine exposes seven HTTP routes + matching CLI subcommands.
All seven operate on the session's ``sid`` and return JSON. The CLI
calls are thin wrappers around the HTTP endpoints — choose whichever
fits the surrounding context. The verbs split into two buckets:

- **Monitor channels** (free to use while a consultation is running):
  ``events`` (live SSE stream — preferred for real-time visibility,
  detailed below), ``status`` (coarse poll, covered in the ask flow),
  ``state`` (one-shot structured snapshot, below).
- **Intervention verbs** (use only when the user's current turn
  implies a mid-flight nudge): ``inject`` / ``control`` / ``pause`` /
  ``resume`` / ``cancel``. Defaults to silent — do NOT call these to
  "check in" on a running session; that's what the monitor channels
  are for.

### state — peek live state

```
claude-consultants state <sid>
```

Returns the current ``StateSnapshot``: ``status``, ``current_node``,
``research[]``, ``critique``, ``partial_synthesis``,
``runtime_control``, ``interrupt_state``. Use to answer "what's the
council doing right now?" without scraping the SSE stream. Returns
410 when the session has been closed (idle reap), 404 when the
sid is unknown.

### inject — add context mid-flight

```
claude-consultants inject <sid> --role researcher -m "Also consider GDPR."
claude-consultants inject <sid> --role any -f /tmp/extra-notes.md
```

Append a ``Doc`` to ``additional_context``. The next node entry
for the target role surfaces it in the prompt. Use when the user
realizes the council needs a fact they forgot to seed — e.g. "tell
the researcher to also check the staging branch" or "remind the
synthesizer to call out latency cost". ``--role any`` is the safe
default if you don't know which role should see it.

### control — mutate runtime_control mid-flight

```
claude-consultants control <sid> --time +30m
claude-consultants control <sid> --max-rounds 5 --confidence 0.7
claude-consultants control <sid> --strictness adversarial \
  --adversarial-focus "attack the claim that the cache is write-through"
claude-consultants control <sid> --disable critic
```

Each flag merges into ``runtime_control`` via the shallow-merge
reducer; unspecified fields stay put. ``--time +30m`` is parsed as
a relative bump on top of ``deadline_ts`` (so "30 minutes from
now"). ``--enable`` and ``--disable`` mutate ``enabled_roles`` via
a snapshot-then-subtract (the CLI reads ``GET /state`` first to
build the diff). Use to grow / shrink the budget after seeing the
plan or first researcher round.

``--strictness`` is the **critic dial** (M4), not an adversary-role
toggle: it threads ``lax`` / ``normal`` / ``strict`` / ``adversarial``
into the next critic + meta-critic prompt, so the change actually
re-shapes the next critique. ``adversarial`` is the live-only level —
it makes the critic actively hunt to *break* the evidence — and pairs
with ``--adversarial-focus "<brief>"`` to point that hunt at a specific
claim (see the **Dynamic adversary** subsection). The static
``adversary_strictness`` config (Subflow G) seeds the boot-time dial
(soft→lax / normal→normal / strict→strict); ``adversarial`` is reachable
only here, mid-flight. Pass ``--adversarial-focus ""`` to clear a brief
you set earlier.

### pause + resume — HITL approval flow

```
claude-consultants pause <sid> --reason "let me read the draft"
# ... user reads /state, optionally injects ...
claude-consultants resume <sid>
claude-consultants resume <sid> --value '{"approve": true}'
```

``pause`` flips ``runtime_control.pause_requested`` so the next
node entry calls ``interrupt()``. ``resume`` clears the interrupt
and schedules a ``Command(resume=value)`` re-invoke. ``--value``
is forwarded as the resume payload (JSON-decoded if parsable,
otherwise a literal string). A dynamic interrupt set by the
synthesizer's low-confidence policy returns the same way.

### cancel — abort with cleanup

```
claude-consultants cancel <sid>
claude-consultants cancel <sid> --discard-partial
```

Cooperative drain. ``--discard-partial`` also deletes the
checkpointer file. Use when the user has changed their mind about
the question. Idempotent on completed sessions (200, no-op).

### events — live monitor (SSE stream)

This is the **engine's real-time monitor channel** — the canonical way
to watch a running consultation. CLI wrapper for the engine's
``GET /v1/consult/{sid}/events`` endpoint:

```
claude-consultants events <sid>
claude-consultants events <sid> --since 47
```

Each event is one SSE block on stdout:

```
id: 12
event: node_enter
data: {"kind":"node_enter","role":"researcher","round":1,"lane_idx":0}
```

Event types: ``node_enter`` / ``node_exit`` (role transitions),
``llm_call`` (each model call, with ``duration_ms``), ``tool_call``
(each tool invocation, with ``duration_ms``), heartbeat (emitted every
15 s through quiet rounds so the connection stays alive), and finally
**``complete``** at session end with
``{"sid": "...", "status": "completed|failed|...", "final_answer_present":
true|false}`` — that's the canonical "council is done" signal, so the
consumer is notified without having to ``/state``-poll.

``--since <event_id>`` (sent as ``Last-Event-ID``) replays the stream
from a known point — useful when reconnecting after a network blip or
across a compaction boundary.

**Pass ``--milestones`` whenever a human (or you) is the consumer.**
Unfiltered, the CLI prints every raw SSE record including every
``llm_call`` and ``tool_call`` — hundreds per council, each a full
payload — which is unreadable as a monitor and buries the
``awaiting_adversary`` event that needs an answer. ``--milestones``
keeps state changes only and prints one compact line each;
``--kinds a,b`` narrows further. The raw form is for machine
consumers: hit the HTTP endpoint directly (``curl -N
$endpoint/v1/consult/<sid>/events``) when you want to parse it
yourself.

### When to autonomously call these

Sparingly. The bar is: **the user's current turn implies a
mid-flight intervention.** Examples:

- User says "the council is taking forever — give it 30 more min"
  → ``control --time +30m``.
- User says "wait, also tell the researcher to check the staging
  branch" → ``inject --role researcher -m "..."``.
- User says "cancel that, I want to ask something different"
  → ``cancel --discard-partial`` then start the new ask.
- User says "show me what the council has so far" → ``state``;
  pretty-print the partial_synthesis + research[].

Routine progress monitoring is the monitor channels' job (``events``
stream from the ask flow above, or ``status`` poll, or ``state``
snapshot) — not these intervention verbs.

---

## Driving the council from a Workflow

The whole engagement — `ask → review → verify → accept|followup` — is a
deterministic loop, which makes it a natural fit for a Claude Code
**Workflow** when the user has opted into multi-agent orchestration (the
"workflow" keyword, ultracode, or an explicit "fan this out" ask). A
committed, ready-to-run script lives at
`.claude/workflows/consult-with-adversarial-review.mjs`; invoke it with
the Workflow tool by name:

```
Workflow(name="consult-with-adversarial-review",
         args={ question: "<the question>",
                cwd: "<absolute project root>",
                effort: "high",            // optional
                verifyBudget: "bounded",   // minimal|bounded|generous
                maxRounds: 4 })            // optional client-side cap
```

It pipelines: a **consult** agent runs `claude-consultants consult …
--wait` and returns the answer; a **review** agent triages it for wrong
assumptions + gaps; a **skeptic panel** (`parallel`, one agent per
load-bearing claim) tries to *refute* each claim against the real repo;
then the script either **accepts** (reviewer satisfied AND nothing
survived refutation) or calls `composeChallenge()` — the surviving
refutations + open concerns become a single focused **follow-up** — and
loops. `composeChallenge()` is where Q1 (a bespoke adversarial brief)
meets Q2 (the programmatic driver).

### The consultancy.status branch table

The driver (and any hand-rolled loop) keys on the same engine-owned
status carried in every `status` / `result` / `follow-up` JSON:

| `consultancy.status` | meaning | driver action |
|----------------------|---------|---------------|
| `in_progress` | council still working / mid-chain | keep polling (or `--wait`) |
| `ready_to_review` | an answer is on the table | run the review + skeptic panel |
| `accepted` | terminal — you (or a prior run) accepted | stop; present the answer |
| `awaiting_approval` | followup cap hit without an override | stop the auto-loop; surface to the user |

### Four mandatory disciplines

These are baked into the committed script; preserve them in any variant:

1. **Thread `--cwd` on every call.** A Workflow agent starts in its own
   cwd; without `--cwd "<root>"` the engine resolves the wrong project
   (or none). Every prompt in the script restates this.
2. **Detect the cap on JSON `ok == false`, never `$?`.** A followup
   refusal is an HTTP 200 with `{"ok": false, "reason":
   "followup_limit_reached"}` — the shell exit code is 0. Branch on the
   parsed JSON, then stop and ask the user (don't auto-retry past the
   cap).
3. **Gate on a complete answer before trusting `consultancy.status`.**
   `--wait` only prints the result once the per-run `status` reaches
   `completed`, so the wait *is* the gate; a hand-rolled poll must check
   per-run `status == "completed"` before reading `consultancy.status`.
4. **Cap the skeptic panel by `verify_budget`.** `minimal` = 2 claims /
   1 round, `bounded` = 3 (default), `generous` = 5 up to the followup
   cap. The script maps the tier to the panel size; the N-council
   breadth variant is worth gating to high/max only.

When the user has NOT opted into orchestration, don't reach for the
Workflow — run the same loop inline via the **Review loop** section
above. The Workflow is the same recipe, parallelized.

---

## Skill-eval — pick the right model for a role

When the user asks "is X a good model for the coder role?" or
"should I switch from kimi to qwen3-next for code generation?",
the answer comes from the **Consultancy Skill-Eval Protocol** —
the canonical evaluation procedure for any candidate consultant
model. The full methodology lives at
[`docs/consultants-skill-eval-protocol.md`](../../docs/consultants-skill-eval-protocol.md);
the running ledger of every score lives at
[`docs/consultants-skill-eval-baselines.md`](../../docs/consultants-skill-eval-baselines.md).

The protocol runs three sub-suites (one is shipped today, two
are scheduled):

- **coder** — 8 HumanEval-style questions × candidate models;
  rubric is `pass_rate ≥ 70% AND avg_quality ≥ 3.5`.
- **stall** (M11a, not yet shipped) — inter-token cadence on hard
  questions; informs M3 stall thresholds.
- **tool_executor** (M11c, not yet shipped) — multi-tool research
  tasks; gates the M6 default-on bit.

**Invoke via CLI** (preferred — wraps the bench scripts):

```
claude-consultants skill-eval coder --dry-run \
    --models kimi-k2.6:cloud,qwen3-next:cloud,glm-5.1:cloud,gemma4:31b-cloud
```

Dry-run validates the harness end-to-end without cloud spend
(~10 s). For a real evaluation:

```
claude-consultants skill-eval coder --live --accept-cost \
    --models <comma-separated> \
    --ollama-base http://192.168.178.2:11433 \
    --judge-model kimi-k2.6:cloud
```

Then render the report:

```
python benchmarks/consultants/analyze.py \
    benchmarks/consultants/results/<date>/coder/trials.jsonl
```

The report.md applies the rubric and recommends a default. If a
new model qualifies, **append** its score to the baselines ledger
— that's how we accumulate evidence over time.

**When to suggest running the eval:**

- The user is considering a new model for a consultant role.
- The user reports the council's code-generation output regressed
  ("it used to write better Python last week") — re-run the eval
  to see if the proxy upstream shifted.
- A new model just landed on the cloud upstream and you want to
  know whether to recommend it.

**When NOT to suggest:**

- For a single ambiguous result. Skill-eval works on an 8-question
  denominator; one bad answer to the user's actual question isn't
  enough signal.
- For non-consultant decisions (general-purpose model picks). The
  protocol is scoped to the consultant roles.

---

## Failure handling (all verbs)

- **CLI returns `{"ok": false, ...}`** → surface the error verbatim.
- **`engine_python not found` / connection refused on `:38096`** →
  the consultants service isn't running. Suggest
  `systemctl --user status claude-hooks-consultants` (always-on
  mode) or `python install.py` if the user hasn't installed yet.
- **Role-specific failure** (e.g. `researcher failed: ...`) →
  surface role + error; suggest `/consultants config` to switch the
  role's model.
- **Cloud upstream flap** → council is particularly retry-prone
  (4 roles × multiple turns); the resilience layer in the proxy
  retries automatically, but exhausted budgets surface here.
  Surface plainly and offer `/consultants followup <failed_sid>`
  to chain off the failed session (researcher + critic work is
  usually salvageable).

## Reference

- **Deep config schema**, role-by-role behavior, effort-tier
  semantics, per-project vs user-global resolution: see
  [`docs/consultants.md`](../../docs/consultants.md).
- **Live engine status & metrics**:
  `claude-consultants config show` (CLI summary) or
  `curl http://127.0.0.1:38096/health` (raw).
- **Past sessions on disk**: `.claude-hooks/consultants/<sid>/`.

## /consultants vs /get-advice

- `/get-advice` is a single-shot conversation with one model. Use
  it when one good model + the project tools can answer the
  question in a single back-and-forth.
- `/consultants` is a multi-agent pipeline. Reach for it when the
  question benefits from a researcher actively grounding in
  project files AND a critic challenging the evidence before the
  final answer.
