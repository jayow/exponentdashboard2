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
# Core LpPositions: scan by discriminator only, no dataSize filter.
# Sizes seen in the wild include 88 (no trackers — common newer variant),
# 128, 168, 208 (1+0 / 1+1 / 1+2 trackers). v1 missed size 88; we don't.
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


def _decode_yt_position(data_b64: str) -> tuple[str, str, int, float, int] | None:
    """YieldTokenPosition layout (verified against exponent-core IDL +
    programs/exponent_core/src/state/yield_token_position.rs):
      [0..8)    discriminator
      [8..40)   owner
      [40..72)  vault
      [72..80)  ytBalance (u64 LE)
      [80..112) interest.lastSeenIndex (Number = [u64; 4] LE = 256-bit fixed-pt, /1e12 = natural)
      [112..120) interest.staged (u64 LE)
      [120..124) emissions array length (u32 LE)
      [124..)   emissions: N × yieldTokenTracker (40 bytes each)

    Returns (owner, vault, ytBalance, lastSeenIndex_natural, interestStaged).
    lastSeenIndex_natural is a Python float (256-bit U256 / 1e12) — sufficient
    precision for unstaged-yield projection since natural values are near 1.0
    and yt_balance only carries ~9 underlying-decimals of resolution.
    Used downstream with vault.final_sy_exchange_rate to compute
        unstaged_atoms = floor((1/lsi − 1/final) × yt_balance)
    per state/yield_token_position.rs::calc_earned_sy.
    """
    try:
        data = base64.b64decode(data_b64)
        if len(data) < 120:
            return None
        owner    = base58.b58encode(data[8:40]).decode()
        vault    = base58.b58encode(data[40:72]).decode()
        yt_bal   = struct.unpack("<Q", data[72:80])[0]
        lsi      = int.from_bytes(data[80:112], 'little') / 1e12
        staged   = struct.unpack("<Q", data[112:120])[0]
        return owner, vault, yt_bal, lsi, staged
    except Exception:
        return None


def _decode_clmm_lp_position(data_b64: str) -> dict | None:
    """CLMM LpPosition layout (per exponent-clmm-idl, verified by
    decoding live positions for wallet 7VsV9DUW... and matching
    against Exponent's UI to within ~10% on PT side):
      [0..8)    discriminator
      [8..40)   owner (pubkey)
      [40..72)  market (= MarketThree pubkey, NOT the SY vault)
      [72..88)  fee_inside_last_pt (u128 Q64.64)
      [88..104) fee_inside_last_sy (u128 Q64.64)
      [104..112) lp_balance (u64) — the "liquidity L" used in Uniswap V3 fee math
      [112..120) tokens_owed_sy (u64) — already-accrued SY fees, ready to claim
      [120..128) tokens_owed_pt (u64) — already-accrued PT fees, ready to claim
      [128..132) lower_tick_idx (u32) — slot index in tick array
      [132..136) upper_tick_idx (u32) — slot index in tick array
      [136..140) farms vec length (u32)
      [140..)    farms trackers (40B each = Number lsi + u64 staged)
      after farms: share_trackers vec — each PrincipalShare is:
        tick_idx u32 + right_tick_idx u32 + split_epoch u64 + lp_share Number
        + emissions vec (variable: u32 len + 40B × n)
      after share_trackers: crossing_split (skipped — we don't use it)

    For unclaimed-yield computation we need:
      • lp_balance + fee_inside_last_pt/sy + tick range + tokens_owed (LP fees)
      • farms[0] lsi + sum(share_trackers[*].lp_share)            (LP farm reward)
    Returns those values flat; the SDK formula is then applied in dbt.
    """
    try:
        data = base64.b64decode(data_b64)
        if len(data) < 140:
            return None
        out = {
            "owner":              base58.b58encode(data[8:40]).decode(),
            "market":             base58.b58encode(data[40:72]).decode(),
            "fee_inside_last_pt": int.from_bytes(data[72:88], "little"),
            "fee_inside_last_sy": int.from_bytes(data[88:104], "little"),
            "lp_balance":         struct.unpack("<Q", data[104:112])[0],
            "tokens_owed_sy":     struct.unpack("<Q", data[112:120])[0],
            "tokens_owed_pt":     struct.unpack("<Q", data[120:128])[0],
            "lower_tick_idx":     struct.unpack("<I", data[128:132])[0],
            "upper_tick_idx":     struct.unpack("<I", data[132:136])[0],
            "farm_lsi":           None,
            "farm_staged":        0,
            "total_lp_share":     0.0,
        }
        # farms vec
        n_farms = struct.unpack("<I", data[136:140])[0]
        off = 140
        if n_farms > 0 and off + 40 <= len(data):
            out["farm_lsi"]    = int.from_bytes(data[off:off + 32], "little") / 1e12
            out["farm_staged"] = struct.unpack("<Q", data[off + 32:off + 40])[0]
        off += n_farms * 40
        # share_trackers vec — capture per-share entries (used for USD valuation)
        if off + 4 > len(data):
            return out
        n_shares = struct.unpack("<I", data[off:off + 4])[0]
        off += 4
        shares: list[dict] = []
        total = 0.0
        for sidx in range(n_shares):
            if off + 48 > len(data):
                break
            tick_idx       = struct.unpack("<I", data[off:off + 4])[0]
            right_tick_idx = struct.unpack("<I", data[off + 4:off + 8])[0]
            lp_share       = int.from_bytes(data[off + 16:off + 48], "little") / 1e12
            shares.append({
                "share_idx": sidx, "tick_idx": tick_idx,
                "right_tick_idx": right_tick_idx, "lp_share": lp_share,
            })
            total += lp_share
            off += 48
            # Skip nested emissions vec
            if off + 4 > len(data):
                break
            n_em = struct.unpack("<I", data[off:off + 4])[0]
            off += 4 + n_em * 40
        out["total_lp_share"] = total
        out["shares"] = shares
        return out
    except Exception:
        return None


async def _fetch_core_by_size(client: SolanaRpcClient, disc: bytes, sizes: tuple[int, ...]) -> list[dict]:
    """YT positions only — uses the extended decoder that also pulls staged
    interest (offset 112..120) AND last_seen_index (256-bit at 80..112)
    so we can compute unstaged yield as
        floor((1/lsi − 1/vault.final_sy_rate) × yt_balance).
    Returns dicts with keys:
      position_account, owner, vault, amount_raw, last_seen_index, staged_raw."""
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
            decoded = _decode_yt_position(data_b64)
            if decoded is None:
                continue
            owner, vault, amount, lsi, staged = decoded
            out.append({
                "position_account": a["pubkey"], "owner": owner, "vault": vault,
                "amount_raw": amount, "last_seen_index": lsi, "staged_raw": staged,
            })
        rprint(f"    size={size}: {len(accts)} accounts")
    return out


async def _fetch_core_lp_any_size(client: SolanaRpcClient) -> list[dict]:
    """Core LpPositions, no dataSize filter — catches every size variant.
    We expect 88 (newest, no trackers) plus 128 / 168 / 208 with trackers.
    """
    out: list[dict] = []
    disc_b58 = base58.b58encode(LP_DISC).decode()
    accts = await client.get_program_accounts(
        EXPONENT_CORE_PROGRAM,
        filters=[{"memcmp": {"offset": 0, "bytes": disc_b58}}],
    )
    sizes = Counter()
    for a in accts:
        data_field = a["account"]["data"]
        data_b64 = data_field[0] if isinstance(data_field, list) else data_field
        decoded = _decode_simple_position(data_b64)
        if decoded is None:
            continue
        owner, vault, amount = decoded
        sizes[len(base64.b64decode(data_b64))] += 1
        out.append({"position_account": a["pubkey"], "owner": owner, "vault": vault, "amount_raw": amount})
    for size, n in sorted(sizes.items()):
        rprint(f"    size={size}: {n} accounts")
    return out


async def _fetch_clmm_lp(client: SolanaRpcClient) -> list[dict]:
    """All CLMM LpPositions. Returns dicts with the FULL CLMM-specific fields
    (fee_inside_last, tick range, tokens_owed) so downstream we can apply the
    Uniswap V3 fee_growth_inside formula (lp_balance × Δinside) >> 64."""
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
        sizes[len(base64.b64decode(data_b64))] += 1
        out.append({
            "position_account": a["pubkey"],
            "owner":            decoded["owner"],
            "vault":            decoded["market"],     # = MarketThree pubkey
            "amount_raw":       decoded["lp_balance"],
            "fee_inside_last_pt": decoded["fee_inside_last_pt"],
            "fee_inside_last_sy": decoded["fee_inside_last_sy"],
            "tokens_owed_sy":     decoded["tokens_owed_sy"],
            "tokens_owed_pt":     decoded["tokens_owed_pt"],
            "lower_tick_idx":     decoded["lower_tick_idx"],
            "upper_tick_idx":     decoded["upper_tick_idx"],
            "farm_lsi":           decoded["farm_lsi"],
            "farm_staged":        decoded["farm_staged"],
            "total_lp_share":     decoded["total_lp_share"],
            "shares":             decoded.get("shares", []),
        })
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
        lp_core = await _fetch_core_lp_any_size(client)
        rprint(f"  → {len(lp_core)} core LP positions")
        rprint("  LpPosition (CLMM):")
        lp_clmm = await _fetch_clmm_lp(client)
        rprint(f"  → {len(lp_clmm)} CLMM LP positions")
        rprint("  CLMM market accounts:")
        clmm_markets = await _fetch_clmm_markets(client)
        rprint(f"  → {len(clmm_markets)} CLMM markets discovered")

    pos_rows: list[tuple] = []
    share_rows: list[tuple] = []  # (snapshot_date, position_account, share_idx, tick_idx, right_tick_idx, lp_share)
    for p in yt:
        # YT carries staged_raw (atoms already moved to claim buffer) AND
        # last_seen_index (the SY rate at the time of the wallet's last
        # touch). Both feed the unstaged-yield computation downstream.
        pos_rows.append((snapshot_date, "YT", p["position_account"], p["owner"], p["vault"],
                         p["amount_raw"], p.get("staged_raw", 0), p.get("last_seen_index"),
                         None, None, None, None, None, None,
                         None, None, None))
    for p in lp_core:
        pos_rows.append((snapshot_date, "LP", p["position_account"], p["owner"], p["vault"], p["amount_raw"],
                         None, None, None, None, None, None, None, None,
                         None, None, None))
    for p in lp_clmm:
        # CLMM LP carries:
        #   • Uniswap V3 fee state: fee_inside_last_pt/sy, tokens_owed, tick range
        #   • Farm reward state: farm_lsi (position.farms[0].lsi), farm_staged,
        #     and total_lp_share (sum of share_trackers[*].lp_share). The farm
        #     reward formula is total_lp_share × (market.lp_farm_index − farm_lsi).
        pos_rows.append((snapshot_date, "LP_CLMM", p["position_account"], p["owner"], p["vault"], p["amount_raw"],
                         None, None,
                         p["fee_inside_last_pt"], p["fee_inside_last_sy"],
                         p["tokens_owed_pt"],    p["tokens_owed_sy"],
                         p["lower_tick_idx"],    p["upper_tick_idx"],
                         p["farm_lsi"], p["farm_staged"], p["total_lp_share"]))
        for s in p.get("shares", []):
            share_rows.append((snapshot_date, p["position_account"], s["share_idx"],
                               s["tick_idx"], s["right_tick_idx"], s["lp_share"]))

    with warehouse() as con:
        con.execute("DELETE FROM raw_positions WHERE snapshot_date = ?", [snapshot_date])
        if pos_rows:
            con.executemany(
                """INSERT INTO raw_positions (snapshot_date, leg, position_account, owner, vault, amount_raw,
                                              staged_raw, last_seen_index,
                                              clmm_fee_inside_last_pt, clmm_fee_inside_last_sy,
                                              clmm_tokens_owed_pt, clmm_tokens_owed_sy,
                                              clmm_lower_tick_idx, clmm_upper_tick_idx,
                                              clmm_farm_lsi, clmm_farm_staged, clmm_total_lp_share)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                pos_rows,
            )
        con.execute("DELETE FROM raw_clmm_lp_share_trackers WHERE snapshot_date = ?", [snapshot_date])
        if share_rows:
            con.executemany(
                """INSERT INTO raw_clmm_lp_share_trackers
                   (snapshot_date, position_account, share_idx, tick_idx, right_tick_idx, lp_share)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                share_rows,
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
