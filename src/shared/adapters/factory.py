"""Factory: pick concrete adapters based on `Settings`.

Stream D injects a `FakeAdapter` directly in tests; production code calls
`make_adapter(settings)`. KMS provider currently raises NotImplementedError —
Phase 7 will implement it (see ADR 0001 for the migration path).
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, cast

from shared.adapters.key_provider_localfile import KeyProviderLocalFile
from shared.adapters.paper_trading import PaperTradingAdapter
from shared.adapters.polymarket import PolymarketAdapter
from shared.db import get_session

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from contextlib import AbstractContextManager
    from uuid import UUID

    from sqlalchemy.orm import Session

    from shared.adapters.key_provider import KeyProvider
    from shared.adapters.prediction_market import PredictionMarketAdapter
    from shared.config.settings import Settings


def make_key_provider(settings: Settings) -> KeyProvider:
    kind = cast("str", settings.KEY_PROVIDER)
    if kind == "encrypted_file":
        return KeyProviderLocalFile(path=settings.KEY_PROVIDER_PATH)
    if kind == "aws_kms":
        msg = "AWS KMS KeyProvider is post-MVP (Phase 7); see docs/adr/0001"
        raise NotImplementedError(msg)
    msg = f"Unknown KEY_PROVIDER: {kind}"
    raise ValueError(msg)


def make_adapter(
    settings: Settings,
    *,
    decision_id_provider: Callable[[], UUID] | None = None,
    session_factory: Callable[[], AbstractContextManager[Session]] | None = None,
) -> PredictionMarketAdapter:
    """Return the concrete adapter for the configured trading mode.

    - `paper` → `PaperTradingAdapter(live=PolymarketAdapter)`.
    - `real_capital` → `PolymarketAdapter`.

    `decision_id_provider` and `session_factory` are consumed only in `paper`
    mode; defaults bind to the trading_cycle Postgres role and a placeholder
    that raises until Stream D wires a ContextVar.
    """
    key = make_key_provider(settings)
    live = PolymarketAdapter(
        key_provider=key,
        host=settings.POLYMARKET_HOST,
        chain_id=settings.POLYGON_CHAIN_ID,
        default_fee_rate_bps=settings.FEE_RATE_BPS,
    )

    mode = cast("str", settings.TRADING_MODE)
    if mode == "real_capital":
        return live
    if mode == "paper":
        return PaperTradingAdapter(
            live_adapter=live,
            session_factory=session_factory or _default_session_factory,
            decision_id_provider=decision_id_provider or _missing_decision_id,
            fee_rate_bps=settings.FEE_RATE_BPS,
        )
    msg = f"Unknown TRADING_MODE: {mode}"
    raise ValueError(msg)


@contextmanager
def _default_session_factory() -> Iterator[Session]:
    with get_session("trading_cycle") as session:
        yield session


def _missing_decision_id() -> UUID:
    msg = (
        "PaperTradingAdapter.place_order called without an active decision_id; "
        "Stream D must set the decision_id ContextVar before submitting orders"
    )
    raise RuntimeError(msg)
