# gemma4-98e tool-use notes

Empirical results from running our caliber grounding proxy against
`gemma4-98e` (custom 98-expert MoE derivation of Gemma 4 E4B/A4B).

## Winning configuration (2026-04-18)

```
Modelfile tag:  gemma4-98e:native-tools
  - Gemma 4 NATIVE tool tokens:
      tool decl:      <|tool>...<tool|>
      tool call:      <|tool_call>call:NAME{ARGS}<tool_call|>
      tool response:  <|tool_response>response:NAME{...}<tool_response|>
      string delim:   <|"|>VALUE<|"|>
  - PARAMETER repeat_penalty 1.15
  - PARAMETER repeat_last_n 256
  - PARAMETER temperature 0.6 / top_p 0.95
  - PARAMETER stop "<turn|>"  and stop "<|tool_response>"
  - FROM gemma4-98e:cd-q6k-256k (256K ctx)

Proxy env:
  CALIBER_GROUNDING_MODEL_OVERRIDE=gemma4-98e:native-tools
  CALIBER_GROUNDING_THINK=medium
  CALIBER_GROUNDING_FORCE_ANSWER_AFTER=5
  CALIBER_GROUNDING_MAX_TOOL_CALLS_PER_TURN=8
```

### Benchmark on claude-hooks (heavy caliber-like prompt)

| Template | Total | Tool rounds | Calls | Refs | Backtick paths | Leaks |
|---|---|---|---|---|---|---|
| qwen3-style (:tools-rp)  | 81.7s | 1 | 8 | 0 | 27 | 0 |
| **native (:native-tools)** | **25.4s** | 3 | 3 unique | 1 file:line | 29 | 0 |

Native format is 3.2x faster, produces cleaner multi-turn behavior
(one tool per round), and surfaces the first `file:line` ref we've
seen (`cerebrum.md:2026`). Content quality cites actual modules
(`dispatcher.py`, `proxy/sse.py`, `stop_guard.py`, `rtk_rewrite.py`,
`install.py`).

## Failure modes observed

| Config | Symptom |
|---|---|
| `think:false` + tools | Model skips tools, fast but ungrounded |
| `think:low/true` + tools (no repeat_penalty) | 800-976 duplicate `<tool_call>` blocks per response |
| `think:low` + tools-rp | One tool call then iter-1 hangs >25 min |
| `think:medium` + tools-rp | **Works** — 81.7s, diverse tool use |

## Known mismatches vs official guidance

Per research into Google/Gemma 4 docs, llama.cpp issues, and Ollama
library blobs:

1. **Our template is qwen3-style**: `<tool_call>{...}</tool_call>`.
   Gemma 4's NATIVE format uses `<|tool|>` / `<|tool_call|>` /
   `<|tool_response|>` tokens with string values wrapped in
   `<|"|>...<|"|>`. The `<|"|>` token we saw leaking in early tests
   is actually Gemma 4's native delimiter, not a bug.

2. **We strip thinking between turns**: Google's docs say to KEEP
   thinking within a single tool-use sequence (model → tool_call →
   tool_response → model completing the same task), and only strip
   between distinct user turns. Our proxy strips unconditionally.
   Empirically this still works with `think:medium` because the
   thinking is short enough to not derail iter 1.

3. **Ollama has no hard thinking budget**: `reasoning_effort:
   "low"/"medium"/"high"` are hints, not token caps. llama.cpp's
   `--reasoning-budget 8192` has no Ollama equivalent.

## Future improvements

- Port to Gemma 4's native tool format in the Modelfile (new tag
  `:native-tools`) and compare against tools-rp head-to-head.
- Switch to llama.cpp serving with `--jinja --reasoning-budget 8192 -c
  32768` for strict thinking budget control; route the proxy at it
  instead of Ollama.
- Push the fixed Modelfile template to `ManniX-ITA/gemma-4-A4B-98e-v3-it-GGUF`
  and `...-109e-...` HF repos only after native-format variant
  benchmarks at parity or better.

## References

- [Ollama library: gemma4](https://ollama.com/library/gemma4)
- [Google Gemma 4 prompt formatting](https://ai.google.dev/gemma/docs/core/prompt-formatting-gemma4)
- [Ollama Thinking blog](https://ollama.com/blog/thinking)
- [llama.cpp #21375 — infinite loop in peg-gemma4 parser](https://github.com/ggml-org/llama.cpp/issues/21375)
- [ollama #15269 — missing public Gemma 4 template](https://github.com/ollama/ollama/issues/15269)
- [ollama #15350 — Gemma 4 Flash Attention hang](https://github.com/ollama/ollama/issues/15350)
