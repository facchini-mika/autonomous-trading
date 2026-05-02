"""KeyProvider Protocol — abstracts EIP-712 signing across key backends.

Implementations land in Phase 4 Stream A:
- `key_provider_localfile.py` — encrypted-at-rest local file (default).
- `key_provider_kms.py` — AWS KMS (post-MVP).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class KeyProvider(Protocol):
    """Abstract signer used by the prediction-market adapter."""

    def sign_eip712(self, typed_data: dict[str, Any]) -> bytes:
        """Sign an EIP-712 typed-data payload and return the raw signature."""
        ...

    def address(self) -> str:
        """Return the 0x-prefixed signer address."""
        ...
