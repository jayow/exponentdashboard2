"""Extract holder snapshots per PT/YT/LP mint via getProgramAccounts.

For every (PT, YT, LP) mint in dim_markets, query the SPL Token Program
for all token accounts holding that mint, decode owner + balance, write
to raw_holders.

Snapshot semantics: one snapshot per UTC date. Re-running on the same
date overwrites that date's rows. Older snapshots retained — concentration
trends can be computed across snapshots later.

Each token account is 165 bytes:
  [0..32)   mint pubkey
  [32..64)  owner pubkey
  [64..72)  amount (u64 LE, raw — divide by 10**decimals for UI units)
"""
from __future__ import annotations
import asyncio
import base64
import base58
import struct
from datetime import datetime, timezone
from typing import Iterable

import duckdb
from rich import print as rprint
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

from .config import RPC_ENDPOINTS
from .solana_rpc_client import SolanaRpcClient
from .load import warehouse


TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


def _mints_to_snapshot(con: duckdb.DuckDBPyConnection) -> list[tuple[str, str, str, int]]:
    """Return (mint, market_key, leg, decimals) for every PT/YT/LP mint.

    Decimals taken from raw_token_metadata when available, falls back to
    underlying_decimals from dim_markets (correct for PT/YT which mirror SY).
    """
    rows = con.execute(
        """
        WITH legs AS (
          SELECT market_key, pt_mint AS mint, 'PT' AS leg FROM main_core.dim_markets WHERE pt_mint IS NOT NULL
          UNION ALL
          SELECT market_key, yt_mint AS mint, 'YT' AS leg FROM main_core.dim_markets WHERE yt_mint IS NOT NULL
          UNION ALL
          SELECT market_key, lp_mint AS mint, 'LP' AS leg FROM main_core.dim_markets WHERE lp_mint IS NOT NULL
          UNION ALL
          -- Tranching LPs (sr/jr risk-tranche shares). Use latest snapshot from
          -- raw_tranche_states; seed_id is the per-vault tag (e.g. 'onycC101').
          SELECT t.seed_id AS market_key, t.mint_lp_senior AS mint, 'TRANCHE_SR' AS leg
          FROM main.raw_tranche_states t
          WHERE t.snapshot_date = (SELECT MAX(snapshot_date) FROM main.raw_tranche_states)
            AND t.mint_lp_senior IS NOT NULL
          UNION ALL
          SELECT t.seed_id AS market_key, t.mint_lp_junior AS mint, 'TRANCHE_JR' AS leg
          FROM main.raw_tranche_states t
          WHERE t.snapshot_date = (SELECT MAX(snapshot_date) FROM main.raw_tranche_states)
            AND t.mint_lp_junior IS NOT NULL
        )
        SELECT
            l.mint,
            l.market_key,
            l.leg,
            COALESCE(tm.decimals, dm.underlying_decimals, 9) AS decimals
        FROM legs l
        LEFT JOIN main.raw_token_metadata tm ON tm.mint = l.mint
        LEFT JOIN main_core.dim_markets dm  ON dm.market_key = l.market_key
        """
    ).fetchall()
    return [(r[0], r[1], r[2], int(r[3])) for r in rows]


def _decode_token_account(data_b64: str) -> tuple[str, int] | None:
    """Decode an SPL token account: (owner_pubkey_base58, raw_amount).
    Returns None if data is malformed or amount is zero."""
    try:
        data = base64.b64decode(data_b64)
        if len(data) < 72:
            return None
        owner = base58.b58encode(data[32:64]).decode()
        amount = struct.unpack("<Q", data[64:72])[0]
        if amount == 0:
            return None
        return owner, amount
    except Exception:
        return None


def _chunked(seq: list, n: int) -> Iterable[list]:
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


async def _fetch_holders_for_mint(
    client: SolanaRpcClient, mint: str
) -> list[tuple[str, int]]:
    """Return (owner, raw_amount) for every non-zero token account of `mint`."""
    # Filter: account data size == 165 AND mint at offset 0 == this mint
    filters = [
        {"dataSize": 165},
        {"memcmp": {"offset": 0, "bytes": mint}},
    ]
    accts = await client.get_program_accounts(TOKEN_PROGRAM, filters=filters)
    out: list[tuple[str, int]] = []
    for a in accts:
        data_field = a["account"]["data"]
        data_b64 = data_field[0] if isinstance(data_field, list) else data_field
        decoded = _decode_token_account(data_b64)
        if decoded:
            out.append(decoded)
    return out


async def run() -> dict:
    if not RPC_ENDPOINTS:
        raise RuntimeError("No SOLANA_RPC_URLS configured in .env")

    snapshot_date = datetime.now(timezone.utc).date()

    with warehouse() as con:
        mints = _mints_to_snapshot(con)
    rprint(f"[cyan]Snapshotting holders for {len(mints)} mints on {snapshot_date}…[/cyan]")

    n_total_holders = 0
    n_mints_with_data = 0

    async with SolanaRpcClient(RPC_ENDPOINTS, concurrency_per_endpoint=4) as client:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            transient=True,
        ) as bar:
            task = bar.add_task("fetching", total=len(mints))
            # Bound concurrency so we don't hammer Helius
            sem = asyncio.Semaphore(8)

            async def one(mint: str, market_key: str, leg: str, decimals: int):
                nonlocal n_total_holders, n_mints_with_data
                async with sem:
                    try:
                        rows = await _fetch_holders_for_mint(client, mint)
                    except Exception as e:
                        rprint(f"  [yellow]warn[/yellow] {market_key}/{leg} {mint[:8]}… → {e}")
                        bar.update(task, advance=1)
                        return mint, market_key, leg, decimals, []
                    bar.update(task, advance=1)
                    return mint, market_key, leg, decimals, rows

            results = await asyncio.gather(*[one(*m) for m in mints])

        # Reopen warehouse for write (we closed read-only earlier)
        with warehouse() as con:
            # Clear today's snapshot for these mints (idempotent re-run)
            mint_list = [r[0] for r in results]
            if mint_list:
                placeholders = ",".join(["?"] * len(mint_list))
                con.execute(
                    f"DELETE FROM raw_holders WHERE snapshot_date = ? AND mint IN ({placeholders})",
                    [snapshot_date, *mint_list],
                )
            # Aggregate per (mint, owner) — a wallet can hold multiple token
            # accounts of the same mint, and we want a single row per holder.
            agg: dict[tuple[str, str], float] = {}
            mint_decimals: dict[str, int] = {}
            for mint, market_key, leg, decimals, holders in results:
                if not holders:
                    continue
                n_mints_with_data += 1
                mint_decimals[mint] = decimals
                for owner, raw_amount in holders:
                    agg[(mint, owner)] = agg.get((mint, owner), 0) + raw_amount
            rows_to_insert = [
                (snapshot_date, mint, owner, raw_amount / (10 ** mint_decimals[mint]))
                for (mint, owner), raw_amount in agg.items()
            ]
            n_total_holders = len(rows_to_insert)
            if rows_to_insert:
                con.executemany(
                    "INSERT INTO raw_holders (snapshot_date, mint, owner, amount) VALUES (?, ?, ?, ?)",
                    rows_to_insert,
                )

    rprint(
        f"[green]Done[/green]  mints={len(mints)}  with_holders={n_mints_with_data}  "
        f"total_holder_rows={n_total_holders}"
    )
    return {
        "mints": len(mints),
        "mints_with_holders": n_mints_with_data,
        "holder_rows": n_total_holders,
        "snapshot_date": str(snapshot_date),
    }


def main() -> None:
    started = datetime.now(timezone.utc)
    rprint(f"[bold]extract_holders[/bold]  started {started.isoformat()}")
    asyncio.run(run())
    rprint(f"[bold]done[/bold]  elapsed {datetime.now(timezone.utc) - started}")


if __name__ == "__main__":
    main()
