"""Async Helius JSON-RPC client.

Capabilities planned:
  - Dual-key round-robin (HELIUS_KEY_1 / HELIUS_KEY_2)
  - JSON-RPC batching (100 sigs per call)
  - Rate limiting + retry with exponential backoff
  - getSignaturesForAddress, getTransaction (jsonParsed)

Stub only — implementation lands in Phase 2.
"""
from __future__ import annotations


class HeliusClient:
    def __init__(self, keys: list[str], concurrency: int = 12):
        raise NotImplementedError("Phase 2: implement async batched client")

    async def get_signatures_for_address(self, address: str, before: str | None = None):
        raise NotImplementedError

    async def get_transactions(self, signatures: list[str]):
        raise NotImplementedError
