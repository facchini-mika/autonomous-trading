#!/usr/bin/env bash
# Phase 6c: long-running Prometheus exporter wrapper.
#
# Run via launchd (macOS) or systemd-unit (Linux). Loads .env, then exec's
# the Python module so signal-handling (SIGTERM/SIGINT) reaches the daemon
# unmediated.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

if [[ -f .env ]]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

exec uv run python -m shared.observability
