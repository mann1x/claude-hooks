# 2026-10-22 run set

`minimax-m3.1-flash:cloud` (free on Ollama until 23:59 UTC that day) through
every bench, N=3, then a gemma4 council re-run on the same engine. Everything
ran on eleven2go's Ollama with at most 2 concurrent cloud calls, booked through
the resource manager (`ollama-cloud:shared:2`). Raw `trials.jsonl` and
per-trial sandboxes stay local (`.gitignore`); each run's `metadata.json` and
`report.md` (rendered by `analyze.py`) are committed.

Council results live under `docs/benchmarks/`:
[`minimax-m3-1-flash-cloud-2026-10-22`](../../../../docs/benchmarks/minimax-m3-1-flash-cloud-2026-10-22/results.md)
and [`gemma4-31b-cloud-2026-10-22`](../../../../docs/benchmarks/gemma4-31b-cloud-2026-10-22/results.md).

## Results

| suite | minimax-m3.1-flash (r1 / r2 / r3) | compared with |
|---|---|---|
| council | PASS / A / C (mode), `P:A R:A C:B S:B`, EVALUATED-ONLY | gemma4 same engine: PASS / B / C, `P:A R:B C:B S:A` |
| tool_executor (24 trials) | 75% pass, quality 4.79; medium tier 0/6 | gemma4 87.5%, 5.00 (2026-05-17, same suite hash `7921555c`) |
| coder (python) | 100 / 88 / 100% | — |
| coder_easy | 96 / 98 / 97% | minimax-m3 98% |
| coder_med | 93 / 90 / 88% | deepseek-v4.1-flash 100%, glm-5.3-flash 97%, minimax-m3 97% (2026-09-23) |
| coder_mlang (18 q) | 39 / 33 / 39%, fails the rubric | not comparable: the 2026-05-17 baseline ran 13 of the questions |
| stall tier 1 (12 trials) | no errors; recommends stall 30 s / hard cap 840 s | — |

Coder runs were judged by the default panel (glm-5.3-flash + deepseek-v4.1-flash,
deepseek settles). The coder_med and coder_easy baselines were judged by
kimi-k2.6, so only pass rates compare across them, not quality.

No role changes: minimax-m3.1-flash is below the current coder routes on
coder_med and below gemma4 on tool_executor; as a council model its
evidence is strong but its Q3 recommendation is the capped status relabel.

## Notes

- The minimax council runs went back to back, not ≥ 10 min apart, and
  shared the cloud with the coder suites.
- A first gemma4 attempt started at 06:39 UTC beside the minimax suites
  (a resource-manager reuse bug, bug-1105, fixed in `8f69cee`); it was
  stopped after ~2 min and discarded. The gemma4 runs reported are the
  re-run at 10:36–11:02 UTC.
- The 2026-09-23 gemma4 council baseline ran with no reasoning (fixed in
  `c148363`); compare gemma4 only with `gemma4-31b-cloud-2026-10-22`.

## Chains

`_chain.sh` (minimax, two slots) and `_chain_gemma.sh` (gemma4 re-run,
queued with `claude-hooks-res wait`) are the exact commands.
