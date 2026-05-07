#!/usr/bin/env bash
#
# Run the canonical /consultants benchmark queries documented in
# docs/consultants-benchmarks.md. Captures trace + answer per query
# under docs/benchmarks/<label>/ so future model sweeps can be
# diffed against any prior run.
#
# Usage:
#   scripts/consultants_benchmark.sh <label> [--model <ollama-tag>]
#       [--at <git-tag-or-HEAD>] [--cwd <path>]
#       [--skip smoke|audit-medium|audit-high]...
#
# Subject codebase pinning (--at):
#   The consultants audit a frozen git worktree at the tag listed in
#   docs/benchmarks/CURRENT_BASELINE by default, so Q2 ground truth +
#   Q3 path:line refs stay valid as the engine evolves. Pass
#   --at <other-tag> to use a different baseline, or --at HEAD to
#   audit the live working tree (screening only — not comparable
#   across labels). The runner creates a worktree, runs from it,
#   and removes it on exit.
#
# Examples:
#   # Default: every role uses its configured model, audit the
#   # frozen baseline tag:
#   scripts/consultants_benchmark.sh kimi-k2.6-cloud-2026-05-07
#
#   # Pin every role to one model:
#   scripts/consultants_benchmark.sh deepseek-v4-pro --model deepseek-v4-pro:cloud
#
#   # Just the cheap one against the live tree (screening):
#   scripts/consultants_benchmark.sh quickcheck --at HEAD \
#       --skip audit-medium --skip audit-high
#
# The script does NOT touch the engine service unit. Restart it
# yourself if you change the model AND the engine runs in always-on
# mode (the consultants config is read fresh on each consult call,
# so model swaps via `claude-consultants config set-role` take
# effect on the next request — no restart needed).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DOC="${REPO}/docs/consultants-benchmarks.md"

LABEL=""
MODEL=""
# Subject codebase: by default, the consultants audit a git
# worktree at the tag listed in docs/benchmarks/CURRENT_BASELINE.
# This freezes Q2 ground truth + Q3 path:line refs across model
# sweeps. ``--at HEAD`` opts out (screening / dev runs).
BASELINE_TAG=""
CWD_OVERRIDE=""
SKIP=()
WORKTREE=""

usage() {
    grep -E '^# ' "$0" | sed 's/^# \?//' >&2
    exit 2
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --model) MODEL="$2"; shift 2 ;;
        --cwd) CWD_OVERRIDE="$(cd "$2" && pwd)"; shift 2 ;;
        --at) BASELINE_TAG="$2"; shift 2 ;;
        --skip) SKIP+=("$2"); shift 2 ;;
        -h|--help) usage ;;
        --*) echo "unknown flag: $1" >&2; usage ;;
        *) [[ -z "$LABEL" ]] && LABEL="$1" || { echo "extra positional: $1" >&2; usage; }; shift ;;
    esac
done

[[ -z "$LABEL" ]] && { echo "label is required" >&2; usage; }

# Resolve the subject codebase. Precedence:
#   1. --cwd <path> — explicit override (screening only).
#   2. --at HEAD    — use the live working tree (screening only).
#   3. --at <tag>   — create a worktree at that tag.
#   4. default      — read docs/benchmarks/CURRENT_BASELINE.
CWD=""
if [[ -n "${CWD_OVERRIDE}" ]]; then
    CWD="${CWD_OVERRIDE}"
    echo "::: subject codebase: explicit --cwd ${CWD} (screening run)"
elif [[ "${BASELINE_TAG}" == "HEAD" ]]; then
    CWD="${REPO}"
    echo "::: subject codebase: live HEAD at $(git -C "${REPO}" rev-parse --short HEAD) (screening run)"
else
    if [[ -z "${BASELINE_TAG}" ]]; then
        local_baseline_file="${REPO}/docs/benchmarks/CURRENT_BASELINE"
        [[ -f "${local_baseline_file}" ]] || {
            echo "!!! ${local_baseline_file} missing and no --at given" >&2
            exit 1
        }
        BASELINE_TAG="$(head -n 1 "${local_baseline_file}" | tr -d '[:space:]')"
    fi
    if ! git -C "${REPO}" rev-parse "${BASELINE_TAG}" >/dev/null 2>&1; then
        echo "!!! baseline tag '${BASELINE_TAG}' not in this repo" >&2
        exit 1
    fi
    WORKTREE="$(mktemp -d -t "claude-hooks-bench-${BASELINE_TAG//\//-}-XXXX")"
    # Clean up if we exit unexpectedly.
    trap 'if [[ -n "${WORKTREE:-}" && -d "${WORKTREE}" ]]; then
            git -C "${REPO}" worktree remove --force "${WORKTREE}" 2>/dev/null || rm -rf "${WORKTREE}"
          fi' EXIT
    git -C "${REPO}" worktree add --detach "${WORKTREE}" "${BASELINE_TAG}" \
        >/dev/null
    CWD="${WORKTREE}"
    echo "::: subject codebase: worktree at tag '${BASELINE_TAG}' "
    echo "    commit=$(git -C "${WORKTREE}" rev-parse --short HEAD)"
    echo "    path=${WORKTREE}"
fi

OUT_DIR="${REPO}/docs/benchmarks/${LABEL}"
mkdir -p "${OUT_DIR}"

# Pull a query body out of the doc by its anchor. The convention is:
#
#     <!-- BENCH-Q: smoke -->
#     ```text
#     <body>
#     ```
#
# We extract the lines between the anchor and the matching closing
# fence. Awk-based, no heredoc gymnastics.
extract_query() {
    local slug="$1"
    awk -v slug="${slug}" '
        $0 == "<!-- BENCH-Q: " slug " -->" { in_anchor = 1; next }
        in_anchor && /^```text/ { in_block = 1; next }
        in_anchor && in_block && /^```/ { exit }
        in_anchor && in_block { print }
    ' "${DOC}"
}

# Set every role's model when --model is given. Returns immediately
# when no model override is provided so per-role configs are
# untouched.
maybe_set_model() {
    [[ -z "${MODEL}" ]] && return 0
    echo "::: pinning every role to model=${MODEL}"
    for role in planner researcher critic synthesizer; do
        "${REPO}/bin/claude-consultants" config set-role \
            "${role}" --model "${MODEL}" >/dev/null
    done
}

is_skipped() {
    local q="$1"
    for s in "${SKIP[@]:-}"; do
        [[ "$s" == "$q" ]] && return 0
    done
    return 1
}

# Issue a single consult, poll until terminal, summarize trace, dump
# the answer file.
run_query() {
    local slug="$1"
    local effort="$2"
    local query
    query="$(extract_query "${slug}")"
    [[ -z "${query}" ]] && {
        echo "!!! no query body for slug=${slug} in ${DOC}" >&2
        return 2
    }
    is_skipped "${slug}" && {
        echo "::: ${slug} -- skipped"
        echo "skipped" > "${OUT_DIR}/${slug}.status"
        return 0
    }

    echo "::: ${slug} (effort=${effort}) -- starting"
    local started_at
    started_at="$(date +%s)"

    local consult_out
    consult_out="$(
        "${REPO}/bin/claude-consultants" consult \
            --trace --effort "${effort}" \
            --message "${query}" --cwd "${CWD}"
    )"
    local sid
    sid="$(printf '%s' "${consult_out}" | python3 -c \
        'import json,sys; print(json.load(sys.stdin)["sid"])')"
    echo "::: ${slug} sid=${sid} -- polling"

    while :; do
        sleep 15
        local status_json
        status_json="$(
            "${REPO}/bin/claude-consultants" status "${sid}" 2>/dev/null
        )" || continue
        local status
        status="$(printf '%s' "${status_json}" | python3 -c \
            'import json,sys; print(json.load(sys.stdin).get("status","?"))')"
        case "${status}" in
            completed|failed) break ;;
        esac
    done

    local finished_at
    finished_at="$(date +%s)"
    local wall_s=$((finished_at - started_at))
    echo "::: ${slug} sid=${sid} status=${status} wall=${wall_s}s"

    # Capture artifacts. Trace lives at ~/.claude/consultants-traces/;
    # the answer + transcript live under <cwd>/.claude-hooks/consultants/<sid>/.
    local trace_src="${HOME}/.claude/consultants-traces/${sid}.jsonl"
    local artifact_dir="${CWD}/.claude-hooks/consultants/${sid}"
    cp -f "${trace_src}" "${OUT_DIR}/${slug}.trace.jsonl" 2>/dev/null || true
    cp -f "${artifact_dir}/summary.md" \
        "${OUT_DIR}/${slug}.summary.md" 2>/dev/null || true
    cp -f "${artifact_dir}/transcript.md" \
        "${OUT_DIR}/${slug}.transcript.md" 2>/dev/null || true
    cp -f "${artifact_dir}/metadata.json" \
        "${OUT_DIR}/${slug}.metadata.json" 2>/dev/null || true
    "${REPO}/scripts/consultants_trace_summary.py" "${sid}" \
        > "${OUT_DIR}/${slug}.waterfall.txt" 2>&1 || true

    {
        echo "sid=${sid}"
        echo "status=${status}"
        echo "wall_s=${wall_s}"
        echo "started_at=${started_at}"
        echo "finished_at=${finished_at}"
        echo "effort=${effort}"
    } > "${OUT_DIR}/${slug}.status"
}

# Render the per-label results.md once all queries have run.
# Captures wall, token totals, LLM call count, tool call count, and
# per-role breakdown — see EVALUATION.md §2 for the KPI list.
write_results_md() {
    local results="${OUT_DIR}/results.md"
    local py_renderer="${REPO}/scripts/consultants_bench_row.py"
    local resolved_py
    resolved_py="$(${REPO}/bin/_resolve_python.sh 2>/dev/null \
        || command -v python3 || echo python3)"
    {
        echo "# Benchmark — \`${LABEL}\`"
        echo
        echo "Generated $(date -Iseconds) on $(hostname)."
        echo "Engine HEAD (\`${REPO}\`): \`$(git -C "${REPO}" rev-parse --short HEAD 2>/dev/null || echo unknown)\`"
        if [[ -n "${BASELINE_TAG}" ]]; then
            local cwd_commit
            cwd_commit="$(git -C "${CWD}" rev-parse --short HEAD 2>/dev/null || echo unknown)"
            echo "Subject baseline: \`${BASELINE_TAG}\` (commit \`${cwd_commit}\`) — frozen worktree at \`${CWD}\`"
        else
            echo "Subject baseline: live HEAD (screening run, NOT comparable across labels)"
        fi
        echo "Cloud model snapshot: see \`models.json\`."
        echo
        if [[ -n "${MODEL}" ]]; then
            echo "Model pin: \`${MODEL}\` (every role)."
        else
            echo "Model pin: per-role config (no override). Snapshot of \`claude-consultants config show\`:"
            echo
            echo '```json'
            "${REPO}/bin/claude-consultants" config show 2>/dev/null \
                || echo '(could not read config)'
            echo '```'
        fi
        echo
        echo "## Summary"
        echo
        echo "| Query | Effort | Status | Wall | Prompt tok | Completion tok | LLM calls | Tool calls | sid |"
        echo "|---|---|---|---|---|---|---|---|---|"
        for slug in smoke audit-medium audit-high; do
            "${resolved_py}" "${py_renderer}" "${slug}" "${OUT_DIR}" \
                --mode summary-row
        done
        echo
        echo "## Per-role breakdown"
        echo
        echo "Wall sums every entry of the role node (researcher in"
        echo "fan-out fires once per lane; counts accumulate). Token"
        echo "totals include cloud-model reasoning tokens."
        echo
        for slug in smoke audit-medium audit-high; do
            "${resolved_py}" "${py_renderer}" "${slug}" "${OUT_DIR}" \
                --mode role-table
        done
        echo "## Files"
        echo
        echo "Per-query artifacts in this directory:"
        echo
        for slug in smoke audit-medium audit-high; do
            echo "- **${slug}**"
            echo "  - \`${slug}.summary.md\` — synthesizer answer"
            echo "  - \`${slug}.transcript.md\` — full role transcript"
            echo "  - \`${slug}.waterfall.txt\` — per-role wall waterfall"
            echo "  - \`${slug}.trace.jsonl\` — raw JSONL trace"
            echo "  - \`${slug}.metadata.json\` — token totals + retries"
        done
        echo
        echo "## Per-query grades (manual, per [\`EVALUATION.md\`](../EVALUATION.md) §3)"
        echo
        echo "| Query | Grade | Notes |"
        echo "|---|---|---|"
        echo "| smoke        | _PASS / WEAK / FAIL_   | _one-line note_ |"
        echo "| audit-medium | _A / B / C / F_        | _one-line note_ |"
        echo "| audit-high   | _A / B / C / F_        | _one-line note_ |"
        echo
        echo "## Per-role grades (manual, per [\`EVALUATION.md\`](../EVALUATION.md) §3.5)"
        echo
        echo "Read each role's output in the per-query \`transcript.md\`"
        echo "files and assign one grade per role aggregated across all"
        echo "three queries. Critic grade is \`n/a\` unless audit-high ran."
        echo
        echo "| Role | Grade | One-sentence justification |"
        echo "|---|---|---|"
        echo "| planner     | _A / B / C / F_      | _why_ |"
        echo "| researcher  | _A / B / C / F_      | _why_ |"
        echo "| critic      | _A / B / C / F / n/a_| _why_ |"
        echo "| synthesizer | _A / B / C / F_      | _why_ |"
        echo
        echo "**Mix string:** \`P:_ R:_ C:_ S:_\`"
        echo
        echo "**Verdict:** _PROD-READY / EVALUATED-ONLY / UNSTABLE_"
        echo
        echo "**Commentary:** _one paragraph — what role(s) this model wins at vs prior labels, which role(s) it should NOT be used for, whether you'd build a heterogeneous mix around it_"
    } > "${results}"
    echo "::: wrote ${results}"
}

main() {
    [[ -f "${DOC}" ]] || {
        echo "!!! ${DOC} not found" >&2
        exit 1
    }

    # Health check before we burn cloud tokens.
    if ! curl -fsS http://127.0.0.1:38095/v1/health >/dev/null 2>&1; then
        echo "!!! consultants engine not responding on :38095" >&2
        echo "    start it with: systemctl --user start claude-hooks-consultants" >&2
        exit 1
    fi

    maybe_set_model

    # Capture the cloud-model snapshot BEFORE running queries so r2/r3
    # have something to verify against. Writes <label>/models.json.
    "${REPO}/scripts/consultants_model_snapshot.py" capture "${OUT_DIR}" \
        || echo "!!! model snapshot capture had probe failures (continuing)" >&2

    run_query smoke         medium
    run_query audit-medium  medium
    run_query audit-high    high

    write_results_md
}

main "$@"
