"""Extract holder snapshots for Strategy Vault LP mints via getProgramAccounts.

For every strategy vault in the LATEST raw_strategy_vault_states snapshot,
query the SPL Token Program for all token accounts holding that vault's
mint_lp, decode owner + balance, filter out program-internal PDAs
(token_lp_escrow, fee_treasury), and write to raw_strategy_vault_holders.

Snapshot semantics: one snapshot per UTC date. Re-running on the same
date overwrites that date's rows (idempotent per snapshot_date).

Each token account is 165 bytes:
  [0..32)   mint pubkey
  [32..64)  owner pubkey
  [64..72)  amount (u64 LE, raw atom units)
"""
from __future__ import annotations
import asyncio
import base64
import base58
import struct
from datetime import datetime, timezone

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


def _vaults_to_snapshot(
    con: duckdb.DuckDBPyConnection,
) -> list[tuple[str, str, str, str]]:
    """Return (vault, mint_lp, token_lp_escrow, fee_treasury) for every
    strategy vault in the LATEST raw_strategy_vault_states snapshot."""
    rows = con.execute(
        """
        SELECT
            vault,
            mint_lp,
            token_lp_escrow,
            fee_treasury
        FROM main.raw_strategy_vault_states
        WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM main.raw_strategy_vault_states)
          AND mint_lp IS NOT NULL
        """
    ).fetchall()
    return [(r[0], r[1], r[2], r[3]) for r in rows]


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


async def _fetch_holders_for_mint(
    client: SolanaRpcClient, mint_lp: str
) -> list[tuple[str, int]]:
    """Return (owner, raw_amount) for every non-zero token account of `mint_lp`."""
    filters = [
        {"dataSize": 165},
        {"memcmp": {"offset": 0, "bytes": mint_lp}},
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

    snapshot_ts = datetime.now(timezone.utc)
    snapshot_date = snapshot_ts.date()

    with warehouse() as con:
        vaults = _vaults_to_snapshot(con)
    rprint(
        f"[cyan]Snapshotting strategy-vault holders for {len(vaults)} vaults "
        f"on {snapshot_date}…[/cyan]"
    )

    n_total_holder_rows = 0
    n_vaults_with_holders = 0

    async with SolanaRpcClient(RPC_ENDPOINTS, concurrency_per_endpoint=4) as client:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            transient=True,
        ) as bar:
            task = bar.add_task("fetching", total=len(vaults))
            sem = asyncio.Semaphore(8)

            async def one(vault: str, mint_lp: str, escrow: str, treasury: str):
                async with sem:
                    try:
                        rows = await _fetch_holders_for_mint(client, mint_lp)
                    except Exception as e:
                        rprint(
                            f"  [yellow]warn[/yellow] vault={vault[:8]}… "
                            f"mint_lp={mint_lp[:8]}… → {e}"
                        )
                        bar.update(task, advance=1)
                        return vault, mint_lp, escrow, treasury, None
                    bar.update(task, advance=1)
                    return vault, mint_lp, escrow, treasury, rows

            results = await asyncio.gather(*[one(*v) for v in vaults])
            # holders=None marks a failed fetch — keep that vault's previously
            # captured rows for today instead of wiping them below.
            results = [r for r in results if r[4] is not None]

        with warehouse() as con:
            # Idempotent per snapshot_date: clear today's rows for these vaults
            vault_list = [r[0] for r in results]
            if vault_list:
                placeholders = ",".join(["?"] * len(vault_list))
                con.execute(
                    f"DELETE FROM raw_strategy_vault_holders "
                    f"WHERE snapshot_date = ? AND vault IN ({placeholders})",
                    [snapshot_date, *vault_list],
                )

            # Aggregate per (vault, holder). A wallet can hold multiple token
            # accounts of the same LP mint — collapse to one row per holder.
            agg: dict[tuple[str, str, str], int] = {}
            for vault, mint_lp, escrow, treasury, holders in results:
                if not holders:
                    continue
                internal = {escrow, treasury}
                real_holders = [(o, a) for o, a in holders if o not in internal]
                if real_holders:
                    n_vaults_with_holders += 1
                for owner, raw_amount in real_holders:
                    key = (vault, mint_lp, owner)
                    agg[key] = agg.get(key, 0) + raw_amount

            rows_to_insert = [
                (
                    vault,
                    mint_lp,
                    holder,
                    lp_amount,
                    snapshot_date,
                    snapshot_ts,
                    None,  # slot — not tracked at this level
                )
                for (vault, mint_lp, holder), lp_amount in agg.items()
            ]
            n_total_holder_rows = len(rows_to_insert)
            if rows_to_insert:
                con.executemany(
                    "INSERT INTO raw_strategy_vault_holders "
                    "(vault, mint_lp, holder, lp_amount, snapshot_date, snapshot_ts, slot) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    rows_to_insert,
                )

    rprint(
        f"[green]Done[/green]  vaults={len(vaults)}  "
        f"with_holders={n_vaults_with_holders}  "
        f"total_holder_rows={n_total_holder_rows}"
    )
    return {
        "vaults": len(vaults),
        "vaults_with_holders": n_vaults_with_holders,
        "holder_rows": n_total_holder_rows,
        "snapshot_date": str(snapshot_date),
    }


def main() -> None:
    started = datetime.now(timezone.utc)
    rprint(f"[bold]extract_strategy_vault_holders[/bold]  started {started.isoformat()}")
    asyncio.run(run())
    rprint(f"[bold]done[/bold]  elapsed {datetime.now(timezone.utc) - started}")


if __name__ == "__main__":
    main()
