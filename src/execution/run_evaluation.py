"""Tier-1 Trade Evaluation production entry point invoked by ``run_evaluation.sh``.

Mirrors ``execution.run_cycle`` for the trading pipeline: builds three
``run_subagent`` callables for the evaluator team and hands them to
``execution.evaluation_bootstrap.bootstrap_evaluation_team``.

Spec: ``trading_feedback.md §6 "Weiterer Ausbau"``.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

from execution.evaluation_bootstrap import (
    EvaluationAbortedError,
    EvaluationArtifacts,
    bootstrap_evaluation_team,
)
from execution.subagent_runner import run_subagent
from shared.config.settings import Settings
from shared.logging import bind, configure, get_logger
from shared.models import (
    AgentPerformanceOutput,
    AgentPerformanceTask,
    OutcomeFetcherOutput,
    OutcomeFetcherTask,
    PnlAggregatorOutput,
    PnlAggregatorTask,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
AGENTS_DIR = REPO_ROOT / ".claude" / "agents"
PROMPTS_DIR = REPO_ROOT / "src" / "research" / "prompts"

logger = get_logger(__name__)


def run_inline(
    *,
    settings: Settings | None = None,
    correlation_id: str | None = None,
) -> EvaluationArtifacts:
    """Run one Tier-1 evaluation pass as a library call.

    Used by ``execution.feedback_phase`` when ``INLINE_TIER1_EVAL_ENABLED``
    is on. Mirrors ``main`` but raises on ``EvaluationAbortedError`` instead
    of converting to an exit code, so the caller can decide how to react.
    """
    cfg = settings or Settings()
    cid = correlation_id or uuid.uuid4().hex
    bind(correlation_id=cid)
    logger.info("run_evaluation_start", lookback_days=cfg.EVALUATION_LOOKBACK_DAYS)

    def outcome_fetcher(task: OutcomeFetcherTask) -> OutcomeFetcherOutput:
        return run_subagent(
            agent_md_path=AGENTS_DIR / "outcome-fetcher.md",
            doctrine_path=_doctrine("outcome_fetcher.md"),
            task=task,
            output_model=OutcomeFetcherOutput,
            timeout_s=cfg.EVALUATION_TIMEOUT_SEC,
            cycle_id=task.cycle_id,
            correlation_id=cid,
            max_budget_usd=cfg.BUDGET_USD_OUTCOME_FETCHER,
        )

    def pnl_aggregator(task: PnlAggregatorTask) -> PnlAggregatorOutput:
        return run_subagent(
            agent_md_path=AGENTS_DIR / "pnl-aggregator.md",
            doctrine_path=_doctrine("pnl_aggregator.md"),
            task=task,
            output_model=PnlAggregatorOutput,
            timeout_s=cfg.EVALUATION_TIMEOUT_SEC,
            cycle_id=task.cycle_id,
            correlation_id=cid,
            max_budget_usd=cfg.BUDGET_USD_PNL_AGGREGATOR,
        )

    def perf_updater(task: AgentPerformanceTask) -> AgentPerformanceOutput:
        return run_subagent(
            agent_md_path=AGENTS_DIR / "agent-performance-updater.md",
            doctrine_path=_doctrine("agent_performance_updater.md"),
            task=task,
            output_model=AgentPerformanceOutput,
            timeout_s=cfg.EVALUATION_TIMEOUT_SEC,
            cycle_id=task.cycle_id,
            correlation_id=cid,
            max_budget_usd=cfg.BUDGET_USD_AGENT_PERFORMANCE_UPDATER,
        )

    artifacts = bootstrap_evaluation_team(
        settings=cfg,
        outcome_fetcher=outcome_fetcher,
        pnl_aggregator=pnl_aggregator,
        perf_updater=perf_updater,
    )
    logger.info(
        "run_evaluation_done",
        cycle_id=artifacts.cycle_id,
        n_candidates=artifacts.n_candidates,
        n_resolved=artifacts.n_resolved,
        n_rows=len(artifacts.rows),
    )
    return artifacts


def main() -> int:
    configure(service="trade_evaluation")
    try:
        run_inline()
    except EvaluationAbortedError as exc:
        logger.warning(
            "run_evaluation_aborted",
            cycle_id=exc.cycle_id,
            stage=exc.stage,
            reason=exc.reason,
        )
        return 2
    return 0


def _doctrine(name: str) -> Path | None:
    path = PROMPTS_DIR / name
    return path if path.exists() else None


if __name__ == "__main__":
    sys.exit(main())
