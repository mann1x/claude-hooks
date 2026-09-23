# Model sampling templates

A cloud model runs on the provider's default sampler unless the request
says otherwise. The Ollama Cloud tags carry no parameters, so before
2026-09-23 every consultants, get-advice and bench call ran glm-5.3 at
temperature 1.0, and glm-5.3 is poor at 1.0 (Cerebriline measured 0.2
broken, 1.0 poor, 0.7 good). The fix belongs in the request, not in an
Ollama Modelfile overlay (`glm-5.3-flash-tpl2` etc.): an overlay is host
state that can be re-pulled, recreated or missing on the other host, and
nothing tells the caller.

## Layers

| layer | where | who edits | survives install |
|---|---|---|---|
| shipped | `config/model-sampling.json` (tracked) | releases | replaced by each release |
| user | `model_sampling.templates` in `config/claude-hooks.json` | you | never overwritten |
| process | `CLAUDE_HOOKS_MODEL_SAMPLING` (JSON templates object) | a benchmark arm | n/a |

Each layer merges over the one above **field by field**:

```json
"model_sampling": {
  "templates": {
    "glm-5.3*":             {"top_p": 0.95},
    "deepseek-v4.1-flash*": {"temperature": 0.7},
    "gemma4*":              null
  }
}
```

- The first entry keeps the shipped `temperature: 0.7` and adds `top_p`.
- `null` on a field drops it (the provider default is used). `null` on a
  pattern disables the template.
- Patterns are `fnmatch` globs. The one with the most literal characters
  wins, so `glm-5.3-flash*` beats `glm-5.3*`, which beats `*`.
- Keys starting with `_` are notes. An unknown field is dropped with a
  warning, never sent.
- A value the caller puts in `options` beats any template. An explicit
  `None` in `options` means "send nothing for this field".

Fields: `temperature top_p top_k min_p typical_p repeat_penalty
repeat_last_n presence_penalty frequency_penalty seed mirostat
mirostat_tau mirostat_eta` (`claude_hooks.model_sampling.FIELDS`).

The files are re-read when their mtime changes, so an edit takes effect
on the next request without a restart.

## Only measured values ship

A shipped template records a value that was measured, with its `_why`.
An unset field is not a zero: it is the provider default, which is the
honest setting until something beats it.

## Measuring a setting

`benchmarks/consultants/judge_eval.py judge` takes `--sampling
template|none` plus repeatable `--option FIELD=VALUE`. The judge's label
(`glm-5.3-flash:cloud@repeat_penalty=1.1,temperature=0.7`) keys every
verdict, so arms never collide. For the coder role, run `coder_bench.py`
with `CLAUDE_HOOKS_MODEL_SAMPLING` set and a separate `--output-dir`.
