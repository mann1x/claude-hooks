#!/usr/bin/env bash
# Judge evaluation, 2026-09-23: is glm-5.3-flash a good enough coder
# judge to replace kimi-k2.6? deepseek-v4.1-flash is measured as the
# second-pass judge for glm-family code, in case glm is fair to others
# but not to itself. Runs between the coder run and the council
# screenings, and never holds more than the 2 connections the bench
# gets (Pro = 3, hooks hold 1).
#
# Phase 1 (1 connection, while worker B still codes): glm base verdicts
#   for the models worker A has finished.
# Phase 2 (2 connections, coder run done): the rest of glm's base
#   verdicts plus its retest and style-variant samples; deepseek on the
#   glm-family solutions; kimi on the same variants and retest sample.
#   Samples are drawn from the full run, so they are taken only here.
set -u
cd /srv/dev-disk-by-label-opt/dev/claude-hooks
PY=/root/anaconda3/envs/claude-hooks/bin/python
R=benchmarks/consultants/results/2026-09-23
P=$R/coder_med/_progress.log
OUT=$R/judge_eval
JE="$PY benchmarks/consultants/judge_eval.py judge --trials $R/coder_med --questions-dir benchmarks/consultants/questions/coder_med --out $OUT"
log() { echo "$* $(date -u +%T)" >> $OUT/_progress.log; }

until grep -q '^DONE kimi-k2.6' $P; do sleep 60; done
done_models=$(grep -oP '^DONE \K\S+(?= rc=0)' $P | sed 's/$/:cloud/' | paste -sd,)
log "phase1 start models=$done_models"
$JE --judge glm-5.3-flash:cloud --kinds base --models "$done_models" --concurrency 1 >> $OUT/glm.log 2>&1
log "phase1 done rc=$?"

until grep -q '^ALL_DONE' $P; do sleep 60; done
log "phase2 start"
$JE --judge glm-5.3-flash:cloud --kinds base,retest,variant --concurrency 2 >> $OUT/glm.log 2>&1
log "glm done rc=$?"
$JE --judge deepseek-v4.1-flash:cloud --kinds base --models glm-5.3:cloud,glm-5.3-flash:cloud --concurrency 2 >> $OUT/deepseek.log 2>&1
log "deepseek done rc=$?"
$JE --judge kimi-k2.6:cloud --kinds retest,variant --concurrency 2 >> $OUT/kimi.log 2>&1
log "kimi done rc=$?"
$PY benchmarks/consultants/judge_eval.py report --out $OUT --trials $R/coder_med \
    --judge glm-5.3-flash:cloud --second-pass deepseek-v4.1-flash:cloud > /dev/null 2>&1
log "JUDGE_DONE"
