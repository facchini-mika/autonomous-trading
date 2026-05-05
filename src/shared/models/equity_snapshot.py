"""Per-cycle equity snapshot.

Single source of truth for ``equity_snapshots`` rows. The Lead writes one
row per cycle from ``execution.lead_bootstrap``; the Prometheus collector
in ``shared.observability.collector`` reads them on every scrape.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class EquitySnapshot(BaseModel):
    """One row of ``equity_snapshots``."""

    model_config = ConfigDict(frozen=True)

    time: datetime
    cycle_id: str
    equity_usd: float
    peak_equity_usd: float = Field(ge=0.0)
    gross_exposure_usd: float = Field(ge=0.0)
    duration_seconds: float = Field(ge=0.0)
