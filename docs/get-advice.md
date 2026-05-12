# `/get-advice` — LLM-to-LLM advisor

> **Status:** v1.1.0 · ships in the core install · opt-in skill
> registration via `python install.py`

`/get-advice` runs a multi-turn conversation between Claude Code
(orchestrator) and a configured **advisor** model on a local
Ollama backend. The advisor's job is one focused second opinion —
validate a function, sanity-check a training recipe, review a
design choice, push back on a hypothesis. It is **not** a council,
not a critique chain, not a long-horizon investigation. For those,
reach for [`/consultants`](consultants.md) instead.

The advisor sees the project through the same six tools the
caliber-grounding-proxy uses (`read_file`, `grep`, `glob`,
`list_files`, `recall_memory`, `recall_kg`), so it grounds in your
actual code via `path:line` citations rather than the user pasting
snippets.

---

## When to use it

| Use `/get-advice` when… | Use `/consultants` when… |
|---|---|
| One model is plenty — you want a single decisive opinion | The question benefits from a specialist split (planner / researcher / critic / synthesizer) |
| The question fits in one to a few turns | The question wants iterative refinement with a critic re-routing back to the researcher |
| Wall-clock budget: seconds to a minute | Wall-clock budget: 1-15 min depending on effort tier |
| You don't need the answer to survive Claude Code restarts | You want a permanent durable transcript on disk under `.claude-hooks/consultants/<sid>/` |
| Single-shot validation, code-review, recipe sanity-check | Architecture audit, refactor risk analysis, release-notes-vs-diff cross-check, deep cross-cutting question |

`/get-advice` is the cheap path. Use it first; escalate to
`/consultants` only when you actually need the council.

---

## Prerequisites

- An **Ollama backend** reachable from the install host. The skill
  reads its endpoint from `~/.claude/get-advice-config.json` (which
  install.py seeds from your `CALIBER_GROUNDING_UPSTREAM` if
  present, else asks).
- The `claude-advisor` CLI on PATH. install.py drops it as a
  POSIX-shell wrapper at `~/.local/bin/claude-advisor` (Linux /
  macOS) or `%LOCALAPPDATA%\claude-hooks\bin\claude-advisor[.cmd]`
  (Windows, with User PATH auto-prepended). See [the install
  section in the README](../README.md#install) for details on the
  wrapper layout.
- The `/get-advice` skill in `~/.claude/skills/` — installed by
  default during `python install.py` step 6.

The skill itself runs entirely in-process inside Claude Code's
Python harness; there's no long-lived service unit, no daemon, no
extra RAM cost when idle. Ollama is the only moving part outside
the skill's call stack.

---

## The /get-advice dispatcher (v1.3+)

`/get-advice` is one skill with four verbs. The first whitespace-
separated arg selects the verb; an unrecognized first token (or no
args at all) implicitly fires the `ask` verb against the whole arg.

| Form | What it does |
|---|---|
| `/get-advice [ask] <query>` | Run an advisor conversation about `<query>`. Polls the advisor through Claude Code's orchestration loop; surfaces the bottom-line answer + recommendations. `ask` may be omitted — bare `/get-advice <query>` works. |
| `/get-advice model [name [ctx]]` | Read or write the advisor's Ollama model. With one arg: set the model, auto-probe `ctx_max` on first use. With two args: set the model AND pin a context length explicitly. With no args: report current. |
| `/get-advice effort [tier]` | Read or write the effort tier. Tier governs how many fresh advisor sessions Claude may spawn per `/get-advice` invocation when quality stalls. `low` = 1, `medium` = 2, `high` = 4, `max` = uncapped. With no args: report. |
| `/get-advice tools [csv\|all\|none]` | Read or write the project-tool list exposed to the advisor. `all` = the full six-tool surface; `none` = no tools (advisor answers from its own training only); CSV = explicit subset (e.g. `read_file,grep`). |

All four verbs persist state to `~/.claude/get-advice-config.json`
so settings stick across Claude Code sessions.

> **v1.3 migration note:** The pre-v1.3 form had separate slash
> commands (`/get-advice--model`, `/get-advice--effort`,
> `/get-advice--tools`). Those were collapsed into the single
> dispatcher above to cut the upfront slash-command menu cost. The
> installer removes the legacy `~/.claude/skills/get-advice--*`
> dirs on first v1.3 upgrade run.

---

## Running an advice session

In a Claude Code prompt:

```
/get-advice does the LR schedule in scripts/train.yaml make sense
for an 8B model on 50k samples? happy if the answer is "yes,
within ±2× of optimal" or "no, it's wrong because <reason>".
```

What happens under the hood:

1. The skill reads model + effort + tools settings via the three
   getter sub-CLIs (`claude-advisor get-model` / `get-effort` /
   `get-tools`).
2. It builds a framing message — your question + minimum project
   context + what "good" looks like + where to look — and starts a
   session with a stable session id like
   `advice-train-yaml-2026-05-08-1410`:

   ```
   claude-advisor turn <sid> --first --message "<framing>" --cwd "$(pwd)"
   ```

3. Claude evaluates the advisor's reply each turn and decides:
   continue the session, reset (when context fills), start a fresh
   session (deliberate angle change), or wrap up.
4. On wrap-up Claude prints a one-paragraph synthesis of the
   advisor's bottom-line answer + a footer:

   ```
   model: kimi-k2.6:cloud · sessions: 2 · resets: 0 · ctx_used: 41%
   ```

The orchestration loop is in
[`.claude/skills/get-advice/SKILL.md`](../.claude/skills/get-advice/SKILL.md)
if you want to read what Claude actually does between turns.

---

## Picking a model

Cloud models (the `:cloud` suffix on Ollama tags) are the typical
choice for `/get-advice` because:

- they're large (mostly 30B+) and produce decisive single-shot
  answers
- they support tool calls cleanly
- their context windows (32k → 262k+) cover even long sessions

Local models work too — anything tools-capable in your Ollama
catalog. The trade-off is wall time per turn and shorter context
windows.

To switch:

```
/get-advice model glm-5.1:cloud          # auto-probe ctx_max on first use
/get-advice model qwen3.5:cloud 32768    # pin ctx_max=32768 explicitly
/get-advice model                        # report current
```

If a model 400's the first request because it doesn't accept the
`reasoning` field, ChatClient strips it once and retries
automatically — the per-host quirk is remembered for subsequent
calls.

> Empirical model picks live in
> [`docs/benchmarks/EVALUATION.md`](benchmarks/EVALUATION.md) —
> the evaluation suite is geared toward `/consultants` but the
> per-model summary applies to `/get-advice` too: cloud-tier
> models that perform well in the council also work well for a
> single-shot second opinion.

---

## Effort tiers

The effort tier caps how many **fresh advisor sessions** the
orchestrator may spawn per invocation. Forced context resets (when
the advisor approaches its `ctx_max`) are free — they don't count
toward the budget. So the tier governs how many distinct angles
Claude is allowed to try when the advisor is hedging, repeating, or
contradicting itself.

| Tier | Budget | Use when |
|---|---|---|
| `low` | 1 session | One-shot questions; the answer is either fine or you'll re-ask |
| `medium` | 2 sessions | Default. Allows one fresh-angle retry if the first session is unsatisfying |
| `high` | 4 sessions | Deeper validation work where the orchestrator should keep pushing |
| `max` | uncapped | Long debugging or design reviews; orchestrator stops when it's actually done |

```
/get-advice effort medium    # set
/get-advice effort           # report
```

---

## Tool surface

The advisor sees six grounding tools by default:

- `read_file(path, start_line?, end_line?)`
- `grep(pattern, path?, ...)`
- `glob(pattern)`
- `list_files(path)`
- `recall_memory(query, k?)` — pgvector recall against your
  memories collection
- `recall_kg(query, k?)` — knowledge graph search

To turn tools off entirely (advisor answers from its own training
only — useful for purely conceptual questions where you don't want
the advisor distracted by your codebase):

```
/get-advice tools none
```

To restrict to a subset:

```
/get-advice tools read_file,grep,recall_memory
```

To re-enable everything:

```
/get-advice tools all
```

The skill's framing message tells the advisor where to look (e.g.
`see scripts/train.py:120`) instead of pasting code; the advisor
then pulls the file with `read_file`. This is intentional —
pasting large blocks burns context that the advisor needs for
reasoning.

---

## Common workflows

### Validate a single function

```
/get-advice does ctd/alignment.py:142 handle the special-token
boundary case correctly? I'm worried the IdentityMapper bypass
skips the EOS check.
```

Advisor reads the file, traces the boundary handling, gives a
yes/no with reasoning. One session.

### Sanity-check a training recipe

```
/get-advice scripts/train.yaml is set up for KL distillation from
QC-14B to QC-1.5B. Is the temperature schedule reasonable? Should
I add a base-anchor KL term?
```

Advisor reads the yaml + nearby trainer code, gives a recipe-level
verdict + one concrete change if it would alter anything.

### Push back on a design choice

```
/get-advice I'm thinking of replacing the daemon's HMAC-on-TCP
with a Unix socket. Trade-offs?
```

Open-ended; orchestrator may run two sessions to get both
perspectives.

---

## Troubleshooting

### "claude-advisor: command not found"

The shim wrapper isn't on PATH for the shell Claude Code spawned.
Fix:

- **Linux / macOS**: confirm `~/.local/bin` is in `PATH`. If not,
  add `export PATH="$HOME/.local/bin:$PATH"` to `~/.bashrc` /
  `~/.zshrc` and restart Claude Code.
- **Windows**: `python install.py` should have prepended
  `%LOCALAPPDATA%\claude-hooks\bin` to your User PATH via `reg
  add`. Verify with
  `reg query HKCU\Environment /v PATH | findstr claude-hooks`.
  If missing, re-run `python install.py` — it's idempotent.

### Advisor returns empty replies

Two common causes:

1. **Cloud flap on the upstream**: ChatClient retries up to 15
   attempts with exponential backoff capped at 90 s — a worst-case
   ~15 min of cloud unavailability is absorbed silently. If the
   reply is still empty after a full retry budget, the cloud's
   genuinely down and `/get-advice` will surface that error.
2. **`ctx_max` mis-pinned**: if you pinned a context length
   smaller than the actual model context, big payloads truncate to
   nothing. Run `/get-advice model` with no args to see the
   pinned value. Run `/get-advice model <name>` (no ctx) to clear
   the pin and re-auto-probe.

### "no candidates" / advisor says it can't reach a tool

Check the advisor's tool list with `/get-advice tools`. If it's
`none` you turned tools off — set to `all` to re-enable. If a
specific tool isn't in the list (e.g. `recall_memory`), your
pgvector backend isn't configured; either configure it via
`python install.py` or set tools to a subset that doesn't include
recall.

### Advisor keeps asking clarifying questions

Sharpen the framing. The skill has a 1-page section on this
called "What good looks like" — every `/get-advice` invocation
should declare what answer shape would satisfy you ("yes/no plus
one concrete change", "ranked list of three issues", "thumbs
up/down with reasoning"). Without that, the advisor will hedge.

---

## Configuration file

`~/.claude/get-advice-config.json`. Hand-editable; the four
sub-skills atomically rewrite it. Schema (v1.1):

```json
{
  "model": "kimi-k2.6:cloud",
  "ctx_max": null,
  "ctx_max_explicit": false,
  "effort": "medium",
  "budget_sessions": 2,
  "tools": ["read_file", "grep", "glob", "list_files",
            "recall_memory", "recall_kg"],
  "endpoint": "http://192.168.178.2:11433"
}
```

`ctx_max: null` means auto-probe via Ollama `/api/show` on first
use; `ctx_max_explicit: true` means the user pinned a value and
the auto-probe is skipped.

---

## Implementation pointers

If you want to read the code:

- Skill: [`.claude/skills/get-advice/SKILL.md`](../.claude/skills/get-advice/SKILL.md) — orchestration logic
- CLI: `claude_hooks/get_advice/cli.py` — argparse surface
- ChatClient: `claude_hooks/get_advice/chat_client.py` — retry
  budget (15 attempts / ~15 min ceiling), 4xx body retry, model-
  reasoning-strip auto-recovery
- ctx probe: `claude_hooks/get_advice/ctx_probe.py` — Ollama
  `/api/show` parser
- Tool surface: shared with the council via
  `claude_hooks/caliber_proxy/tools.py`
- Tests: `tests/test_get_advice_*.py`

---

## See also

- [`docs/consultants.md`](consultants.md) — when one advisor isn't
  enough
- [`docs/benchmarks/EVALUATION.md`](benchmarks/EVALUATION.md) —
  cloud-model evaluation suite
- [`docs/caliber-proxy.md`](caliber-proxy.md) — the agent-loop
  runner that backs both `/get-advice` and `/consultants`
- [`docs/RELEASING.md`](RELEASING.md) — when to expect changes to
  this skill
