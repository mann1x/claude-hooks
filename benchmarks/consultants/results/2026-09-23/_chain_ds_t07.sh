#!/usr/bin/env bash
# deepseek-v4.1-flash as coder at temperature 0.7, 2026-09-24. Sampling
# templates are per model, not per role: a ds 0.7 template (better as a
# judge: retest agreement 65 -> 73 %, same AUC, cheaper) would also change
# ds as the primary coder on most routes. Baseline = coder_med/
# deepseek-v4.1-flash (cloud default). kimi-k2.6 judges both arms; it has
# no template, so the subject's sampling is the only variable. One
# connection. Ends with repair.py for anything an outage leaves.
set -u
cd /srv/dev-disk-by-label-opt/dev/claude-hooks
PY=/root/anaconda3/envs/claude-hooks/bin/python
R=benchmarks/consultants/results/2026-09-23
Q=benchmarks/consultants/questions/coder_med
LOG=$R/judge_eval/_progress.log
log() { echo "$* $(date -u +%T)" >> $LOG; }
D=$R/coder_med-sampling/deepseek-v4.1-flash-t07
mkdir -p $D
T='{"deepseek-v4.1-flash*":{"temperature":0.7}}'
echo "$T" > $D/sampling.json
log "ds_t07 start"
CLAUDE_HOOKS_MODEL_SAMPLING="$T" $PY benchmarks/consultants/coder_bench.py --live --accept-cost \
  --questions-dir $Q --models deepseek-v4.1-flash:cloud \
  --ollama-base http://192.168.178.161:11434 --judge-model kimi-k2.6:cloud \
  --judge-timeout-s 300 --judge-max-retries 1 --output-dir $D > $D/run.log 2>&1
log "ds_t07 coder rc=$?"
$PY benchmarks/consultants/repair.py --questions-dir $Q --rounds 4 --pause-s 600 \
  --coder-run $D >> $D/repair.log 2>&1
log "ds_t07 repair rc=$?"
log "DS_T07_DONE"
