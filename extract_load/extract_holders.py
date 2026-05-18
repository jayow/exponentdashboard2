"""Extract current holder snapshots per PT/YT/LP mint via getProgramAccounts.

Writes one row per (snapshot_date, mint, owner). Older snapshots retained.

Stub — implementation in Phase 2.
"""
from __future__ import annotations
from .load import warehouse


def main() -> None:
    with warehouse() as _con:
        print("extract_holders: stub — Phase 2")


if __name__ == "__main__":
    main()
