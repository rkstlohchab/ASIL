#!/usr/bin/env bash
#
# record_demo.sh — a single-shot tour of ASIL designed to be screen-recorded
# straight through. Each section prints a banner, runs the command, then
# pauses so a viewer (or you, narrating) has a beat to read the output before
# the next thing scrolls past.
#
# Usage:
#   ./scripts/record_demo.sh           # interactive — waits for ENTER between sections
#   ./scripts/record_demo.sh --auto    # autoplay — fixed pauses, ready to record
#
# Recording recipe:
#   1. `asciinema rec asil-demo.cast` (or open Cmd+Shift+5 and select your terminal)
#   2. Run `./scripts/record_demo.sh --auto` in the recorded terminal
#   3. Ctrl+D when the script exits, then `agg asil-demo.cast asil-demo.gif`
#
# Prereqs (the script checks these and exits early if missing):
#   - `make up` has been run
#   - `uv run asil ingest .` has been run at least once
#   - At least one postmortem has been ingested + linked
#
# Designed to run in <90 seconds end-to-end on `--auto` so the resulting
# GIF stays small enough to embed in a blog post.

set -euo pipefail

# ---------------------------------------------------------------------- knobs

AUTO=false
INCIDENT_ID="INC-2026-04-12-payments-cascade"
ASK_QUESTION="How does the LLM router pick a provider for a given tier?"
PAUSE_SHORT=2
PAUSE_LONG=4

while [[ $# -gt 0 ]]; do
    case "$1" in
        --auto) AUTO=true; shift ;;
        --incident) INCIDENT_ID="$2"; shift 2 ;;
        --question) ASK_QUESTION="$2"; shift 2 ;;
        --short)    PAUSE_SHORT="$2"; shift 2 ;;
        --long)     PAUSE_LONG="$2"; shift 2 ;;
        --help|-h)
            sed -n '2,30p' "$0"; exit 0 ;;
        *) echo "unknown flag: $1"; exit 2 ;;
    esac
done

# --------------------------------------------------------------------- helpers

BOLD=$'\033[1m'; DIM=$'\033[2m'; CYAN=$'\033[36m'; RESET=$'\033[0m'
GREEN=$'\033[32m'; YELLOW=$'\033[33m'

banner() {
    local title="$1"
    local subtitle="${2:-}"
    printf '\n'
    printf '%s━━━ %s ━━━%s\n' "${BOLD}${CYAN}" "$title" "${RESET}"
    [[ -n "$subtitle" ]] && printf '%s%s%s\n' "${DIM}" "$subtitle" "${RESET}"
    printf '\n'
}

pause() {
    local seconds="$1"
    if $AUTO; then
        sleep "$seconds"
    else
        printf '%s[press ENTER to continue]%s' "${DIM}" "${RESET}"
        read -r _
    fi
}

run() {
    printf '%s$ %s%s\n' "${GREEN}" "$*" "${RESET}"
    "$@"
}

abort() {
    printf '%s\n%s%s\n' "${YELLOW}" "$1" "${RESET}"
    exit 1
}

# --------------------------------------------------------------- preflight

cd "$(dirname "$0")/.."

command -v uv >/dev/null || abort "uv not found — install from astral.sh/uv first"
docker ps >/dev/null 2>&1 || abort "docker not running — start Docker Desktop"

# Quick connectivity probe so we don't get halfway in and discover Neo4j is down.
uv run asil status >/dev/null 2>&1 || abort "asil status failed — run 'make up' first"

banner "ASIL — 90-second tour" "Engineering Intelligence Infrastructure"
printf '%sRecorded for the blog post. Every command below works on a fresh checkout.%s\n' \
    "${DIM}" "${RESET}"
pause "$PAUSE_SHORT"

# ---------------------------------------------------------------- 1. status

banner "1. Health check" "all backing services up?"
run uv run asil status
pause "$PAUSE_LONG"

# ----------------------------------------------------------------- 2. ask (fresh + cached)

banner "2. Ask a question" "verifier + citations + confidence — fresh call ≈ \$0.01"
run uv run asil ask "$ASK_QUESTION"
pause "$PAUSE_LONG"

banner "3. Ask the SAME question again" "cached recall ≈ \$0.0001 — this is the money shot"
run uv run asil ask "$ASK_QUESTION"
pause "$PAUSE_LONG"

# ---------------------------------------------------------------- 4. cost summary

banner "4. Cost summary" "total spent + per-provider + episodic-memory savings"
run uv run asil cost summary
pause "$PAUSE_LONG"

# ------------------------------------------------------------- 5. incident replay

banner "5. Incident replay (Phase 5)" "timeline + ranked causes + cascade + state diff"
if ! uv run asil temporal causes "$INCIDENT_ID" >/dev/null 2>&1; then
    printf '%sNo causal chain for %s yet — running the linker first...%s\n' \
        "${YELLOW}" "$INCIDENT_ID" "${RESET}"
    uv run asil postmortem ingest "research/postmortems/2025-08-14-payments-redis-cascade.yaml" >/dev/null
    uv run asil temporal link prod >/dev/null
fi
run uv run asil replay "$INCIDENT_ID"
pause "$PAUSE_LONG"

# ----------------------------------------------------------- 6. fix propose

banner "6. Phase 8: constrained fix" "narrow LLM prompt → unified diff → confidence bound by weakest cause"
run uv run asil fix propose "$INCIDENT_ID"
pause "$PAUSE_LONG"

# ------------------------------------------------------------ 7. scan (PR gate)

banner "7. CI scan (SonarQube-shaped surface)" "single command, exits 0/1/2 based on the gate"
# `|| true` so a gate failure here doesn't kill the demo — it's the point of the screenshot.
run uv run asil scan --pr-comment - --gate normal || true
pause "$PAUSE_LONG"

# ---------------------------------------------------------------- closing

banner "Tour complete" "open the dashboard at http://localhost:3001 for the visual half"
printf '%sNext:%s\n' "${BOLD}" "${RESET}"
printf '  • screenshot the ReactFlow causal graph on /incidents/<id>\n'
printf '  • screenshot the /cost daily-spend bars + savings card\n'
printf '  • render the PR-comment markdown locally for the GitHub-comment shot\n'
printf '\n'
