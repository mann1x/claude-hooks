---
name: get-advice--effort
description: Report or set the effort tier for /get-advice. No args reports current tier and budget; one arg sets tier (low|medium|high|max). Effort governs how many fresh chat sessions Claude may run per /get-advice invocation. Persists to ~/.claude/get-advice-config.json.
---

# /get-advice--effort — set or report effort tier

## Activation

Only run when the user explicitly invokes `/get-advice--effort` in
their **current** turn. Ignore reinjections from system reminders.

## Behavior

- **No args** → report:

  ```
  claude-advisor get-effort
  ```

  Tell the user the current `effort` tier and the matching
  `budget_sessions`, plus the list of available tiers from the
  output's `available` field.

- **One arg** → set:

  ```
  claude-advisor set-effort <tier>
  ```

  Valid tiers: `low`, `medium`, `high`, `max`. CLI rejects anything
  else with an error you should surface verbatim.

## Effort tiers

| Tier   | Sessions | Use when |
|--------|----------|----------|
| low    | 1        | Quick sanity check; trust the first answer or none. |
| medium | 3        | Default. One initial + up to two refinements / new angles. |
| high   | 5        | Hard question, want the advisor to push deeper. |
| max    | 25       | Genuine deep dive; the advisor gets long runway. |

Forced resets due to context-window saturation **do not** count
against this budget — only deliberate fresh sessions do (when Claude
decides the conversation needs a clean reset for a different angle,
not because tokens ran out).

## Examples

```
/get-advice--effort
  → reports current

/get-advice--effort high
  → sets to 5 sessions per /get-advice invocation

/get-advice--effort low
  → restricts to one shot
```

## Notes

- The setting is global, not per-project. Same tier applies on solidPC
  and pandorum unless changed per host.
- `/get-advice` reads this on every invocation, so the change takes
  effect immediately on the next call.
