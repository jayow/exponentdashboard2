"""Extract user YT and LP positions from Exponent's custom Anchor accounts.

YT and LP positions live in `YieldTokenPosition` / `LpPosition` Anchor
accounts under one of two Exponent programs:

  - Core (ExponentnaRg…) — original AMM. YT (sizes 124/164/204) and LP
    (sizes 128/168/208) share the same simple layout:
      [0..8)   discriminator
      [8..40)  owner pubkey
      [40..72) vault pubkey  (SY vault for YT, MarketTwo for LP)
      [72..80) amount (u64 LE)

  - CLMM (XPC1MM4d…) — concentrated-liquidity AMM. Same LpPosition
    discriminator (69f125c8e002fc5a) but a longer struct:
      [0..8)    discriminator
      [8..40)   owner pubkey
      [40..72)  market pubkey  (a CLMM market account, disc f2f01a0f94bab9cd)
      [72..88)  feeInsideLastPt   (u128)
      [88..104) feeInsideLastSy   (u128)
      [104..112) lpBalance        (u64 — THE balance we care about)
    Sizes are variable (720+) because each position carries tick range,
    farms, share trackers, etc. Layout taken from @exponent-labs/exponent-sdk
    build/client/clmm/accounts/lpPosition.js.

The CLMM market's `pt_mint` lives at offset 72 of the CLMM market account
(disc f2f01a0f94bab9cd), giving us a clmm_market → market_key mapping
when joined back to dim_markets.pt_mint.
"""
from __future__ import annotations
import asyncio
import base64
import struct
from collections import Counter
from datetime import datetime, timezone

import base58
from rich import print as rprint

from .config import RPC_ENDPOINTS, EXPONENT_CORE_PROGRAM, EXPONENT_CLMM_PROGRAM
from .solana_rpc_client import SolanaRpcClient
from .load import warehouse


YT_DISC = bytes.fromhex("e35c92311d55475e")
LP_DISC = bytes.fromhex("69f125c8e002fc5a")
CLMM_MARKET_DISC = bytes.fromhex("f2f01a0f94bab9cd")
YT_SIZES = (124, 164, 204)
LP_CORE_SIZES = (128, 168, 208)
# CLMM LP balance lives at offset 104 (past the two u128 fee_inside fields).


def _decode_simple_position(data_b64: str) -> tuple[str, str, int] | None:
    """Core program YT/LP layout: balance at offset 72."""
    try:
        data = base64.b64decode(data_b64)
        if len(data) < 80:
            return None
        owner  = base58.b58encode(data[8:40]).decode()
        vault  = base58.b58encode(data[40:72]).decode()
        amount = struct.unpack("<Q", data[72:80])[0]
        return owner, vault, amount
    except Exception:
        return None


def _decode_clmm_lp_position(data_b64: str) -> tuple[str, str, int] | None:
    """CLMM LpPosition layout: balance at offset 104 (after 2× u128 fees)."""
    try:
        data = base64.b64decode(data_b64)
        if len(data) < 112:
            return None
        owner  = base58.b58encode(data[8:40]).decode()
        market = base58.b58encode(data[40:72]).decode()
        amount = struct.unpack("<Q", data[104:112])[0]
        return owner, market, amount
    except Exception:
        return None


async def _fetch_core_by_size(client: SolanaRpcClient, disc: bytes, sizes: tuple[int, ...]) -> list[dict]:
    """Returns list of {'position_account','owner','vault','amount_raw'} for core program."""
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
            decoded = _decode_simple_position(data_b64)
            if decoded is None:
                continue
            owner, vault, amount = decoded
            out.append({"position_account": a["pubkey"], "owner": owner, "vault": vault, "amount_raw": amount})
        rprint(f"    size={size}: {len(accts)} accounts")
    return out


async def _fetch_clmm_lp(client: SolanaRpcClient) -> list[dict]:
    """All CLMM LpPositions (sizes vary). vault → CLMM market account."""
    out: list[dict] = []
    disc_b58 = base58.b58encode(LP_DISC).decode()
    accts = await client.get_program_accounts(
        EXPONENT_CLMM_PROGRAM,
        filters=[{"memcmp": {"offset": 0, "bytes": disc_b58}}],
    )
    sizes = Counter()
    for a in accts:
        data_field = a["account"]["data"]
        data_b64 = data_field[0] if isinstance(data_field, list) else data_field
        decoded = _decode_clmm_lp_position(data_b64)
        if decoded is None:
            continue
        owner, market, amount = decoded
        sizes[len(base64.b64decode(data_b64))] += 1
        out.append({"position_account": a["pubkey"], "owner": owner, "vault": market, "amount_raw": amount})
    for size, n in sorted(sizes.items()):
        rprint(f"    size={size}: {n} accounts")
    return out


async def _fetch_clmm_markets(client: SolanaRpcClient) -> list[tuple[str, str]]:
    """CLMM market accounts (disc f2f01a0f94bab9cd). Returns (account, pt_mint) — pt_mint at offset 72."""
    out: list[tuple[str, str]] = []
    disc_b58 = base58.b58encode(CLMM_MARKET_DISC).decode()
    accts = await client.get_program_accounts(
        EXPONENT_CLMM_PROGRAM,
        filters=[{"memcmp": {"offset": 0, "bytes": disc_b58}}],
    )
    for a in accts:
        data_field = a["account"]["data"]
        data = base64.b64decode(data_field[0] if isinstance(data_field, list) else data_field)
        if len(data) < 104:
            continue
        pt_mint = base58.b58encode(data[72:104]).decode()
        out.append((a["pubkey"], pt_mint))
    return out


async def run() -> dict:
    if not RPC_ENDPOINTS:
        raise RuntimeError("No SOLANA_RPC_URLS configured in .env")

    snapshot_date = datetime.now(timezone.utc).date()
    rprint(f"[cyan]Snapshotting Anchor positions for {snapshot_date}…[/cyan]")

    async with SolanaRpcClient(RPC_ENDPOINTS) as client:
        rprint("  YieldTokenPosition (core):")
        yt = await _fetch_core_by_size(client, YT_DISC, YT_SIZES)
        rprint(f"  → {len(yt)} YT positions")
        rprint("  LpPosition (core AMM):")
        lp_core = await _fetch_core_by_size(client, LP_DISC, LP_CORE_SIZES)
        rprint(f"  → {len(lp_core)} core LP positions")
        rprint("  LpPosition (CLMM):")
        lp_clmm = await _fetch_clmm_lp(client)
        rprint(f"  → {len(lp_clmm)} CLMM LP positions")
        rprint("  CLMM market accounts:")
        clmm_markets = await _fetch_clmm_markets(client)
        rprint(f"  → {len(clmm_markets)} CLMM markets discovered")

    pos_rows: list[tuple] = []
    for p in yt:
        pos_rows.append((snapshot_date, "YT", p["position_account"], p["owner"], p["vault"], p["amount_raw"]))
    for p in lp_core:
        pos_rows.append((snapshot_date, "LP", p["position_account"], p["owner"], p["vault"], p["amount_raw"]))
    for p in lp_clmm:
        # leg='LP_CLMM' so int_holders_current can route via the CLMM-market
        # → pt_mint mapping instead of the AMM amm_pool join.
        pos_rows.append((snapshot_date, "LP_CLMM", p["position_account"], p["owner"], p["vault"], p["amount_raw"]))

    with warehouse() as con:
        con.execute("DELETE FROM raw_positions WHERE snapshot_date = ?", [snapshot_date])
        if pos_rows:
            con.executemany(
                "INSERT INTO raw_positions (snapshot_date, leg, position_account, owner, vault, amount_raw) VALUES (?, ?, ?, ?, ?, ?)",
                pos_rows,
            )
        # raw_clmm_markets — discovered each run; used as a clmm_market → pt_mint
        # lookup so the SQL layer can resolve back to market_key via dim_markets.
        con.execute(
            "CREATE TABLE IF NOT EXISTS raw_clmm_markets (snapshot_date DATE NOT NULL, clmm_market VARCHAR NOT NULL, pt_mint VARCHAR NOT NULL, PRIMARY KEY (snapshot_date, clmm_market))"
        )
        con.execute("DELETE FROM raw_clmm_markets WHERE snapshot_date = ?", [snapshot_date])
        if clmm_markets:
            con.executemany(
                "INSERT INTO raw_clmm_markets (snapshot_date, clmm_market, pt_mint) VALUES (?, ?, ?)",
                [(snapshot_date, m, pt) for m, pt in clmm_markets],
            )

    rprint(f"[green]Wrote {len(pos_rows)} positions + {len(clmm_markets)} CLMM markets[/green]")
    return {
        "yt": len(yt),
        "lp_core": len(lp_core),
        "lp_clmm": len(lp_clmm),
        "clmm_markets": len(clmm_markets),
        "snapshot_date": str(snapshot_date),
    }


def main() -> None:
    started = datetime.now(timezone.utc)
    rprint(f"[bold]extract_positions[/bold]  started {started.isoformat()}")
    asyncio.run(run())
    rprint(f"[bold]done[/bold]  elapsed {datetime.now(timezone.utc) - started}")


if __name__ == "__main__":
    main()
