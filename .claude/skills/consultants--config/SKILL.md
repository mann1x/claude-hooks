---
name: consultants--config
description: Interactive configuration walk-through for the /consultants engine — toggle roles on/off, change per-role models, pin context, switch effort tier, change service mode (always-on / smart-start), set idle timeout. Use when the user invokes /consultants--config in their current turn. Drives the underlying claude-consultants config CLI; no direct file editing.
---

# /consultants--config — interactive configuration

You walk the user through configuration changes via AskUserQuestion.
Every actual change goes through `claude-consultants config <subcommand>`,
which validates inputs and atomically writes the TOML file. The
skill never opens a file in an editor.

## ⚠️ Activation guard — read first

Only execute this workflow when **all** of these are true:

1. The user's **current turn** explicitly invokes
   `/consultants--config`.
2. The skill is being called for the *first time* in this user
   request — not an echo from a `<system-reminder>` listing skills
   invoked earlier.

## Step 1 — show current state

Run:

```
claude-consultants config show --cwd "$(pwd)"
```

Parse the JSON. Render a status block to the user:

```
=== /consultants config ===
Service mode: smart-start (idle 30 min)   |   always-on
Endpoint: http://127.0.0.1:38096
Effort: xhigh (budget 5, multi-model active)
Topology: council

Roles:
  planner       ENABLED   model=kimi-k2.6:cloud         ctx=auto
  researcher    ENABLED   model=qwen3.5:cloud           ctx=auto
                  extra_models: kimi-k2.6:cloud, deepseek-v4-pro:cloud
  critic        ENABLED   model=deepseek-v4-pro:cloud   ctx=auto
                  extra_models: (none — only used at xmax)
  synthesizer   ENABLED   model=kimi-k2.6:cloud         ctx=auto
                (synthesizer cannot be disabled)
```

When `extras_active` is `true` in the JSON (i.e. the configured
effort is x-prefixed), append `, multi-model active` to the
Effort line as shown.

For each role, render `extra_models` only when:
- Researcher and critic always (with `(none)` when empty), so the
  user knows the option exists without remembering the schema.
- Planner and synthesizer NEVER — they aren't fan-outable, so
  the field is hidden to keep the block compact.

Use the actual values from the JSON. Format ctx_max as `auto` when
null and `<N>` (with explicit / pinned indicator) when set.

## Step 2 — top-level menu

Use AskUserQuestion to ask:

> What do you want to change?

Options (single-select, max 4):

1. **Edit a role** — toggle on/off, model, context length
2. **Change service mode** — always-on or smart-start
3. **Change effort tier** — low / medium / high / max, or the
   multi-model x-prefixed variants (xmedium / xhigh / xmax) when
   the user wants fan-out across `extra_models`
4. **Done** — exit without further changes

If the user picks an option, drive the matching subflow below.
Loop back to step 2 after each successful change so they can make
further edits, until they pick "Done".

## Subflow A — Edit a role

AskUserQuestion to pick the role: `planner`, `researcher`, `critic`,
`synthesizer`. Then for the selected role, AskUserQuestion the
sub-action:

- **Toggle enabled** — only offer for non-mandatory roles. Reject
  with a friendly message if the user picks this for synthesizer.
- **Change model**
- **Manage extra models** — only offer for `researcher` and
  `critic`. These are consulted at x-prefixed effort tiers
  (xmedium / xhigh / xmax) for multi-model fan-out. Hidden for
  `planner` / `synthesizer` because the engine doesn't fan those
  out.
- **Pin context length**
- **Clear context pin (set auto)**
- **Back to main menu**

### Change model

Run:

```
claude-consultants config list-models
```

Parse the JSON. Pick up to 3 most-likely candidates as
AskUserQuestion options (newest tags first, plus any cloud tags).
The 4th option is always **Other (type a custom tag)**, which falls
through to AskUserQuestion's "Other" free-text field. Then:

```
claude-consultants config set-role <role> --model <chosen>
```

### Manage extra models

Only offered for `researcher` and `critic`. These models are used
at x-prefixed effort tiers (xmedium / xhigh / xmax) — each
plan-item lane fans out to the primary plus every extra, so the
synthesizer (or meta-critic at xmax) sees diverse perspectives.

Read the current state from the role's `extra_models` field in
`config show` output. Render as a numbered list in your
introduction so the user sees what's already configured.

AskUserQuestion sub-action:

- **Add a model** — proceed to selection
- **Remove a model** — only offer when the list is non-empty;
  AskUserQuestion the entries plus a "Cancel" option
- **Clear all extras** — only offer when non-empty
- **Back**

For **Add a model**: run `claude-consultants config list-models`,
parse the JSON, filter out the current primary AND existing
extras, surface up to 3 most-likely candidates plus
**Other (type a custom tag)**. Then:

```
claude-consultants config set-role <role> --add-model <chosen>
```

For **Remove a model**:

```
claude-consultants config set-role <role> --remove-model <chosen>
```

For **Clear all extras**:

```
claude-consultants config set-role <role> --clear-extras
```

Important to surface to the user once when adding the FIRST
extra:

> Note: extras are only consulted at the x-prefixed effort tiers
> (xmedium / xhigh / xmax). At low/medium/high/max your primary
> model runs alone — the extras list is silent. To activate
> them, set the effort tier to `xhigh` for example.

### Pin context length

AskUserQuestion options: `8192`, `32768`, `131072`, `Other (custom
integer)`. Then:

```
claude-consultants config set-role <role> --ctx <chosen>
```

### Toggle enabled

```
claude-consultants config set-role <role> --enabled false
# or --enabled true to re-enable
```

If the user tries to disable synthesizer, the CLI returns
`{"ok": false, "error": "role 'synthesizer' is mandatory and cannot
be disabled"}`. Surface that and loop back to Step 2.

If the user disables planner AND researcher, the next
`claude-consultants consult` will reject with a "validation"
error — warn them at the moment of the second toggle that this
combination is invalid.

## Subflow B — Change service mode

AskUserQuestion: `always-on` (default) or `smart-start` (idle
shutdown after 30 min). On selection:

```
claude-consultants config set-service-mode <mode>
```

The CLI returns a `follow_up` string in its JSON output — surface
it verbatim. Typical content: "Run `python install.py` to
install/uninstall the systemd unit, then restart
`claude-hooks-daemon`." This is NOT something the skill does
automatically — service unit changes need user consent.

If switching to smart-start, also offer to set the idle timeout via
AskUserQuestion: `5 min`, `30 min` (default), `2 hours`, `Other`.
Then:

```
claude-consultants config set-idle-timeout <seconds>
```

## Subflow C — Change effort tier

AskUserQuestion. Two question rounds because there are 7 valid
tiers and AskUserQuestion caps at 4 options:

1. First, ask which family — base or x-prefixed (multi-model):
   - `base — single primary model only` — proceed to base tiers
   - `x — multi-model fan-out at researcher (and critic at xmax)` — proceed to x-tiers
2. Then offer the four tiers in the chosen family:
   - base: `low (1)` / `medium (3)` / `high (5)` / `max (25)`
   - x: `xmedium (3)` / `xhigh (5)` / `xmax (25)` / `back to base`

Use the (N) suffix in labels so the user sees the follow-up
budget. Then:

```
claude-consultants config set-effort <tier>
```

When the user picks an x-tier, check whether `roles.researcher.
extra_models` is empty (read from the same `config show` JSON).
If empty, surface a one-line note:

> Note: x-tiers fan out the researcher across `extra_models`, but
> your researcher has no extras configured. The consultation will
> behave like the corresponding base tier until you add extras
> via Subflow A → Manage extra models.

Don't block the change — the user might want to set the tier
first and add extras in the next iteration.

## After every change

Re-run `claude-consultants config show` and re-render the status
block so the user sees the updated state immediately. Then return to
step 2's main menu unless the user picked "Done".

## Notes

- The TOML file (`~/.claude/consultants-config.toml` user-global
  or `<project>/.claude-hooks/consultants.toml` per-project) is
  the on-disk source of truth and is safe to hand-edit. The skill
  exposes a structured editor over it so the user doesn't have to
  remember the exact key names.
- Per-project overrides: if the user wants a different model for
  one project only, mention they can use
  `claude-consultants config set-role <role> --model <m> --project
  --cwd "$(pwd)"`. The skill itself defaults to user-global edits;
  only mention the project-scope flag if the user explicitly asks
  for it.
- Don't make changes the user didn't ask for. Each
  AskUserQuestion picks one specific change; loop, don't batch.
