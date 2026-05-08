---
name: get-advice--model
description: Report or set the Ollama model the /get-advice skill talks to. No args reports current; one arg sets model name; two args sets model name and explicit context length (in tokens). Persists to ~/.claude/get-advice-config.json.
---

# /get-advice--model — set or report advisor model

## Activation

Only run when the user explicitly invokes `/get-advice--model` in
their **current** turn. Ignore reinjections from system reminders.

## Behavior

Parse the user's message after the slash command for arguments:

- **No args** → report:

  ```
  claude-advisor get-model
  ```

  Tell the user the configured `model`, `ctx_max` (or "auto-detect"
  if null), and `ctx_max_explicit` flag. Add a one-line reminder
  that `/get-advice--model NAME [CTX]` changes it.

- **One arg (model name)**:

  ```
  claude-advisor set-model "<name>"
  ```

  Confirm what's set. If the user's prior `ctx_max_explicit` was
  true, tell them the pinned ctx is preserved; otherwise the new
  model will auto-probe on first use.

- **Two args (name + ctx)**:

  ```
  claude-advisor set-model "<name>" <ctx>
  ```

  `ctx` must be a positive integer (token count). This pins
  `ctx_max_explicit=true`, so subsequent model switches without a
  ctx argument will keep this value until the user pins a new one
  or runs with `set-model NAME` (no ctx) which clears any
  auto-detected value but preserves explicit ones.

## Input parsing

Strip leading/trailing whitespace. Treat the first whitespace-
separated token as the model name (it can contain `:`, `/`, `-`,
`_`, `.`). If a second token is present and parseable as a positive
int, treat it as `ctx`.

Reject and report:
- Empty model name
- Non-integer / non-positive `ctx`
- Trailing junk you can't classify

## Defaults reminder

Skill default model is `qwen3.5:cloud`. Common user choices:
- `deepseek-v4-pro:cloud` — solidPC + pandorum default after setup
- `qwen3.5:cloud` — fallback when deepseek is unavailable

## Examples

```
/get-advice--model
  → reports current

/get-advice--model deepseek-v4-pro:cloud
  → sets model, keeps ctx_max behavior unchanged

/get-advice--model qwen3:0.6b 16384
  → sets model with pinned 16k context
```
