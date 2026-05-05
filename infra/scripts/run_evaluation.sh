#!/usr/bin/env bash
# Tier-1 Trade Evaluation Team — cron wrapper.
#
# Invoked by `infra/cron/evaluation.cron`. Loads .env, hands control to the
# Python entry point. The Python orchestrator binds to the `lessons_summary`
# DB role; no signing keys, no order endpoints.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

if [[ -f .env ]]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

exec uv run python -m execution.run_evaluation
