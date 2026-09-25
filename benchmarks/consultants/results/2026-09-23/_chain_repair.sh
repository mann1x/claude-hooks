#!/usr/bin/env bash
# Repair step for 2026-09-23's runs (WAN outages ~19:54-20:07 and
# ~20:21-20:24 UTC left 502'd verdicts and unjudged coder trials). Waits
# for QUEUE_DONE so nothing is still appending, then benchmarks/consultants/
# repair.py re-asks exactly the holes (same judge, sampling and prompt),
# re-settles panel verdicts, and loops up to 4 rounds with a 10-minute
# pause for a line that is still down. Two workers = the bench's 2
# connections. Clients wait out an outage for 30 min (bench_client).
set -u
cd /srv/dev-disk-by-label-opt/dev/claude-hooks
PY=/root/anaconda3/envs/claude-hooks/bin/python
R=benchmarks/consultants/results/2026-09-23
Q=benchmarks/consultants/questions/coder_med
JUNE=benchmarks/consultants/results/2026-06-04/coder_med
LOG=$R/judge_eval/_progress.log
log() { echo "$* $(date -u +%T)" >> $LOG; }
until grep -q '^QUEUE_DONE' $LOG; do sleep 60; done
REP="$PY benchmarks/consultants/repair.py --questions-dir $Q --rounds 4 --pause-s 600"
log "repair start"
( $REP --judge-eval $R/judge_eval=$R/coder_med \
       --coder-run $R/coder_med-sampling/glm-5.3-flash-t07 \
       --coder-run $R/coder_med-sampling/glm-5.3-flash-t07-pen
  log "repair today rc=$?" ) >> $R/judge_eval/repair-a.log 2>&1 &
( $REP --judge-eval $R/judge_eval_june=$JUNE
  log "repair june rc=$?" ) >> $R/judge_eval/repair-b.log 2>&1 &
wait
log "REPAIR_DONE"
