"""Trading-cycle production entry point invoked by ``run_cycle.sh``.

Wires factory-built adapters and subagent-runner callables into
``bootstrap_team``. Phase 5 ships this as the canonical cron entry; Phase 6
performs the first live paper-cycle against the real Polymarket API.
"""

from __future__ import annotations

import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

from sqlalchemy import text

from execution.decision_context import require_decision_id
from execution.lead_bootstrap import CycleAbortedError, bootstrap_team
from execution.subagent_runner import run_subagent
from shared.adapters.factory import make_adapter
from shared.config.settings import Settings
from shared.db import get_session
from shared.logging import bind, configure, get_logger
from shared.models import (
    RiskExecutionOutput,
    RiskExecutionTask,
    TradingAgentOutput,
    TradingAgentTask,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager

    from sqlalchemy.orm import Session

HIGH_WATER_MARK_KEY: Final = "last_trading_cycle_at"

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
AGENTS_DIR = REPO_ROOT / ".claude" / "agents"
PROMPTS_DIR = REPO_ROOT / "research" / "prompts"
MCP_CONFIG_PATH = REPO_ROOT / ".mcp.json"
TRADING_AGENT_MCP_TOOLS = ("mcp__research__web_search",)

logger = get_logger(__name__)


def main() -> int:
    configure(service="trading_cycle")
    correlation_id = uuid.uuid4().hex
    bind(correlation_id=correlation_id)

    settings = Settings()
    logger.info("run_cycle_start", mode=settings.TRADING_MODE)

    adapter = make_adapter(settings, decision_id_provider=require_decision_id)

    def trading(task: TradingAgentTask) -> TradingAgentOutput:
        return run_subagent(
            agent_md_path=AGENTS_DIR / "trading-agent.md",
            doctrine_path=_doctrine("trading_agent.md"),
            task=task,
            output_model=TradingAgentOutput,
            timeout_s=settings.AGENT_TIMEOUT_SEC,
            cycle_id=task.cycle_id,
            correlation_id=correlation_id,
            mcp_config_path=MCP_CONFIG_PATH if MCP_CONFIG_PATH.exists() else None,
            allowed_mcp_tools=TRADING_AGENT_MCP_TOOLS,
            max_budget_usd=settings.BUDGET_USD_TRADING,
        )

    def risk(task: RiskExecutionTask) -> RiskExecutionOutput:
        return run_subagent(
            agent_md_path=AGENTS_DIR / "risk-execution.md",
            doctrine_path=_doctrine("risk_execution.md"),
            task=task,
            output_model=RiskExecutionOutput,
            timeout_s=settings.AGENT_TIMEOUT_SEC,
            cycle_id=task.cycle_id,
            correlation_id=correlation_id,
            max_budget_usd=settings.BUDGET_USD_RISK,
        )

    try:
        artifacts = bootstrap_team(
            settings=settings,
            adapter=adapter,
            trading=trading,
            risk=risk,
        )
    except CycleAbortedError as exc:
        # Budget exhaustion (or another recoverable abort) is logged structured
        # and exits with a distinct non-zero code so cron can alert without
        # treating it as a hard crash.
        logger.warning(
            "run_cycle_aborted",
            cycle_id=exc.cycle_id,
            stage=exc.stage,
            reason=exc.reason,
        )
        return 2
    _write_heartbeat(None, datetime.now(UTC))
    logger.info(
        "run_cycle_done",
        cycle_id=artifacts.cycle_id,
        predictions=len(artifacts.predictions),
        decisions=len(artifacts.decisions),
        trades=len(artifacts.trades),
    )
    return 0


def _doctrine(name: str) -> Path | None:
    path = PROMPTS_DIR / name
    return path if path.exists() else None


def _write_heartbeat(
    factory: Callable[[], AbstractContextManager[Session]] | None,
    ts: datetime,
) -> None:
    """Stamp `system_state.last_trading_cycle_at` after a successful cycle.

    Mirrors the high-water-mark write pattern used by outcome_ingestion and
    lessons_summary. Only invoked on the success path; an aborted cycle must
    not advance the heartbeat.
    """
    actual = factory if factory is not None else (lambda: get_session("trading_cycle"))
    with actual() as session:
        session.execute(
            text(
                """
                INSERT INTO system_state (key, value, updated_at)
                VALUES (:k, jsonb_build_object('ts', CAST(:v AS TEXT)), NOW())
                ON CONFLICT (key) DO UPDATE
                    SET value = EXCLUDED.value, updated_at = NOW()
                """,
            ),
            {"k": HIGH_WATER_MARK_KEY, "v": ts.isoformat()},
        )


if __name__ == "__main__":
    sys.exit(main())
