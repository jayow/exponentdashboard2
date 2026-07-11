"""Snapshot authoritative on-chain token supply per Exponent mint.

WHY: int_mint_supplies_daily reconstructs supply by cumulatively summing
mint/burn deltas from indexed tx history. That drifts — any missed event
undercounts forever (USX SY was 31% low vs the real on-chain supply). The
chain stores the truth: the SPL Mint account's `supply` field, and the core
Vault account's `pt_supply`. This reads both, directly.

Method (all on-chain, verified against getTokenSupply + Vault.pt_supply):
  1. getProgramAccounts on the core program → every Vault (discriminator
     [211,8,232,43,2,152,117,119]); decode mint_sy(40), mint_yt(72),
     mint_pt(104), pt_supply(449) at fixed IDL offsets.
  2. getMultipleAccounts on all unique mints → decode SPL Mint supply
     (offset 36, u64 LE) + decimals (offset 44).
  3. Write one row per (mint, snapshot_date) with leg + supply_ui.

gPA is 429-blocked on datacenter IPs (see rpc-429 memory); run locally, or
on CI via the known-vaults fallback pattern once wired.
"""
from __future__ import annotations

import asyncio
import base64
import struct
from datetime import datetime, timezone

import base58
from rich import print as rprint

from .config import RPC_ENDPOINTS, EXPONENT_CORE_PROGRAM
from .solana_rpc_client import SolanaRpcClient
from .load import warehouse

# Core Vault account discriminator (from exponent_core IDL, account "Vault").
VAULT_DISCRIMINATOR = bytes([211, 8, 232, 43, 2, 152, 117, 119])

# Fixed byte offsets into the Vault account (8-byte disc + borsh, no padding).
# Verified 2026-07-11: mint_sy/mint_pt match known market mints, and
# pt_supply@449 matches on-chain PT mint supply exactly.
OFF_MINT_SY = 40
OFF_MINT_YT = 72
OFF_MINT_PT = 104
OFF_PT_SUPPLY = 449  # u64; after 3×Number(32B) + total_sy_in_escrow + sy_for_pt

# SPL Mint layout: COption authority (36) + supply u64 (36) + decimals u8 (44).
OFF_MINT_SUPPLY = 36
OFF_MINT_DECIMALS = 44


def _pk(raw: bytes, off: int) -> str:
    return base58.b58encode(raw[off:off + 32]).decode()


def _u64(raw: bytes, off: int) -> int:
    return struct.unpack("<Q", raw[off:off + 8])[0]


async def run() -> dict:
    if not RPC_ENDPOINTS:
        raise RuntimeError("No SOLANA_RPC_URLS configured in .env")
    snapshot_ts = datetime.now(timezone.utc)
    snapshot_date = snapshot_ts.date()
    rprint(f"[cyan]Snapshotting mint supplies on {snapshot_date}…[/cyan]")

    disc_b58 = base58.b58encode(VAULT_DISCRIMINATOR).decode()

    async with SolanaRpcClient(RPC_ENDPOINTS) as client:
        vaults = await client.get_program_accounts(
            EXPONENT_CORE_PROGRAM,
            filters=[{"memcmp": {"offset": 0, "bytes": disc_b58}}],
        )
        rprint(f"  decoded {len(vaults)} Vault accounts")

        # mint -> (leg, vault). SY mints are shared across maturities; keep the
        # first-seen vault (supply is per-mint, so it doesn't matter which).
        mint_leg: dict[str, tuple[str, str]] = {}
        vault_pt_supply: dict[str, int] = {}
        for a in vaults:
            raw = base64.b64decode(a["account"]["data"][0])
            if len(raw) < OFF_PT_SUPPLY + 8:
                continue
            vault_pk = a["pubkey"]
            sy, yt, pt = _pk(raw, OFF_MINT_SY), _pk(raw, OFF_MINT_YT), _pk(raw, OFF_MINT_PT)
            mint_leg.setdefault(sy, ("SY", vault_pk))
            mint_leg.setdefault(yt, ("YT", vault_pk))
            mint_leg.setdefault(pt, ("PT", vault_pk))
            vault_pt_supply[pt] = _u64(raw, OFF_PT_SUPPLY)

        mints = list(mint_leg.keys())
        rprint(f"  fetching supply for {len(mints)} unique mints…")

        rows: list[tuple] = []
        pt_mismatch = 0
        for i in range(0, len(mints), 100):  # getMultipleAccounts cap
            chunk = mints[i:i + 100]
            infos = await client.get_multiple_accounts(chunk)
            for mint, info in zip(chunk, infos):
                if info is None:
                    continue  # mint closed / not found
                data_field = info["data"]
                data_b64 = data_field[0] if isinstance(data_field, list) else data_field
                raw = base64.b64decode(data_b64)
                if len(raw) < OFF_MINT_DECIMALS + 1:
                    continue
                supply_raw = _u64(raw, OFF_MINT_SUPPLY)
                decimals = raw[OFF_MINT_DECIMALS]
                supply_ui = supply_raw / (10 ** decimals)
                leg, vault_pk = mint_leg[mint]
                # Sanity: PT mint supply should match Vault.pt_supply.
                if leg == "PT" and mint in vault_pt_supply:
                    if abs(vault_pt_supply[mint] - supply_raw) > 1:
                        pt_mismatch += 1
                rows.append((
                    snapshot_date, mint, leg, vault_pk,
                    supply_raw, decimals, supply_ui, snapshot_ts,
                ))

    if pt_mismatch:
        rprint(f"  [yellow]note: {pt_mismatch} PT mint supplies differ from Vault.pt_supply "
               f"(timing/decode drift — mint supply is authoritative)[/yellow]")

    with warehouse() as con:
        con.execute("DELETE FROM raw_mint_supplies WHERE snapshot_date = ?", [snapshot_date])
        if rows:
            con.executemany(
                "INSERT INTO raw_mint_supplies "
                "(snapshot_date, mint, leg, vault, supply_raw, decimals, supply_ui, snapshot_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    n_sy = sum(1 for r in rows if r[2] == "SY")
    rprint(f"[green]Wrote {len(rows)} mint supplies ({n_sy} SY)[/green]")
    return {"mints": len(rows), "sy": n_sy, "snapshot_date": str(snapshot_date)}


def main() -> None:
    started = datetime.now(timezone.utc)
    rprint(f"[bold]extract_mint_supplies[/bold]  started {started.isoformat()}")
    asyncio.run(run())
    rprint(f"[bold]done[/bold]  elapsed {datetime.now(timezone.utc) - started}")


if __name__ == "__main__":
    main()
