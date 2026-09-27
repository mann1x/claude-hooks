#!/usr/bin/env bash
# Head-to-head: glm-5.3-flash vs deepseek-v4.1-flash as the default coder
# judge. Runs after the council screenings (they hold the bench's 2
# connections). Today's run: deepseek gets everything glm got. June's
# coder_med (420 solutions, 32 failing — 3x the failures today's run
# has) is judged by both, so separation rests on more than a handful of
# broken solutions.
set -u
cd /srv/dev-disk-by-label-opt/dev/claude-hooks
PY=/root/anaconda3/envs/claude-hooks/bin/python
R=benchmarks/consultants/results/2026-09-23
Q=benchmarks/consultants/questions/coder_med
BH=/srv/dev-disk-by-label-opt/dev/bench-home-2026-09-23
OUT=$R/judge_eval
JUNE=$R/judge_eval_june
mkdir -p $JUNE
log() { echo "$* $(date -u +%T)" >> $OUT/_progress.log; }
until grep -q '^ALL_DONE' $BH/council.log 2>/dev/null; do sleep 60; done
log "h2h start"
J="$PY benchmarks/consultants/judge_eval.py judge --questions-dir $Q --concurrency 1"
$J --trials $R/coder_med --out $OUT --judge deepseek-v4.1-flash:cloud --kinds base,retest,variant >> $OUT/deepseek.log 2>&1 &
$J --trials benchmarks/consultants/results/2026-06-04/coder_med --out $JUNE --judge glm-5.3-flash:cloud --kinds base >> $JUNE/glm.log 2>&1
wait
log "h2h today+june-glm done"
$J --trials benchmarks/consultants/results/2026-06-04/coder_med --out $JUNE --judge deepseek-v4.1-flash:cloud --kinds base >> $JUNE/deepseek.log 2>&1
log "H2H_DONE"
