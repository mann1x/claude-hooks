#!/usr/bin/env bash
# Judge sampling test, 2026-09-23: does temperature 0.7 make
# glm-5.3-flash and deepseek-v4.1-flash better judges than the cloud
# default? Same 720 solutions (today + June, 42 failing) and the same 20
# retest solutions judged 3 more times, at 0.7 and at the default.
# Separation (AUC vs tests passing) and repeat agreement are the two
# numbers that decide it. Two processes = the bench's 2 connections.
set -u
cd /srv/dev-disk-by-label-opt/dev/claude-hooks
PY=/root/anaconda3/envs/claude-hooks/bin/python
R=benchmarks/consultants/results/2026-09-23
Q=benchmarks/consultants/questions/coder_med
LOG=$R/judge_eval/_progress.log
log() { echo "$* $(date -u +%T)" >> $LOG; }
J="$PY benchmarks/consultants/judge_eval.py judge --questions-dir $Q --concurrency 1 --retest-repeats 3"
one() {  # judge, sampling flags...
  local j="$1"; shift
  $J --trials $R/coder_med --out $R/judge_eval --judge "$j" --kinds base,retest "$@"
  $J --trials benchmarks/consultants/results/2026-06-04/coder_med --out $R/judge_eval_june --judge "$j" --kinds base "$@"
}
log "temp start"
( one glm-5.3-flash:cloud --temperature 0.7; one glm-5.3-flash:cloud --sampling none ) >> $R/judge_eval/temp-glm.log 2>&1 &
( one deepseek-v4.1-flash:cloud --temperature 0.7; one deepseek-v4.1-flash:cloud --sampling none ) >> $R/judge_eval/temp-ds.log 2>&1 &
wait
log "TEMP_DONE"
