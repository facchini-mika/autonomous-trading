"""Trading-cycle production entry point invoked by ``run_cycle.sh``.

Wires factory-built adapters and subagent-runner callables into
``bootstrap_team``. Phase 5 ships this as the canonical cron entry; Phase 6
performs the first live paper-cycle against the real Polymarket API.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

from execution.decision_context import require_decision_id
from execution.lead_bootstrap import bootstrap_team
from execution.subagent_runner import run_subagent
from shared.adapters.factory import make_adapter
from shared.config.settings import Settings
from shared.logging import bind, configure, get_logger
from shared.models import (
    RiskExecutionOutput,
    RiskExecutionTask,
    ScannerReviewerOutput,
    ScannerReviewerTask,
    TradingAgentOutput,
    TradingAgentTask,
)

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

    def scanner(task: ScannerReviewerTask) -> ScannerReviewerOutput:
        return run_subagent(
            agent_md_path=AGENTS_DIR / "scanner-reviewer.md",
            doctrine_path=_doctrine("scanner_reviewer.md"),
            task=task,
            output_model=ScannerReviewerOutput,
            timeout_s=settings.AGENT_TIMEOUT_SEC,
        )

    def trading(task: TradingAgentTask) -> TradingAgentOutput:
        return run_subagent(
            agent_md_path=AGENTS_DIR / "trading-agent.md",
            doctrine_path=_doctrine("trading_agent.md"),
            task=task,
            output_model=TradingAgentOutput,
            timeout_s=settings.AGENT_TIMEOUT_SEC,
            mcp_config_path=MCP_CONFIG_PATH if MCP_CONFIG_PATH.exists() else None,
            allowed_mcp_tools=TRADING_AGENT_MCP_TOOLS,
        )

    def risk(task: RiskExecutionTask) -> RiskExecutionOutput:
        return run_subagent(
            agent_md_path=AGENTS_DIR / "risk-execution.md",
            doctrine_path=_doctrine("risk_execution.md"),
            task=task,
            output_model=RiskExecutionOutput,
            timeout_s=settings.AGENT_TIMEOUT_SEC,
        )

    artifacts = bootstrap_team(
        settings=settings,
        adapter=adapter,
        scanner=scanner,
        trading=trading,
        risk=risk,
    )
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


if __name__ == "__main__":
    sys.exit(main())
