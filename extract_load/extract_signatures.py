"""Extract tx signatures per market/SY mint via getSignaturesForAddress.

Cursor lives in DuckDB (raw_signatures has a max(block_time) per address),
so this is naturally incremental — new run picks up where the last left off.

Stub — implementation in Phase 2.
"""
from __future__ import annotations
from .load import warehouse


def main() -> None:
    with warehouse() as _con:
        print("extract_signatures: stub — Phase 2")


if __name__ == "__main__":
    main()
