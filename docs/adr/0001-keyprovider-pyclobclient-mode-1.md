# ADR 0001 — KeyProvider × py-clob-client (Modus 1)

**Status:** Accepted (Phase 4a)
**Date:** 2026-05-02
**Decider:** Single operator (facchini-mika)

## Context

Phase 3 introduced `KeyProvider` Protocol with two methods:

```python
def sign_eip712(self, typed_data: dict[str, Any]) -> bytes: ...
def address(self) -> str: ...
```

This abstraction was designed to support multiple backends — encrypted local
file (Phase 4), AWS KMS (Phase 7+), and potentially HSM. The intent: signing
material never leaves the provider boundary.

Phase 4a needs to integrate with `py-clob-client`, the official Polymarket
Python CLOB SDK. py-clob-client expects to be constructed with the raw private
key and performs **all** EIP-712 order signing internally (it handles L1
auth/API-key derivation, L2 HMAC headers, salt, expiration encoding, and the
exact EIP-712 typed-data shape that the Polymarket exchange contracts expect).

Two integration paths:

- **Modus 1 — Raw-key passthrough.** Add a paket-private hook
  `KeyProviderLocalFile._unsafe_export_priv_key() -> bytes`. The adapter calls
  it once at construction and passes the raw key to `ClobClient(host, key=...)`.
  py-clob-client signs everything. The KeyProvider Protocol stays formally
  intact; the abstraction is partially fictitious for the local-file backend.

- **Modus 2 — Custom signer hook.** Build the EIP-712 typed-data structures
  ourselves (Polymarket `Order` struct, domain, salt, expiration), call
  `KeyProvider.sign_eip712()` for the signature, then inject the signed
  payload into py-clob-client at the right level. Requires reverse-engineering
  py-clob-client internals or building a parallel order-submission path.

## Decision

We adopt **Modus 1 for Phase 4** with a documented Phase 7 migration to
Modus 2 when AWS KMS is needed.

Rationale:
- Modus 1 is ~100 LOC of wrapper code; Modus 2 is several hundred LOC of
  EIP-712 reimplementation that risks order-format drift if Polymarket
  upgrades their exchange contracts.
- The local-file backend already holds the raw key in process memory after
  decrypt. Exporting it to py-clob-client is no additional security exposure.
- KMS signing keys never leave the HSM boundary, so Modus 2 is mandatory
  there. We accept the Phase-7 refactor cost in exchange for shipping Phase 4
  faster and with less custom signing code to maintain.

## Implementation

`shared/adapters/key_provider_localfile.py`:

```python
def _unsafe_export_priv_key(self) -> bytes:
    """Return the raw 32-byte private key (paket-private)."""
    self._unlock()
    return self._priv_key
```

`shared/adapters/polymarket.py`:

```python
def _make_client(self, factory):
    if not isinstance(self._key_provider, KeyProviderLocalFile):
        raise TypeError("Modus 1 requires KeyProviderLocalFile")
    priv_hex = self._key_provider._unsafe_export_priv_key().hex()
    return ClobClient(self._host, key=priv_hex, chain_id=self._chain_id)
```

The `_unsafe_` prefix and the `isinstance` guard are intentional: a Modus-2
KMS provider will not implement `_unsafe_export_priv_key`, forcing the
adapter to refactor instead of silently breaking.

## Consequences

**Negative:**
- The KeyProvider Protocol is formally a sound abstraction but practically a
  thin layer in Phase 4. New backends cannot just implement
  `sign_eip712()`/`address()` — they must also reason about whether py-clob-
  client can be replaced or augmented.
- Phase 7 (KMS) cannot reuse `PolymarketAdapter` as-is; it needs Modus 2.

**Positive:**
- Phase 4 ships with battle-tested EIP-712 logic from py-clob-client (1.2k
  GitHub stars, active maintenance).
- We do not own a parallel EIP-712 implementation that might drift.
- The migration path is explicit: when Phase 7 (KMS) lands, we delete the
  `_unsafe_export_priv_key` hook, build a Modus-2 signer that wraps the
  KeyProvider, and submit signed orders via a lower-level py-clob-client API
  or by calling Polymarket REST directly.

## Phase 7 migration plan

1. Implement `KeyProviderKMS` satisfying the Protocol (no raw-key export).
2. Add a Modus-2 code path in `PolymarketAdapter` selected when the provider
   does not implement `_unsafe_export_priv_key`.
3. Implement EIP-712 typed-data builders for the current Polymarket
   `Order`/`OrderResponse` schemas (cross-check against py-clob-client at the
   time of migration).
4. Validate against the Phase-6 sandbox-smoke fixtures.
5. Once Modus 2 passes the smoke and parity-check tests, route both
   `KeyProviderLocalFile` and `KeyProviderKMS` through Modus 2 and remove the
   Modus-1 branch.

## References

- `shared/adapters/key_provider.py` — Protocol definition (Phase 3).
- `shared/adapters/key_provider_localfile.py` — Phase-4 implementation.
- `shared/adapters/polymarket.py` — adapter consuming Modus 1.
- `docs/operations/sandbox_smoke.md` — operator runbook for Mainnet-Mini smoke.
