#!/usr/bin/env bash
# Cron-invoked wrapper: load env, apply timeout, dispatch to one of the three
# Phase-5 cycle entry points. Operator installs this via the .cron files in
# infra/cron/. See docs/operations/first_cycle.md for the full runbook.
#
# Usage: run_cycle.sh {trading_cycle|outcome_ingestion|lessons_summary}

set -euo pipefail

if [[ -z "${1:-}" ]]; then
  echo "usage: run_cycle.sh {trading_cycle|outcome_ingestion|lessons_summary}" >&2
  exit 64
fi
cycle="$1"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

# coreutils on Linux ships `timeout`; macOS users install via `brew install coreutils`
# (gtimeout) — first_cycle.md documents the alias.
TIMEOUT_BIN="${TIMEOUT_BIN:-timeout}"

case "$cycle" in
  trading_cycle)
    "$TIMEOUT_BIN" 600 uv run python -m execution.run_cycle
    ;;
  outcome_ingestion)
    "$TIMEOUT_BIN" 1800 uv run python -m execution.outcome_ingestion
    ;;
  lessons_summary)
    "$TIMEOUT_BIN" 1800 uv run python -m research.skills.lessons_summary
    ;;
  *)
    echo "run_cycle.sh: unknown cycle '$cycle'" >&2
    echo "expected: trading_cycle | outcome_ingestion | lessons_summary" >&2
    exit 64
    ;;
esac
