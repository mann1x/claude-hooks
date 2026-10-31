# Compaction bench — 2026-10-19

Model `qwen36-base-mtp_tb:27b-q4km-128k` at `http://192.168.178.161:22434`, window pinned to 32,768 tokens, think=False, 4 trials per arm, commit `8185719`.

## By arm

| arm | n | answered | fixed (of answered) | compactions (mean / min–max) | fold calls | fold s / trial | files read | re-reads | coverage | errors |
|---|---|---|---|---|---|---|---|---|---|---|
| full/system | 4 | 4 | 75% (3) | 3.0 / 2–5 | 12.5 | 482 | 28.5/28 | 11.2 | 0.77 | 0 |
| basic/system | 4 | 4 | 0% (0) | 5.0 / 2–9 | 0.0 | 0 | 26.8/28 | 15.8 | 1.00 | 0 |
| reduced/system | 4 | 4 | 25% (1) | 6.5 / 2–17 | 20.5 | 660 | 28.0/28 | 30.2 | 0.77 | 0 |

## Fix-rate by compaction count

| compactions | fork baseline | full/system | basic/system | reduced/system |
|---|---|---|---|---|
| 0 | 85% | — (0) | — (0) | — (0) |
| 1 | 63% | — (0) | — (0) | — (0) |
| 2 | 50% | 50% (2) | 0% (1) | 50% (2) |
| 3+ | 25% | 100% (2) | 0% (3) | 0% (2) |

## Placement A/B (user vs system)


## Facts carried

| arm | decisecond unit | ×100 fix | ticket (follow-up) | limits.py re-reads refused |
|---|---|---|---|---|
| full/system | 4/4 | 3/4 | 3/4 | 2 |
| basic/system | 0/4 | 0/4 | 4/4 | 7 |
| reduced/system | 2/4 | 2/4 | 3/4 | 5 |

## Grader

Task v3. A trial is fixed when the answer names `should_retry` and `backoff`, the decisecond unit, a ×100 conversion and ticket `LDG-4471`. Each fact is available once (see the module docstring).
