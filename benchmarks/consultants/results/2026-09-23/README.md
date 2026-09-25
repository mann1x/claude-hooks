# 2026-09-23/24 run set

New cloud models (glm-5.3, glm-5.3-flash, deepseek-v4.1-flash,
deepseek-v4-pro) as coder, a judge evaluation, and sampling tests.
Everything ran on eleven2go's Ollama with at most 2 concurrent cloud
calls. Findings are written up in
[`docs/benchmarks/judge-and-sampling.md`](../../../../docs/benchmarks/judge-and-sampling.md),
[`coder-med-results.md`](../../../../docs/benchmarks/coder-med-results.md#2026-09-23-re-baseline)
and [`costs.md`](../../../../docs/benchmarks/costs.md).

Raw `trials.jsonl`, `verdicts.jsonl` and per-trial sandboxes stay local
(`.gitignore`). What is committed here is each run's `metadata.json`,
the sampling of each arm, the judge reports and the chain scripts that
produced them.

## Citable

| dir | what | judge |
|---|---|---|
| `coder_med/` | coder_med@1.0 re-baseline, 5 models × 60 q, cloud-default sampling (`_launch.sh`) | kimi-k2.6 |
| `coder_med-sampling/glm-5.3-flash-t07/` | glm-5.3-flash at temperature 0.7 | kimi-k2.6 |
| `coder_med-sampling/glm-5.3-flash-t07-pen/` | … + repeat_last_n 2048, repeat_penalty 1.1, frequency_penalty 0.1 | kimi-k2.6 |
| `coder_med-sampling/deepseek-v4.1-flash-t07/` | deepseek-v4.1-flash at temperature 0.7 | kimi-k2.6 |
| `judge_eval/`, `judge_eval_june/` | candidate judges on this run's 300 and 2026-06-04's 420 solutions; sampling arms; the glm + ds panel (`judge_eval.py synth`) | — |

Each `sampling.json` holds the `CLAUDE_HOOKS_MODEL_SAMPLING` the arm
ran under. Two WAN outages (~19:54–20:07 and ~20:21–20:24 UTC on
09-23) left 125 verdicts and 4 coder judgments without a score;
`repair.py` (`_chain_repair.sh`) filled every one before anything was
reported. Filled trials carry `quality_filled`, repaired verdicts
`repaired`.

## Not citable — `*-aborted-*`

| dir | why |
|---|---|
| `coder_med-aborted-saturated/` | 6–16 concurrent calls against a 3-connection plan: judge timeouts, not model behaviour |
| `coder_med-aborted-6way/` | per-language shards, still over the connection limit |
| `coder_med-aborted-judge-timeout/` | kimi judge 60 s timeout too short; restarted at 300 s |
| `coder_med-aborted-retiring-deepseek-v4-flash/` | stopped: model retires 2026-09-25 and was not asked for |
| `coder_med-aborted-anchor-glm-5.2/`, `…-anchor-kimi-k2.6/` | stopped: incumbents were not to be re-run ("no anchors") |

## Chains

`_chain_*.sh` (here and in `judge_eval/`) are the exact commands, in
order: `judge_eval/_chain.sh` / `_chain2.sh` (judges),
`judge_eval/_chain_temp.sh` (judge temperature),
`_chain_queue.sh` (coder sampling arms, penalty judging, panel;
it replaced `_chain_penalty.sh` and `_chain_panel.sh`, which never ran),
`_chain_repair.sh` and `_chain_ds_t07.sh`.
