"""Extract user YT and LP positions from Exponent's custom Anchor accounts.

YT positions are NOT held in SPL token accounts — they live in
`YieldTokenPosition` Anchor accounts under the Exponent core program. The
SPL YT-mint token accounts are all owned by program PDAs (pool vaults).
LP positions follow the same pattern via `LpPosition` accounts. This is
the only correct way to identify the user wallet behind a YT/LP holding.

Account layout (both YieldTokenPosition and LpPosition):
  [0..8)    8-byte Anchor discriminator
  [8..40)   owner pubkey                ← the user wallet
  [40..72)  vault pubkey                ← maps to dim_markets.vault → market_key
  [72..80)  amount (u64 LE, raw — divide by underlying_decimals for UI units)
  [80...]   tracker / emissions data (account size varies by emission count)

Discriminators (SHA-256 of "account:<Name>" truncated to 8 bytes):
  YieldTokenPosition: e35c92311d55475e — sizes 124, 164, 204 (0/1/2 emissions)
  LpPosition:         69f125c8e002fc5a — sizes 128, 168, 208 (1+0/1+1/1+2 trackers)
"""
from __future__ import annotations
import asyncio
import base64
import struct
from datetime import datetime, timezone

import base58
import duckdb
from rich import print as rprint

from .config import RPC_ENDPOINTS, EXPONENT_CORE_PROGRAM
from .solana_rpc_client import SolanaRpcClient
from .load import warehouse


YT_DISC = bytes.fromhex("e35c92311d55475e")
LP_DISC = bytes.fromhex("69f125c8e002fc5a")
YT_SIZES = (124, 164, 204)
LP_SIZES = (128, 168, 208)


def _decode_position(data_b64: str) -> tuple[str, str, int] | None:
    try:
        data = base64.b64decode(data_b64)
        if len(data) < 80:
            return None
        owner = base58.b58encode(data[8:40]).decode()
        vault = base58.b58encode(data[40:72]).decode()
        amount = struct.unpack("<Q", data[72:80])[0]
        return owner, vault, amount
    except Exception:
        return None


async def _fetch(client: SolanaRpcClient, disc: bytes, sizes: tuple[int, ...]) -> list[dict]:
    """Returns list of {'position_account','owner','vault','amount_raw'}."""
    out: list[dict] = []
    disc_b58 = base58.b58encode(disc).decode()
    for size in sizes:
        accts = await client.get_program_accounts(
            EXPONENT_CORE_PROGRAM,
            filters=[{"dataSize": size}, {"memcmp": {"offset": 0, "bytes": disc_b58}}],
        )
        for a in accts:
            data_field = a["account"]["data"]
            data_b64 = data_field[0] if isinstance(data_field, list) else data_field
            decoded = _decode_position(data_b64)
            if decoded is None:
                continue
            owner, vault, amount = decoded
            out.append({
                "position_account": a["pubkey"],
                "owner": owner,
                "vault": vault,
                "amount_raw": amount,
            })
        rprint(f"    size={size}: {len(accts)} accounts")
    return out


async def run() -> dict:
    if not RPC_ENDPOINTS:
        raise RuntimeError("No SOLANA_RPC_URLS configured in .env")

    snapshot_date = datetime.now(timezone.utc).date()
    rprint(f"[cyan]Snapshotting Anchor positions for {snapshot_date}…[/cyan]")

    async with SolanaRpcClient(RPC_ENDPOINTS) as client:
        rprint("  YieldTokenPosition:")
        yt = await _fetch(client, YT_DISC, YT_SIZES)
        rprint(f"  → {len(yt)} YT positions")
        rprint("  LpPosition:")
        lp = await _fetch(client, LP_DISC, LP_SIZES)
        rprint(f"  → {len(lp)} LP positions")

    rows: list[tuple] = []
    for p in yt:
        rows.append((snapshot_date, "YT", p["position_account"], p["owner"], p["vault"], p["amount_raw"]))
    for p in lp:
        rows.append((snapshot_date, "LP", p["position_account"], p["owner"], p["vault"], p["amount_raw"]))

    with warehouse() as con:
        # Idempotent re-run: clear today's snapshot first
        con.execute("DELETE FROM raw_positions WHERE snapshot_date = ?", [snapshot_date])
        if rows:
            con.executemany(
                "INSERT INTO raw_positions (snapshot_date, leg, position_account, owner, vault, amount_raw) VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )

    rprint(f"[green]Wrote {len(rows)} positions[/green]")
    return {"yt": len(yt), "lp": len(lp), "snapshot_date": str(snapshot_date)}


def main() -> None:
    started = datetime.now(timezone.utc)
    rprint(f"[bold]extract_positions[/bold]  started {started.isoformat()}")
    asyncio.run(run())
    rprint(f"[bold]done[/bold]  elapsed {datetime.now(timezone.utc) - started}")


if __name__ == "__main__":
    main()
