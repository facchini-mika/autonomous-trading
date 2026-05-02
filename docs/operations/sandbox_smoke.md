# Operator Runbook — Mainnet-Mini Sandbox Smoke

**When to run:** Once, manually, before the first `real_capital` cycle (Phase 6).

**Why:** Polymarket has no public testnet. CI uses only mocked py-clob-client
responses; this is the single end-to-end verification that EIP-712 signing,
token approvals, and order submission round-trip correctly against the live
exchange.

**Risk:** ~$5 USDC. Acceptable: bug surfaces here, not in production.

## Prerequisites

- A funded Polygon wallet with at least:
  - **6 USDC** (5 for the test order + ~1 buffer for gas estimation slop)
  - **0.5 MATIC** for gas
- The wallet's private key encrypted via `KeyProviderLocalFile.create_new()`
  and stored at `Settings.KEY_PROVIDER_PATH`.
- `WALLET_PASSPHRASE` env var set, or be ready to type the passphrase.
- A Polygon RPC URL (Polymarket's `https://polygon-rpc.com` is fine).

## Step 1 — Approve tokens (one-shot)

```bash
uv run python -m infra.scripts.approve_tokens
```

Verifies USDC + Conditional Token approvals to both the CTF Exchange and
NegRisk Exchange. Note the four tx hashes for the AUDIT_LOG entry below.

## Step 2 — Pick a target market

Choose a high-liquidity, low-stake market on https://polymarket.com (e.g.
a 1-2 day weather prediction). Note the market `condition_id` from the URL
or the markets API. Record current YES bid/ask.

## Step 3 — Place a $5 marketable-limit order

Open a Python REPL (or write a short script):

```python
from shared.adapters.factory import make_adapter
from shared.config.settings import Settings
from shared.models import Order

settings = Settings(TRADING_MODE="real_capital")
adapter = make_adapter(settings)

order = Order(
    market_id="<token_id_for_yes>",
    side="yes",
    size=10.0,           # 10 shares
    price=0.50,          # well above current mid for safety, marketable-limit
    notional_usd=5.0,
    idempotency_key="smoke-001",
)
result = adapter.place_order(order)
print(result)
```

Expected: `OrderResult(status="filled", broker_order_id=..., fill_price=...)`.

## Step 4 — Verify on-chain

- Confirm `broker_order_id` resolves on https://polymarket.com (your trades
  page).
- Verify the fill tx on https://polygonscan.com.
- Sum: total cost (USDC out) + gas should match `notional_usd + fees +
  gas_estimate` within $0.05.

## Step 5 — Cancel/cleanup (optional)

If the order is partial or open:
```python
adapter.cancel_order(result.broker_order_id)
```

Otherwise wait for resolution and let outcome_ingestion (Phase 4b) settle PnL.

## Step 6 — AUDIT_LOG entry

Append to `AUDIT_LOG.md`:

```markdown
### 2026-MM-DD — Sandbox Smoke (Phase 6 prep)

- Wallet: 0x...
- USDC->CTF Exchange approve tx: 0x...
- USDC->NegRisk Exchange approve tx: 0x...
- CTF->CTF Exchange setApprovalForAll tx: 0x...
- CTF->NegRisk Exchange setApprovalForAll tx: 0x...
- Market: <slug> (condition_id 0x...)
- Order: 10 shares YES @ 0.50, notional $5
- broker_order_id: ...
- Fill tx: 0x...
- Observed: status=filled, fill_price=<X>, fees=<Y>
- Operator review: I confirm the order amount, market, side, and signing
  produced the expected on-chain effect. No surprise behavior. Safe to
  flip TRADING_MODE=real_capital for Phase 6 first cycle.
```

## Failure modes

| Symptom | Likely cause | Action |
|---|---|---|
| `WalletDecryptError` | wrong passphrase | re-enter; verify `KEY_PROVIDER_PATH` |
| `PolymarketPermanentError: 401` | API creds not derived | check L1 → L2 derivation logged |
| `PolymarketPermanentError: 400 invalid signature` | EIP-712 mismatch — **STOP** | do not retry; investigate py-clob-client version vs Polymarket exchange contract version |
| approval tx fails (out-of-gas) | gas-estimate too low | bump gasPrice; re-run approve_tokens |
| `OrderResult(status="rejected")` | balance / approval not propagated | re-check approvals; ensure USDC sufficient |
| order placed but not visible on web UI | indexer lag | wait 30s, refresh; verify on Polygonscan |
