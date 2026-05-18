"""Extract historical USD prices for tracked underlying tokens.

Sources: Pyth/Birdeye/CoinGecko (TBD). Writes daily series to raw_prices.

Stub — implementation in Phase 2.
"""
from __future__ import annotations
from .load import warehouse


def main() -> None:
    with warehouse() as _con:
        print("extract_prices: stub — Phase 2")


if __name__ == "__main__":
    main()
