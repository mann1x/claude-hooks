#!/usr/bin/env bash
# coder_med v1.0, 2026-09-23. Four new candidates, plus the four models the
# current coder routes use (the protocol re-baselines incumbents in the same
# run). Upstream: eleven2go's Ollama, which is only a relay. Every call
# lands on the Ollama Cloud account.
#
# CONCURRENCY IS THE ACCOUNT'S, NOT THE HOST'S. The Pro plan allows 3
# concurrent connections, and the claude-hooks hooks already hold one. So
# the benchmark gets at most 2: two workers, each running one model's 60
# questions in order. Inside a trial the coder call and the judge call are
# sequential, so each worker holds one connection at a time. The earlier
# attempts in ../coder_med-aborted-* ran 6 to 16 at once and timed out.
#
# The kimi-k2.6 judge thinks ~2k tokens per verdict and can overrun 60 s:
# timeout 300 s, 1 retry.
#
# Worker A runs the deepseek models first, to use the off-peak window
# (before 12:00 UTC on weekdays). Every call's tokens land in trials.jsonl
# `usage`.
set -u
PY=/root/anaconda3/envs/claude-hooks/bin/python
BASE=benchmarks/consultants/results/2026-09-23/coder_med
QDIR=benchmarks/consultants/questions/coder_med
OLLAMA=http://192.168.178.161:11434

run_one() {
  local model="$1" slug="$2"
  mkdir -p "$BASE/$slug"
  echo "START $slug $(date -u +%H:%M:%S)" >> "$BASE/_progress.log"
  "$PY" benchmarks/consultants/coder_bench.py --live --accept-cost \
    --questions-dir "$QDIR" --models "$model" \
    --ollama-base "$OLLAMA" --judge-model kimi-k2.6:cloud \
    --judge-timeout-s 300 --judge-max-retries 1 \
    --output-dir "$BASE/$slug" > "$BASE/$slug/run.log" 2>&1
  echo "DONE $slug rc=$? $(date -u +%H:%M:%S)" >> "$BASE/_progress.log"
}

worker_a() {
  run_one 'deepseek-v4.1-flash:cloud' deepseek-v4.1-flash
  run_one 'deepseek-v4-flash:cloud'   deepseek-v4-flash
  run_one 'glm-5.3:cloud'             glm-5.3
  run_one 'kimi-k2.6:cloud'           kimi-k2.6
}
worker_b() {
  run_one 'deepseek-v4-pro:cloud'     deepseek-v4-pro
  run_one 'glm-5.3-flash:cloud'       glm-5.3-flash
  run_one 'glm-5.2:cloud'             glm-5.2
  run_one 'minimax-m3:cloud'          minimax-m3
}

worker_a & sleep 5
worker_b &
wait
echo "ALL_DONE $(date -u +%H:%M:%S)" >> "$BASE/_progress.log"
