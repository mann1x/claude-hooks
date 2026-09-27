#!/usr/bin/env bash
# Job queue for the rest of 2026-09-23's sampling + panel work. It
# replaces two chains that waited for TEMP_DONE and so would have left a
# connection idle for hours (the deepseek half of _chain_temp.sh finishes
# long before the glm half). Two slots = the bench's 2 connections (Pro
# allows 3, the hooks hold 1). Each slot starts when its _chain_temp.sh
# worker is gone, then takes the next job.
#
# Jobs:
#   coder_t07 / coder_pen — glm-5.3-flash as coder on coder_med (60 q),
#     temperature 0.7, and 0.7 + repeat_last_n 2048, repeat_penalty 1.1,
#     frequency_penalty 0.1. The cloud-default arm is coder_med/glm-5.3-flash.
#     kimi-k2.6 judges, as it did the default arm; it has no template, so
#     the subject's sampling is the only variable. Sampling goes in through
#     CLAUDE_HOOKS_MODEL_SAMPLING, never the live claude-hooks.json.
#   judge_pen_* — glm-5.3-flash as judge with the same penalties, on the
#     solutions the temperature test used (its 0.7 arm is the comparison).
#   panel_* — judge_panel's deepseek synthesizer settles the glm + deepseek
#     verdicts on disk, for `report --judge <panel label>`.
set -u
cd /srv/dev-disk-by-label-opt/dev/claude-hooks
PY=/root/anaconda3/envs/claude-hooks/bin/python
R=benchmarks/consultants/results/2026-09-23
Q=benchmarks/consultants/questions/coder_med
JUNE=benchmarks/consultants/results/2026-06-04/coder_med
LOG=$R/judge_eval/_progress.log
QF=$R/judge_eval/_queue.txt
log() { echo "$* $(date -u +%T)" >> $LOG; }
PEN='{"glm-5.3*":{"temperature":0.7,"repeat_last_n":2048,"repeat_penalty":1.1,"frequency_penalty":0.1}}'
T07='{"glm-5.3*":{"temperature":0.7}}'
OPTS="--option repeat_last_n=2048 --option repeat_penalty=1.1 --option frequency_penalty=0.1"
J="$PY benchmarks/consultants/judge_eval.py judge --questions-dir $Q --concurrency 1 --judge glm-5.3-flash:cloud"
S="$PY benchmarks/consultants/judge_eval.py synth --questions-dir $Q --members glm-5.3-flash:cloud,deepseek-v4.1-flash:cloud"
printf '%s\n' coder_t07 coder_pen judge_pen_today judge_pen_june panel_today panel_june > $QF

coder() {  # slug env-json
  local d=$R/coder_med-sampling/$1; mkdir -p $d; echo "$2" > $d/sampling.json
  CLAUDE_HOOKS_MODEL_SAMPLING="$2" $PY benchmarks/consultants/coder_bench.py --live --accept-cost \
    --questions-dir $Q --models glm-5.3-flash:cloud \
    --ollama-base http://192.168.178.161:11434 --judge-model kimi-k2.6:cloud \
    --judge-timeout-s 300 --judge-max-retries 1 --output-dir $d > $d/run.log 2>&1
}
job() {
  case "$1" in
    coder_t07) coder glm-5.3-flash-t07 "$T07" ;;
    coder_pen) coder glm-5.3-flash-t07-pen "$PEN" ;;
    judge_pen_today) $J --trials $R/coder_med --out $R/judge_eval --kinds base,retest --retest-repeats 3 $OPTS >> $R/judge_eval/pen-today.log 2>&1 ;;
    judge_pen_june) $J --trials $JUNE --out $R/judge_eval_june --kinds base $OPTS >> $R/judge_eval/pen-june.log 2>&1 ;;
    panel_today) $S --trials $R/coder_med --out $R/judge_eval --kinds base,retest >> $R/judge_eval/panel-today.log 2>&1 ;;
    panel_june) $S --trials $JUNE --out $R/judge_eval_june --kinds base >> $R/judge_eval/panel-june.log 2>&1 ;;
  esac
}
next_job() {  # pop the first line under a lock
  (
    flock 9
    head -n1 $QF
    sed -i 1d $QF
  ) 9>$QF.lock
}
busy() {  # a _chain_temp.sh worker for this judge still running? checked twice
  # _chain_temp.sh writes "--judge X --kinds"; this queue's own judge
  # jobs write "--judge X --trials", so they never look like it.
  local pat="judge_eval.py judge .*--judge $1 --kinds"
  pgrep -f "$pat" > /dev/null && return 0
  sleep 20
  pgrep -f "$pat" > /dev/null
}
slot() {  # name, the _chain_temp.sh worker's judge
  while busy "$2"; do sleep 60; done
  log "slot $1 free"
  while :; do
    local j; j=$(next_job); [ -z "$j" ] && break
    log "slot $1 start $j"; job "$j"; log "slot $1 done $j rc=$?"
  done
}
log "queue start"
slot A deepseek-v4.1-flash:cloud &
slot B glm-5.3-flash:cloud &
wait
log "QUEUE_DONE"
