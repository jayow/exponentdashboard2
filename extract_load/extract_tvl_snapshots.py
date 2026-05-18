"""Extract daily TVL snapshots — read each market PDA's underlying balance on-chain.

For every market in dim_markets, sample the underlying-token balance held by
the market PDA at one slot per day (close-of-day in UTC). Write rows to
raw_tvl_snapshots keyed by (snapshot_date, market_key).

Why this instead of the API: API TVL is a derived number — chain balance is
the truth. If the API ever drifts, on-chain wins.

Stub — implementation in Phase 2.
"""
from __future__ import annotations
from .load import warehouse


def main() -> None:
    with warehouse() as _con:
        print("extract_tvl_snapshots: stub — Phase 2")


if __name__ == "__main__":
    main()
