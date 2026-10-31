# Compaction bench — 2026-10-19

Model `qwen36-base-mtp_tb:27b-q4km-128k` at `http://192.168.178.161:22434`, window pinned to 32,768 tokens, think=False, 4 trials per arm, commit `5b60e8a`.

## By arm

| arm | n | fixed | compactions (mean / min–max) | fold calls | fold s / trial | files read | re-reads | coverage | errors |
|---|---|---|---|---|---|---|---|---|---|
| reduced/user | 4 | 0% | 6.8 / 3–10 | 13.5 | 223 | 28.0/28 | 27.2 | 1.00 | 0 |
| full/system | 4 | 50% | 7.0 / 3–13 | 24.2 | 290 | 28.0/28 | 21.5 | 0.79 | 0 |
| reduced/system | 4 | 0% | 7.0 / 6–9 | 19.2 | 251 | 28.0/28 | 37.2 | 1.00 | 0 |
| basic/system | 4 | 25% | 6.8 / 0–12 | 0.0 | 0 | 22.8/28 | 24.8 | 0.75 | 0 |
| full/user | 4 | 25% | 8.2 / 0–23 | 15.5 | 184 | 21.8/28 | 18.5 | 0.34 | 0 |

## Fix-rate by compaction count

| compactions | fork baseline | reduced/user | full/system | reduced/system | basic/system | full/user |
|---|---|---|---|---|---|---|
| 0 | 85% | — (0) | — (0) | — (0) | 0% (1) | 0% (1) |
| 1 | 63% | — (0) | — (0) | — (0) | — (0) | — (0) |
| 2 | 50% | — (0) | — (0) | — (0) | — (0) | 0% (1) |
| 3+ | 25% | 0% (4) | 50% (4) | 0% (4) | 33% (3) | 50% (2) |

## Placement A/B (user vs system)

- **reduced**: system 0% (4), user 0% (4)
- **full**: system 50% (4), user 25% (4)

## Facts carried

| arm | decisecond unit | ×100 fix | ticket (follow-up) | limits.py re-reads refused |
|---|---|---|---|---|
| reduced/user | 0/4 | 0/4 | 4/4 | 4 |
| full/system | 3/4 | 2/4 | 4/4 | 4 |
| reduced/system | 2/4 | 0/4 | 4/4 | 5 |
| basic/system | 1/4 | 1/4 | 3/4 | 4 |
| full/user | 1/4 | 1/4 | 1/4 | 4 |

## Grader

Task v2. A trial is fixed when the answer names `should_retry` and `backoff`, the decisecond unit, a ×100 conversion and ticket `LDG-4471`. Each fact is available once (see the module docstring).
