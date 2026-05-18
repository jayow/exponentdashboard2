"""Extract market universe from Exponent API + on-chain MarketThree accounts.

Writes to raw_markets with source='api' | 'onchain'. Downstream dbt model
stg_markets dedupes and unions them.

Stub — implementation in Phase 2.
"""
from __future__ import annotations
from .load import warehouse


def main() -> None:
    with warehouse() as _con:
        print("extract_markets: stub — Phase 2")


if __name__ == "__main__":
    main()
