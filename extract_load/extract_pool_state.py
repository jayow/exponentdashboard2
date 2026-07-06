"""Discover Exponent AMM pool accounts and snapshot their on-chain mapping.

Architecture (empirically derived):
  - Exponent's AMM lives in a separate program:
    XP1BRLn8eCYSygrd8er5P4GKdzqKbC3DLoSsS5UYVZy
  - One pool account per SY token (= per underlying type), NOT per market.
  - Pool account = `fe938810a3cb625d` Anchor discriminator, sizes 380-868 bytes.
  - SY mint sits at offset 8 (right after the discriminator).
  - The pool account is the AUTHORITY of an SPL token account that holds the
    SY reserves for that pool. Pool PT reserves are held in a separate token
    account whose authority is the MarketThree account (dim_markets.amm_pool).
  - Markets that share an underlying share an SY pool — sibling markets are
    apportioned downstream by PT-supply weight.

This extractor persists the (snapshot_date, sy_pool_account, sy_mint) mapping
into raw_pool_state. Reserves themselves are computed in dbt by joining
sy_pool_account back to stg_token_changes (we already crawl all the relevant
mints).
"""
from __future__ import annotations
import asyncio
import base64
import struct
from datetime import datetime, timezone

import base58
from rich import print as rprint

from .config import RPC_ENDPOINTS
from .solana_rpc_client import SolanaRpcClient
from .load import warehouse


EXPONENT_AMM_PROGRAM = "XP1BRLn8eCYSygrd8er5P4GKdzqKbC3DLoSsS5UYVZy"
POOL_DISCRIMINATOR = bytes.fromhex("fe938810a3cb625d")

# Pool's `index` field (= current SY exchange rate). Per the generic_sy IDL,
# this is a 32-byte PreciseNumber starting at offset 97. The first u64 limb
# carries the integer part scaled by 1e12 — matches the API's syExchangeRate
# (verified: 1117213400000 / 1e12 = 1.117213, API reports 1.11721404).
SY_RATE_OFFSET = 97
SY_RATE_SCALE = 1e12


async def run() -> dict:
    if not RPC_ENDPOINTS:
        raise RuntimeError("No SOLANA_RPC_URLS configured in .env")
    snapshot_date = datetime.now(timezone.utc).date()
    rprint(f"[cyan]Discovering AMM pool accounts on {snapshot_date}…[/cyan]")

    async with SolanaRpcClient(RPC_ENDPOINTS) as client:
        accts = await client.get_program_accounts(
            EXPONENT_AMM_PROGRAM,
            filters=[
                {"memcmp": {"offset": 0, "bytes": base58.b58encode(POOL_DISCRIMINATOR).decode()}}
            ],
        )

    rows: list[tuple] = []
    n_with_rate = 0
    for a in accts:
        data_field = a["account"]["data"]
        data_b64 = data_field[0] if isinstance(data_field, list) else data_field
        data = base64.b64decode(data_b64)
        if len(data) < 40:
            continue
        sy_mint = base58.b58encode(data[8:40]).decode()
        # Pool's `index` field — first u64 limb of the 32-byte PreciseNumber.
        rate = None
        if len(data) >= SY_RATE_OFFSET + 8:
            rate_raw = struct.unpack_from("<Q", data, SY_RATE_OFFSET)[0]
            if rate_raw > 0:
                rate = rate_raw / SY_RATE_SCALE
                n_with_rate += 1
        rows.append((snapshot_date, a["pubkey"], sy_mint, rate))
    rprint(f"  found {len(rows)} pool accounts ({n_with_rate} with sy_exchange_rate)")

    with warehouse() as con:
        con.execute("DELETE FROM raw_pool_state WHERE snapshot_date = ?", [snapshot_date])
        if rows:
            con.executemany(
                "INSERT INTO raw_pool_state (snapshot_date, pool_account, sy_mint, current_sy_exchange_rate) VALUES (?, ?, ?, ?)",
                rows,
            )

    rprint(f"[green]Wrote {len(rows)} pool ↔ sy_mint mappings[/green]")
    return {"pools": len(rows), "snapshot_date": str(snapshot_date)}


def main() -> None:
    started = datetime.now(timezone.utc)
    rprint(f"[bold]extract_pool_state[/bold]  started {started.isoformat()}")
    asyncio.run(run())
    rprint(f"[bold]done[/bold]  elapsed {datetime.now(timezone.utc) - started}")


if __name__ == "__main__":
    main()
