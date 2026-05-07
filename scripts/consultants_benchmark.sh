#!/usr/bin/env bash
#
# Run the canonical /consultants benchmark queries documented in
# docs/consultants-benchmarks.md. Captures trace + answer per query
# under docs/benchmarks/<label>/ so future model sweeps can be
# diffed against any prior run.
#
# Usage:
#   scripts/consultants_benchmark.sh <label> [--model <ollama-tag>]
#       [--skip smoke|audit-medium|audit-high]... [--cwd <path>]
#
# Examples:
#   # Use the model already configured per role:
#   scripts/consultants_benchmark.sh kimi-k2.6-cloud-2026-05-07
#
#   # Pin every role to one model:
#   scripts/consultants_benchmark.sh deepseek-v4-pro --model deepseek-v4-pro:cloud
#
#   # Just the cheap one:
#   scripts/consultants_benchmark.sh quickcheck --skip audit-medium --skip audit-high
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
CWD="${REPO}"
SKIP=()

usage() {
    grep -E '^# ' "$0" | sed 's/^# \?//' >&2
    exit 2
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --model) MODEL="$2"; shift 2 ;;
        --cwd) CWD="$(cd "$2" && pwd)"; shift 2 ;;
        --skip) SKIP+=("$2"); shift 2 ;;
        -h|--help) usage ;;
        --*) echo "unknown flag: $1" >&2; usage ;;
        *) [[ -z "$LABEL" ]] && LABEL="$1" || { echo "extra positional: $1" >&2; usage; }; shift ;;
    esac
done

[[ -z "$LABEL" ]] && { echo "label is required" >&2; usage; }

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
write_results_md() {
    local results="${OUT_DIR}/results.md"
    {
        echo "# Benchmark — \`${LABEL}\`"
        echo
        echo "Generated $(date -Iseconds) on $(hostname)."
        echo
        if [[ -n "${MODEL}" ]]; then
            echo "Model pin: \`${MODEL}\` (every role)."
        else
            echo "Model pin: per-role config (no override)."
        fi
        echo
        echo "| Query | Effort | Status | Wall | sid |"
        echo "|---|---|---|---|---|"
        for slug in smoke audit-medium audit-high; do
            local f="${OUT_DIR}/${slug}.status"
            if [[ ! -f "${f}" ]]; then
                echo "| ${slug} | – | (no run) | – | – |"
                continue
            fi
            # shellcheck disable=SC1090
            source "${f}"
            if [[ "${status:-skipped}" == "skipped" ]]; then
                echo "| ${slug} | – | skipped | – | – |"
            else
                echo "| ${slug} | ${effort} | ${status} | ${wall_s}s | \`${sid}\` |"
            fi
            unset sid status wall_s started_at finished_at effort
        done
        echo
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

    run_query smoke         medium
    run_query audit-medium  medium
    run_query audit-high    high

    write_results_md
}

main "$@"
