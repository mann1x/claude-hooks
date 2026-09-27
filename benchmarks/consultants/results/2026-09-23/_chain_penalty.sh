#!/usr/bin/env bash
# glm-5.3-flash repetition-penalty test, 2026-09-23. Does
#   repeat_last_n 2048, repeat_penalty 1.1, frequency_penalty 0.1
# help glm-5.3-flash on top of temperature 0.7, or hurt it?
#
# Coder role (where penalties matter: long outputs, repeated code idioms):
#   default arm  = coder_med/glm-5.3-flash      (already on disk, cloud default)
#   0.7 arm      = coder_med-sampling/glm-5.3-flash-t07
#   penalty arm  = coder_med-sampling/glm-5.3-flash-t07-pen
#   60 questions each, kimi-k2.6 judge (same judge as the default arm, and
#   it has no template, so the subject's sampling is the only variable).
# Judge role: judge_eval on the same 720 solutions + 20 retests × 3, the
#   0.7 arm being the one _chain_temp.sh produces.
#
# Sampling reaches the coder through CLAUDE_HOOKS_MODEL_SAMPLING (merged
# over config/model-sampling.json), so the live claude-hooks.json is
# never touched. Starts after TEMP_DONE; two workers = the bench's 2
# connections (Pro allows 3, hooks hold 1).
set -u
cd /srv/dev-disk-by-label-opt/dev/claude-hooks
PY=/root/anaconda3/envs/claude-hooks/bin/python
R=benchmarks/consultants/results/2026-09-23
LOG=$R/judge_eval/_progress.log
log() { echo "$* $(date -u +%T)" >> $LOG; }
until grep -q '^TEMP_DONE' $LOG; do sleep 60; done
PEN='{"glm-5.3*":{"temperature":0.7,"repeat_last_n":2048,"repeat_penalty":1.1,"frequency_penalty":0.1}}'
T07='{"glm-5.3*":{"temperature":0.7}}'
OPTS=(--option repeat_last_n=2048 --option repeat_penalty=1.1 --option frequency_penalty=0.1)
coder() {  # slug env-json
  local d=$R/coder_med-sampling/$1; mkdir -p $d
  echo "$2" > $d/sampling.json
  CLAUDE_HOOKS_MODEL_SAMPLING="$2" $PY benchmarks/consultants/coder_bench.py --live --accept-cost \
    --questions-dir benchmarks/consultants/questions/coder_med --models glm-5.3-flash:cloud \
    --ollama-base http://192.168.178.161:11434 --judge-model kimi-k2.6:cloud \
    --judge-timeout-s 300 --judge-max-retries 1 --output-dir $d > $d/run.log 2>&1
  log "coder $1 rc=$?"
}
J="$PY benchmarks/consultants/judge_eval.py judge --questions-dir benchmarks/consultants/questions/coder_med --concurrency 1 --judge glm-5.3-flash:cloud"
log "penalty start"
( coder glm-5.3-flash-t07 "$T07"
  $J --trials $R/coder_med --out $R/judge_eval --kinds base,retest --retest-repeats 3 "${OPTS[@]}"
  log "judge pen today rc=$?" ) >> $R/judge_eval/pen-a.log 2>&1 &
sleep 5
( coder glm-5.3-flash-t07-pen "$PEN"
  $J --trials benchmarks/consultants/results/2026-06-04/coder_med --out $R/judge_eval_june --kinds base "${OPTS[@]}"
  log "judge pen june rc=$?" ) >> $R/judge_eval/pen-b.log 2>&1 &
wait
log "PEN_DONE"
