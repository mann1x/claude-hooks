# Judges and sampling — 2026-09-23/24

> ↟ [Benchmark index](index.md) · costs: [`costs.md`](costs.md#judge-evaluations) ·
> protocol: [`EVALUATION.md` §2.2–2.4](EVALUATION.md)

This page answers two questions:

1. Which judge should score coder_bench now that kimi-k2.6 is too
   expensive to keep as the default?
2. Which sampling settings should the new cloud models run with?

## Verdict

| decision | outcome | evidence |
|---|---|---|
| coder_bench judge | **panel: glm-5.3-flash + deepseek-v4.1-flash, settled by deepseek-v4.1-flash** (`judge_panel.py`, default since 44c943e) | separates passing from failing code as well as kimi (AUC 0.958 vs 0.955) at 1/7 the cost per verdict |
| glm-5.3* sampling | **temperature 0.7**, shipped in `config/model-sampling.json` | Cerebriline's hand test (0.2 broken, 1.0 poor, 0.7 good). As a judge: repeat agreement 67 → 78 %, separation within noise. As a coder: no change |
| repetition penalties for glm-5.3-flash | **not shipped** | as a coder, one run 0.770 → 0.740 quality, 58 → 56/60 passed; as a judge no better than 0.7 alone |
| deepseek-v4.1-flash sampling | **temperature 0.7**, shipped 2026-09-24 | as a coder identical to the default (Q 0.827, 60/60 both); as a judge steadier (65 → 73 %), cheaper and faster |

## Method

`benchmarks/consultants/judge_eval.py` makes a candidate judge score
code whose correctness is already known. Two coder_med runs provide
it: 300 solutions from 2026-09-23 and 420 from 2026-06-04, 720 in
total, of which 42 fail their tests. The candidate is graded on:

- **Separation.** The AUC of its score against *tests pass*. This
  measures whether the judge ranks working code above broken code. It
  is the oracle kimi's scores were never checked against.
- **Repeat agreement.** 20 solutions judged 3 more times each; the
  share of repeats that match the first score exactly.
- **Self-bias.** The judge's offset against kimi on its own family's
  code, minus its offset on everyone else's.
- **Style affinity and invariance**, speed, and $ per verdict.

A candidate's sampling is part of its name
(`glm-5.3-flash:cloud@temperature=0.7`), so results under different
settings never pool. Two WAN outages on 2026-09-23 left 125 verdicts
without a score. `repair.py` asked for exactly those again; the tables
below have no gaps.

## Judges

AUC over the 654 solutions kimi also scored. The 95 % intervals come
from 2000 bootstrap resamples.

| judge | AUC [95 % CI] | repeat agreement | self-bias | $ / verdict | s / verdict |
|---|---|---|---|---|---|
| kimi-k2.6 (old reference) | 0.955 [0.913, 0.989] | — | — | 0.0145 | 94 (p90 203) |
| **panel glm + ds, ds settles** | **0.958** [0.905, 0.995] | 63 % | glm +0.14 · ds −0.12 | **0.0020** | 17 |
| glm-5.3-flash | 0.927 [0.864, 0.976] | 67 % | +0.14 | 0.0006 | 10 |
| glm-5.3-flash @ 0.7 | 0.909 [0.844, 0.967] | 78 % | +0.13 | 0.0006 | 9 |
| glm-5.3-flash @ 0.7 + penalties | 0.924 [0.859, 0.980] | 68 % | +0.15 | 0.0006 | 7 |
| deepseek-v4.1-flash | 0.882 [0.810, 0.948] | 65 % | −0.12 | 0.0004 | 1.8 |
| deepseek-v4.1-flash @ 0.7 | 0.891 [0.825, 0.949] | 73 % | −0.08 | 0.0003 | 1.2 |

"Penalties" means `repeat_last_n 2048, repeat_penalty 1.1,
frequency_penalty 0.1`.

- **The panel is the only cheap judge that matches kimi on
  separation.** The judges disagreed on 184 of the 720 solutions. The
  synthesizer checks each review's claim against the code; it took the
  higher score 119 times and the lower 62 times, and never split the
  difference. It sees the reviews as A and B, in an order set by a hash
  of the trial. That matters because it is deepseek, which is also one
  of the reviewers.
- **glm still favours glm code slightly.** The panel's glm-family
  offset (+0.14) equals glm's own. It is under a sixth of a point on a
  1–5 scale, and deepseek scores its own family 0.12 lower.
- **Temperature 0.7 makes both judges more consistent** without
  moving separation outside noise, and makes deepseek cheaper and
  faster.
- The members of this panel ran at the cloud default. In coder_bench,
  glm now runs at its 0.7 template, which is the steadier setting.

## Sampling — coder

60 coder_med questions per arm, all judged by kimi-k2.6 (no template),
so the subject's sampling is the only variable. Q = mean of
*judge score ÷ 5* over trials whose tests pass, with failing trials
counting 0.

| subject | sampling | Q | passed | judge avg | $ / trial (coder) |
|---|---|---|---|---|---|
| glm-5.3-flash | cloud default | 0.770 | 58/60 | 3.88 | 0.00055 |
| glm-5.3-flash | temperature 0.7 | 0.767 | 58/60 | 3.87 | 0.00052 |
| glm-5.3-flash | 0.7 + penalties | 0.740 | 56/60 | 3.82 | 0.00049 |
| deepseek-v4.1-flash | cloud default | 0.827 | 60/60 | 4.13 | 0.00133 |
| deepseek-v4.1-flash | temperature 0.7 | 0.827 | 60/60 | 4.13 | 0.00134 |

Per language, Q:

| glm-5.3-flash | c | cpp | csharp | go | python | rust |
|---|---|---|---|---|---|---|
| default | 0.78 | 0.82 | 0.86 | 0.62 | 0.84 | 0.70 |
| 0.7 | 0.74 | 0.84 | 0.78 | 0.76 | 0.86 | 0.62 |
| 0.7 + penalties | 0.68 | 0.74 | 0.64 | 0.76 | 0.92 | 0.70 |

| deepseek-v4.1-flash | c | cpp | csharp | go | python | rust |
|---|---|---|---|---|---|---|
| default | 0.88 | 0.88 | 0.80 | 0.72 | 0.90 | 0.78 |
| 0.7 | 0.86 | 0.88 | 0.80 | 0.78 | 0.94 | 0.70 |

Each arm is one run (N=1). A 0.03 gap in Q is one or two questions,
and per-language cells move by one question either way. Only the glm
penalty arm's two extra failures lean one way. deepseek at 0.7 matches
its default to the third decimal, so shipping its template for the
judge role costs the coder role nothing.

## Why sampling lives in the request

The Ollama cloud tags carry no parameters. Before 2026-09-23, every
call ran at the provider default, glm-5.3 included (temperature 1.0).
Ollama Modelfile overlays (`glm-5.3-flash-tpl2`, …) would fix that on
one host and silently not on another. Templates now travel with each
request instead. They come from `config/model-sampling.json`, with user
overrides in `config/claude-hooks.json` and a per-process
`CLAUDE_HOOKS_MODEL_SAMPLING`. Runbook: [`../model-sampling.md`](../model-sampling.md).

## Reproduce

```bash
PY=/root/anaconda3/envs/claude-hooks/bin/python
Q=benchmarks/consultants/questions/coder_med
# a judge arm (any sampler field via --option)
$PY benchmarks/consultants/judge_eval.py judge --questions-dir $Q \
    --trials benchmarks/consultants/results/2026-09-23/coder_med \
    --out benchmarks/consultants/results/2026-09-23/judge_eval \
    --judge glm-5.3-flash:cloud --kinds base,retest --retest-repeats 3 \
    --option repeat_penalty=1.1
# the panel, settled over verdicts already on disk
$PY benchmarks/consultants/judge_eval.py synth --questions-dir $Q \
    --members glm-5.3-flash:cloud,deepseek-v4.1-flash:cloud \
    --trials benchmarks/consultants/results/2026-09-23/coder_med \
    --out benchmarks/consultants/results/2026-09-23/judge_eval
# a coder arm
CLAUDE_HOOKS_MODEL_SAMPLING='{"glm-5.3*":{"temperature":0.7}}' \
  $PY benchmarks/consultants/coder_bench.py --live --accept-cost \
    --questions-dir $Q --models glm-5.3-flash:cloud \
    --judge-model kimi-k2.6:cloud --output-dir <arm dir>
# always last: fill what an outage left
$PY benchmarks/consultants/repair.py --questions-dir $Q \
    --judge-eval <out>=<trials> --coder-run <arm dir>
```

Chains used: `benchmarks/consultants/results/2026-09-23/_chain_*.sh`.
