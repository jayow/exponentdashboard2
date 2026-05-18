"""Extract daily LST exchange rates from stake pool / wrapper accounts on-chain.

For each LST underlying we care about (jitoSOL, mSOL, bSOL, fragSOL, INF, ...),
read the canonical state account that publishes its exchange rate to the base
asset (usually SOL).

Combined with Pyth base price in stg_prices, this gives the LST's USD price:
  lst_usd = lst_per_base * base_usd

Writes raw_lst_rates(snapshot_date, lst_mint, base_mint, lst_per_base).

Stub — implementation in Phase 2.
"""
from __future__ import annotations
from .load import warehouse


def main() -> None:
    with warehouse() as _con:
        print("extract_lst_rates: stub — Phase 2")


if __name__ == "__main__":
    main()
