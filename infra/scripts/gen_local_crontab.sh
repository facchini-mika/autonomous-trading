#!/usr/bin/env bash
# Print a path-resolved crontab fragment for the three core trading cycles
# (trading_cycle, outcome_ingestion, lessons_summary) to stdout.
#
# Operator usage:
#   crontab -l > /tmp/crontab.bak.$(date +%s)        # backup current crontab
#   bash infra/scripts/gen_local_crontab.sh | crontab -
#   crontab -l                                       # verify three lines installed
#
# Pre-flight: aborts (exit ≠ 0) if gtimeout is missing, .env is absent,
# TIMEOUT_BIN is not pinned to gtimeout, or .env opts into real_capital.
# evaluation.cron (Tier-1) is intentionally not emitted — install separately.

set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

err() { echo "gen_local_crontab.sh: $*" >&2; exit 1; }

[[ -f .env ]] || err ".env not found at $repo_root/.env"
command -v gtimeout >/dev/null 2>&1 || err "gtimeout missing — \`brew install coreutils\`"
grep -qE '^TIMEOUT_BIN=gtimeout$' .env || err ".env must contain TIMEOUT_BIN=gtimeout (macOS)"

# Real-capital opt-in is a separate, audited PR. This generator is paper-only.
if grep -qE '^TRADING_MODE=real_capital' .env; then
  err "refusing to emit cron with TRADING_MODE=real_capital in .env (paper-only generator)"
fi

mkdir -p logs

cat <<EOF
# autonomous_trading — generated $(date '+%Y-%m-%d %H:%M:%S %Z') by gen_local_crontab.sh
# Repo: $repo_root
# Mode: paper (TRADING_MODE default; MAX_CAPITAL_EUR=0 hardcoded)
# Uninstall: crontab -e and delete these lines, or crontab -r to clear all.
*/30 * * * * cd $repo_root && bash infra/scripts/run_cycle.sh trading_cycle      >> logs/trading_cycle.log 2>&1
0   * * * * cd $repo_root && bash infra/scripts/run_cycle.sh outcome_ingestion  >> logs/outcome_ingestion.log 2>&1
30  4 * * * cd $repo_root && bash infra/scripts/run_cycle.sh lessons_summary    >> logs/lessons_summary.log 2>&1
EOF
