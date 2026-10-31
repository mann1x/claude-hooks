# Compaction bench — 2026-10-15

Model `qwen36-base-mtp_tb:27b-q4km-128k` at `http://192.168.178.161:22434`, window pinned to 32,768 tokens, think=False, 4 trials per arm, commit `d9976a0`.

## By arm

| arm | n | fixed | compactions (mean / min–max) | fold calls | fold s / trial | files read | re-reads | coverage | errors |
|---|---|---|---|---|---|---|---|---|---|
| reduced/system | 4 | 75% | 5.2 / 3–7 | 16.2 | 136 | 28.0/28 | 32.5 | 1.00 | 0 |
| reduced/user | 4 | 25% | 7.2 / 6–9 | 12.2 | 79 | 28.0/28 | 27.0 | 1.00 | 0 |
| full/system | 4 | 50% | 6.5 / 6–8 | 24.8 | 315 | 28.0/28 | 21.8 | 0.79 | 0 |
| full/user | 4 | 50% | 7.0 / 3–10 | 26.0 | 297 | 28.0/28 | 42.5 | 1.00 | 0 |
| basic/system | 4 | 0% | 3.0 / 3–3 | 0.0 | 0 | 28.0/28 | 1.2 | 1.00 | 0 |

## Fix-rate by compaction count

| compactions | fork baseline | reduced/system | reduced/user | full/system | full/user | basic/system |
|---|---|---|---|---|---|---|
| 0 | 85% | — (0) | — (0) | — (0) | — (0) | — (0) |
| 1 | 63% | — (0) | — (0) | — (0) | — (0) | — (0) |
| 2 | 50% | — (0) | — (0) | — (0) | — (0) | — (0) |
| 3+ | 25% | 75% (4) | 25% (4) | 50% (4) | 50% (4) | 0% (4) |

## Placement A/B (user vs system)

- **reduced**: system 75% (4), user 25% (4)
- **full**: system 50% (4), user 50% (4)

## Facts carried

| arm | decisecond unit | ×100 fix | ticket (follow-up) | limits.py re-reads refused |
|---|---|---|---|---|
| reduced/system | 3/4 | 3/4 | 4/4 | 5 |
| reduced/user | 1/4 | 1/4 | 4/4 | 4 |
| full/system | 2/4 | 2/4 | 4/4 | 5 |
| full/user | 3/4 | 2/4 | 4/4 | 5 |
| basic/system | 1/4 | 0/4 | 4/4 | 2 |

## Grader

Task v2. A trial is fixed when the answer names `should_retry` and `backoff`, the decisecond unit, a ×100 conversion and ticket `LDG-4471`. Each fact is available once (see the module docstring).

## Provenance

The run was paused at 15 of 20 trials (05:29–06:25) so the server could be upgraded and lent out. A process keeps the code it imported, so the two segments ran on different code and servers:

| trials | code | server |
|---|---|---|
| #0–#2 (15) | `dev` at ~04:13, before `b263f9e`: Milestone C complete, D1–D3 standard, no D4–D15 | xOllama before 0.40.4-xollama.1 |
| #3 (5) | `d9976a0`: D4–D15 in, among them D5 (`read_file` cuts on whole lines) and D6 (cap warning), both on this bench's path | 0.40.4-xollama.1 |

The commit printed at the top is the resume's. Trial #3's outcomes match the pattern of #0–#2 (reduced/system and full/system fixed; reduced/user, full/user and basic not), so the split is noted, not corrected for.

## Reading

Four trials an arm separate nothing statistically: reduced/system 3/4 against reduced/user 1/4 is p = 0.49 (Fisher exact), and system against user pooled over both flows (5/8 against 3/8) is p = 0.62. What the run does show:

- **Summarising earns its calls.** basic fixed 0 of 4. Every summarising arm fixed at least one, 8 of 16 together (p = 0.12 against basic). The v2 facts are readable once, so a model that lost them could not re-read them back, as it did in v1.
- **Every arm ran ≥ 3 compactions in every trial.** That is the fork's worst bucket (25 %). reduced/system fixed 75 % there, full 50 %.
- **reduced is no worse than full, at under half the fold time** (136 s against 315 s a trial on system placement; 16 calls against 25).
- **`user` placement folded more under reduced** (7.2 against 5.2) but did not re-read more (27.0 against 32.5). v1's restart after each fold (102 re-reads in 130) did not recur. The fold count alone does not explain its 1/4.

Decision: the defaults stay `flow = "reduced"`, `placement = "system"`. The bench gives no reason to move either, and weak evidence for both. A placement verdict would need ≥ 20 trials an arm.
