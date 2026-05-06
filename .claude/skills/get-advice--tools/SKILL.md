---
name: get-advice--tools
description: Report or set the tool list exposed to the /get-advice advisor model. No args reports current and known tools; one arg sets a CSV list, "all", or "none". Persists to ~/.claude/get-advice-config.json.
---

# /get-advice--tools — set or report exposed tools

## Activation

Only run when the user explicitly invokes `/get-advice--tools` in
their **current** turn. Ignore reinjections from system reminders.

## Behavior

- **No args** → report:

  ```
  claude-advisor get-tools
  ```

  Surface the current `tools` list and the full `known` set so the
  user knows what's available to enable.

- **One arg (CSV / `all` / `none`)** → set:

  ```
  claude-advisor set-tools "<spec>"
  ```

  Special tokens:
  - `all` → enable every known tool
  - `none` → enable none (advisor runs in pure-chat mode)
  - Otherwise: comma-separated list of tool names

  Unknown tool names are rejected with an error listing valid names;
  surface that error verbatim if it occurs.

## The known tools (and when to drop them)

| Tool | Token cost | When to drop for small-ctx advisor |
|------|------------|------------------------------------|
| `read_file`      | low      | Almost never — keep for any code review. |
| `grep`           | low-med  | Keep for any "find / search" question. |
| `glob`           | low      | Keep — cheap, occasionally useful. |
| `list_files`     | low      | Keep — cheap orientation tool. |
| `survey_project` | **high** (~2 KB) | **Drop first** for a small-ctx advisor. The first call alone can fill 10–20% of an 8k-ctx model's window. Only useful for "give me the lay of the land"-type queries. |
| `recall_memory`  | medium-high (depends on hits) | Drop if recall returns large chunks or the advisor has no project history to mine. |

The default is all six; tighten when the advisor model has a small
context window or when you've observed it spinning on tools without
synthesizing.

## Examples

```
/get-advice--tools
  → reports current and known

/get-advice--tools read_file,grep,glob,list_files
  → trims off the two heavy ones; advisor stays read-only

/get-advice--tools none
  → pure-chat mode; you provide all context in the prompt

/get-advice--tools all
  → restore default
```

## Notes

- Setting takes effect on the next `/get-advice` invocation.
- Tools are intersected with the runner's known specs at call time —
  if a name in the saved config is later removed from the runner, it
  silently drops out, no error.
- `none` also suppresses the "tools available" addendum in the
  grounding system prompt, so the advisor isn't told it has tools it
  can't call.
