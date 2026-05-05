"""Per-agent rolling stats — one row per Tier-1 evaluation cycle.

Single source of truth for ``agent_performance`` rows. Written by the
Tier-1 Trade Evaluation Team orchestrator (`execution.evaluation_bootstrap`),
read by the Prometheus collector (`shared.observability.collector`) and
downstream Tier-2 consumers (``optimization.md`` lessons / pattern miners).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class AgentPerformance(BaseModel):
    """One row of ``agent_performance``.

    Fields follow ``data_infrastructure.md §1`` exactly. ``hit_rate_30d`` and
    ``sharpe_30d`` are nullable: hit_rate is undefined when n_samples = 0;
    sharpe is undefined when n_samples < 2 or when the realized-PnL series
    is constant (zero-stddev).
    """

    model_config = ConfigDict(frozen=True)

    agent_id: str
    time: datetime
    hit_rate_30d: float | None = Field(default=None, ge=0.0, le=1.0)
    sharpe_30d: float | None = None
    pnl_30d: float
    n_samples: int = Field(default=0, ge=0)
