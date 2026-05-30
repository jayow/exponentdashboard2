"""Daily snapshot of all v2 (XPBook) orderbook accounts.

One getProgramAccounts call → all 14 book accounts → decode each → write:
  - raw_v2_book_snapshots: 1 row per (date, book) with header + counts
  - raw_v2_orders_snapshots: 1 row per resting order

Idempotent for the day: re-runs DELETE today's rows then INSERT fresh.

Run: python -m extract_load.extract_v2_books
"""
from __future__ import annotations
import asyncio
import base64
import json
from datetime import datetime, timezone

import httpx
from rich import print as rprint

from .config import RPC_ENDPOINTS
from .load import warehouse
from .v2_book_decoder import (
    BOOK_DISC_HEX, decode_book, flatten_orders,
)


XPBOOK_PROGRAM = "XPBookgQTN2p8Yw1C2La35XkPMmZTCEYH77AdReVvK1"


async def fetch_book_accounts() -> list[dict]:
    """Pull all 14 book accounts (disc eb0d265b) from XPBook."""
    url = RPC_ENDPOINTS[0]
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, json={
            "jsonrpc": "2.0", "id": 1, "method": "getProgramAccounts",
            "params": [XPBOOK_PROGRAM, {"encoding": "base64"}],
        })
        resp.raise_for_status()
        accounts = resp.json()["result"]

    books = []
    for a in accounts:
        data_field = a["account"]["data"]
        data_b64 = data_field[0] if isinstance(data_field, list) else data_field
        raw = base64.b64decode(data_b64)
        if raw[:8].hex() == BOOK_DISC_HEX:
            books.append({"pubkey": a["pubkey"], "raw": raw})
    return books


async def run() -> dict:
    if not RPC_ENDPOINTS:
        raise RuntimeError("No SOLANA_RPC_URLS configured in .env")

    snap = datetime.now(timezone.utc).date()
    rprint(f"[cyan]Snapshotting v2 books for {snap}…[/cyan]")
    books = await fetch_book_accounts()
    rprint(f"  fetched {len(books)} book accounts")

    total_offers = 0
    with warehouse() as con:
        # Clear today's rows — single transaction with the inserts below
        con.execute("DELETE FROM raw_v2_book_snapshots WHERE snapshot_date = ?", [snap])
        con.execute("DELETE FROM raw_v2_orders_snapshots WHERE snapshot_date = ?", [snap])

        for book in books:
            decoded = decode_book(book["raw"])
            orders = flatten_orders(decoded)

            header = {
                "price_decimals": decoded.price_decimals,
                "expiration_ts":  decoded.expiration_ts,
                "yt_bal":         decoded.yt_bal,
                "sy_bal":         decoded.sy_bal,
                "pt_bal":         decoded.pt_bal,
                "yt_fee":         decoded.yt_fee,
                "sy_fee":         decoded.sy_fee,
                "pt_fee":         decoded.pt_fee,
                "staged_sy":      decoded.staged_sy,
                "ln_maker_fee":   decoded.ln_maker_fee,
                "ln_taker_fee":   decoded.ln_taker_fee,
                "threshold_amount": decoded.threshold_amount,
            }

            con.execute("""
                INSERT INTO raw_v2_book_snapshots
                  (snapshot_date, book_account, header_json,
                   n_offers, n_price_nodes, n_escrows, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """, [
                snap, book["pubkey"], json.dumps(header),
                len(decoded.offers),
                sum(1 for p in decoded.prices.values() if p.key > 0),
                len(decoded.escrows),
            ])

            if orders:
                con.executemany("""
                    INSERT INTO raw_v2_orders_snapshots (
                        snapshot_date, book_account, offer_idx,
                        owner_wallet, side, apy_rate, size_sy_atomic,
                        expiry_at, created_at,
                        type_flag, virtual_offer, fok
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, [(
                    snap, book["pubkey"], o.offer_idx,
                    o.owner_wallet, o.side, o.apy_rate, o.size_sy_atomic,
                    o.expiry_at, o.created_at,
                    o.type_flag, o.virtual_offer, o.fok,
                ) for o in orders])

            total_offers += len(orders)
            rprint(
                f"  [green]✓[/green] {book['pubkey'][:12]}…  "
                f"offers={len(orders):>4}  matures={decoded.expiration_ts}"
            )

    rprint(
        f"[green]Wrote v2 snapshot[/green]: "
        f"{len(books)} books, {total_offers} orders for {snap}"
    )
    return {"books": len(books), "orders": total_offers, "date": str(snap)}


def main() -> None:
    started = datetime.now(timezone.utc)
    rprint(f"[bold]extract_v2_books[/bold]  started {started.isoformat()}")
    asyncio.run(run())
    rprint(f"[bold]done[/bold]  elapsed {datetime.now(timezone.utc) - started}")


if __name__ == "__main__":
    main()
