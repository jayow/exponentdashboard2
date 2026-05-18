"""Extract full tx payloads (jsonParsed) for sigs in raw_signatures
that don't yet exist in raw_helius_tx. Batched JSON-RPC, dual-key.

Anti-join makes this naturally idempotent — re-running fetches only the gap.

Stub — implementation in Phase 2.
"""
from __future__ import annotations
from .load import warehouse


def main() -> None:
    with warehouse() as _con:
        print("extract_transactions: stub — Phase 2")


if __name__ == "__main__":
    main()
