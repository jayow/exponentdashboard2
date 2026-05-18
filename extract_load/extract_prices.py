"""Extract historical + live USD prices for tracked base assets.

Pricing strategy (on-chain-first):
  - Historical: Pyth Hermes HTTP API (free, serves on-chain Pyth oracle data over HTTP)
  - Live:       getAccountInfo on Pyth price accounts via Solana RPC
  - LST→base:   read stake pool accounts on-chain (see extract_lst_rates.py).
                Combined: lst_usd = lst_per_base * base_usd_from_pyth

Tokens without a Pyth feed (long-tail underlyings) get NULL price_usd.
Downstream marts skip USD aggregation for those markets; native-unit
series still render.

Stub — implementation in Phase 2.
"""
from __future__ import annotations
from .load import warehouse


def main() -> None:
    with warehouse() as _con:
        print("extract_prices: stub — Phase 2")


if __name__ == "__main__":
    main()
