#!/usr/bin/env bash
# Offline panel check, 2026-09-23: settle the glm-5.3-flash and
# deepseek-v4.1-flash verdicts already on disk with the deepseek
# synthesizer (judge_panel.resolve), then `report --judge <panel label>`
# grades the panel on the same AUC / stability / self-bias / cost as
# each member. Starts after PEN_DONE; 2 workers = 2 connections.
set -u
cd /srv/dev-disk-by-label-opt/dev/claude-hooks
PY=/root/anaconda3/envs/claude-hooks/bin/python
R=benchmarks/consultants/results/2026-09-23
LOG=$R/judge_eval/_progress.log
log() { echo "$* $(date -u +%T)" >> $LOG; }
until grep -q '^PEN_DONE' $LOG; do sleep 60; done
S="$PY benchmarks/consultants/judge_eval.py synth --questions-dir benchmarks/consultants/questions/coder_med --members glm-5.3-flash:cloud,deepseek-v4.1-flash:cloud"
log "panel start"
$S --trials $R/coder_med --out $R/judge_eval --kinds base,retest >> $R/judge_eval/panel-today.log 2>&1 &
$S --trials benchmarks/consultants/results/2026-06-04/coder_med --out $R/judge_eval_june --kinds base >> $R/judge_eval/panel-june.log 2>&1 &
wait
log "PANEL_DONE"
