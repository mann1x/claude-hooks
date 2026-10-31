# Compaction bench — 2026-10-19

Model `qwen36-base-mtp_tb:27b-q4km-128k` at `http://192.168.178.161:22434`, window pinned to 32,768 tokens, think=False, 4 trials per arm, commit `8185719`.

## By arm

| arm | n | answered | fixed (of answered) | compactions (mean / min–max) | fold calls | fold s / trial | files read | re-reads | coverage | errors |
|---|---|---|---|---|---|---|---|---|---|---|
| full/system | 4 | 4 | 0% (0) | 6.5 / 3–11 | 22.2 | 520 | 28.0/28 | 20.0 | 0.78 | 0 |
| basic/system | 4 | 4 | 0% (0) | 3.8 / 2–7 | 0.0 | 0 | 27.0/28 | 6.2 | 1.00 | 0 |
| reduced/system | 4 | 4 | 0% (0) | 15.0 / 9–23 | 23.8 | 559 | 28.0/28 | 39.2 | 1.00 | 0 |

## Fix-rate by compaction count

| compactions | fork baseline | full/system | basic/system | reduced/system |
|---|---|---|---|---|
| 0 | 85% | — (0) | — (0) | — (0) |
| 1 | 63% | — (0) | — (0) | — (0) |
| 2 | 50% | — (0) | 0% (1) | — (0) |
| 3+ | 25% | 0% (4) | 0% (3) | 0% (4) |

## Placement A/B (user vs system)


## Facts carried

| arm | decisecond unit | ×100 fix | ticket (follow-up) | limits.py re-reads refused |
|---|---|---|---|---|
| full/system | 0/4 | 0/4 | 4/4 | 4 |
| basic/system | 0/4 | 0/4 | 4/4 | 3 |
| reduced/system | 0/4 | 0/4 | 4/4 | 6 |

## Grader

Task v3. A trial is fixed when the answer names `should_retry` and `backoff`, the decisecond unit, a ×100 conversion and ticket `LDG-4471`. Each fact is available once (see the module docstring).
