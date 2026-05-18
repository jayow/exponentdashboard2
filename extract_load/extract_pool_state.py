"""Extract daily AMM pool state — SY reserve, PT reserve, LP supply per market.

Reads each AMM pool account on-chain at one slot per day. From these reserves
we derive PT spot price and implied APY without needing tx-level reconstruction.

Writes raw_pool_state(snapshot_date, market_key, sy_reserve, pt_reserve, lp_supply).

Stub — implementation in Phase 2.
"""
from __future__ import annotations
from .load import warehouse


def main() -> None:
    with warehouse() as _con:
        print("extract_pool_state: stub — Phase 2")


if __name__ == "__main__":
    main()
