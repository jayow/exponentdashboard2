"""Snapshot CLMM MarketThree + per-market Ticks PDA state.

For every Exponent MarketThree account (Anchor disc f2f01a0f94bab9cd in
the CLMM program XPC1MM4dYACDfykNuXYZ5una2DsMDWL24CrYubCvarC):

  1. Decode the market account for fixed-offset fields:
       financials.liquidity_balance, lp_farm.last_seen_timestamp +
       farm_emissions[0].{mint, token_rate, expiry_timestamp, index}
       (those are what we project unstaged farm rewards from).

  2. Follow market.ticks → fetch the Ticks PDA → decode the active ticks
     and the global fee_growth state.

Both pieces are persisted so dbt can compute:
  • CLMM LP fees via Uniswap V3 fee_growth_inside(lower, upper) and
    (lp_balance × delta_inside) >> 64.
  • CLMM LP farm rewards via virtual-index projection per
    state/lp_position.rs::stage_all + claimableFarmEmissions SDK code.

Byte layouts verified on 2026-06-03 against:
  • exponent-clmm-idl/src/exponent_clmm.json (MarketThree struct order)
  • @exponent-labs/exponent-fetcher exponentFetcher.js:420-510 (Ticks)
  • Live decode of CLMM market D7kT5Hnwtf… for wallet 7VsV9DUW…
    reconciles PT-side claimable to within 4-14% of Exponent's UI.
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


EXPONENT_CLMM_PROGRAM = "XPC1MM4dYACDfykNuXYZ5una2DsMDWL24CrYubCvarC"
MARKET_THREE_DISC = bytes.fromhex("f2f01a0f94bab9cd")

# MarketThree byte offsets (verified against IDL + live decode).
# Pubkeys (32 bytes each): admin, address_lookup_table, mint_pt, mint_sy,
#   mint_yt, vault, token_pt_escrow, token_sy_escrow, token_yt_escrow,
#   token_fee_treasury_sy, token_fee_treasury_pt, self_address, ticks.
# Then signer_bump (1), status_flags (1), sy_program (32), exponent_core_program (32),
# configuration_options (1062: f64+u16+u64+f64+u64+u32+[u8;1024]),
# financials (36: u32 + 4×u64),
# emissions vec (variable),
# lp_farm (variable).
OFF_MINT_PT  = 8 + 32 * 2          # 72
OFF_MINT_SY  = 8 + 32 * 3          # 104
OFF_MINT_YT  = 8 + 32 * 4          # 136
OFF_VAULT    = 8 + 32 * 5          # 168
OFF_TICKS    = 8 + 32 * 12         # 392
OFF_CONFIG   = 8 + 32 * 13 + 1 + 1 + 32 + 32  # 490
OFF_FINANCIALS = OFF_CONFIG + 1062  # 1552
OFF_EMISSIONS_VEC = OFF_FINANCIALS + 36  # 1588

# CLMM market emission tracker (MarketEmission, 72B each):
#   token_escrow (32) + lp_share_index Number (32) + last_seen_staged u64 (8)
EMISSION_SIZE = 72

# FarmEmission (76B each):
#   mint (32) + token_rate u64 (8) + expiry_timestamp u32 (4) + index Number (32)
FARM_EMISSION_SIZE = 76


def _decode_market_three(data_b64: str) -> dict | None:
    try:
        data = base64.b64decode(data_b64)
        if len(data) < OFF_EMISSIONS_VEC + 4:
            return None
        mint_pt = base58.b58encode(data[OFF_MINT_PT:OFF_MINT_PT + 32]).decode()
        mint_sy = base58.b58encode(data[OFF_MINT_SY:OFF_MINT_SY + 32]).decode()
        mint_yt = base58.b58encode(data[OFF_MINT_YT:OFF_MINT_YT + 32]).decode()
        vault   = base58.b58encode(data[OFF_VAULT:OFF_VAULT + 32]).decode()
        ticks   = base58.b58encode(data[OFF_TICKS:OFF_TICKS + 32]).decode()
        liquidity_balance = struct.unpack("<Q", data[OFF_FINANCIALS + 28:OFF_FINANCIALS + 36])[0]

        # Skip the emissions vec (variable length) to reach lp_farm.
        n_emissions = struct.unpack("<I", data[OFF_EMISSIONS_VEC:OFF_EMISSIONS_VEC + 4])[0]
        off = OFF_EMISSIONS_VEC + 4 + n_emissions * EMISSION_SIZE

        # lp_farm: last_seen_timestamp (u32) + farm_emissions vec
        lp_farm_last_ts = struct.unpack("<I", data[off:off + 4])[0]
        off += 4
        n_farms = struct.unpack("<I", data[off:off + 4])[0]
        off += 4
        lp_farm_mint = lp_farm_rate = lp_farm_expiry = lp_farm_index = None
        if n_farms > 0 and off + FARM_EMISSION_SIZE <= len(data):
            lp_farm_mint   = base58.b58encode(data[off:off + 32]).decode()
            lp_farm_rate   = struct.unpack("<Q", data[off + 32:off + 40])[0]
            lp_farm_expiry = struct.unpack("<I", data[off + 40:off + 44])[0]
            lp_farm_index  = int.from_bytes(data[off + 44:off + 76], "little") / 1e12

        return {
            "mint_pt": mint_pt, "mint_sy": mint_sy, "mint_yt": mint_yt,
            "vault": vault, "ticks": ticks,
            "liquidity_balance": liquidity_balance,
            "lp_farm_last_ts": lp_farm_last_ts,
            "lp_farm_rate": lp_farm_rate,
            "lp_farm_expiry": lp_farm_expiry,
            "lp_farm_index": lp_farm_index,
            "lp_farm_mint": lp_farm_mint,
        }
    except Exception:
        return None


# Ticks PDA byte layout (verified via brute-force layout calibration +
# match against Exponent UI for known wallet positions):
#   [0..8)    discriminator
#   [8..40)   RB-tree header (32 bytes)
#   [40..360,032)   1000 nodes × 360B each
#   [...]     footer (96B): market (32) + fg_global_pt u128 (16) +
#             fg_global_sy u128 (16) + current_prefix_sum u64 (8) +
#             current_spot_price f64 (8) + current_tick_slot u32 (4) +
#             padding (12)
TICKS_MAX_NODES = 1000
TICKS_TREE_HEADER = 32
NODE_TOTAL = 360
NODE_RB_HEADER = 16  # color/parent/left/right (4× u32) — verified empirically


def _decode_ticks(data_b64: str) -> dict | None:
    try:
        data = base64.b64decode(data_b64)
        if len(data) < 8 + TICKS_TREE_HEADER + TICKS_MAX_NODES * NODE_TOTAL + 96:
            return None
        ticks: list[dict] = []
        off = 8 + TICKS_TREE_HEADER
        for i in range(TICKS_MAX_NODES):
            base = off + NODE_RB_HEADER
            apy_bp = struct.unpack("<I", data[base:base + 4])[0]
            # Layout within value (after apy_bp + 4 padding):
            #   fg_outside_pt u128 (16), fg_outside_sy u128 (16),
            #   liquidity_net i128 (16), liquidity_gross u64 (8),
            #   spot_price f64 (8), principal_pt u128 (16), principal_sy u128 (16),
            #   principal_share_supply Number (32), farms 2×32, emissions 2×64,
            #   last_split_epoch u64 (8), frozen_liquidity u64 (8) = 344
            if apy_bp > 0:
                vbase = base + 8  # past apy_bp + 4 pad
                fg_pt = int.from_bytes(data[vbase:vbase + 16], "little")
                fg_sy = int.from_bytes(data[vbase + 16:vbase + 32], "little")
                # liquidity_net at vbase+32..48 (skipped)
                liq_gross = struct.unpack("<Q", data[vbase + 48:vbase + 56])[0]
                spot = struct.unpack("<d", data[vbase + 56:vbase + 64])[0]
                # principal_pt u64 @ vbase+64, principal_sy u64 @ +72  (per SDK Tick layout)
                principal_pt = struct.unpack("<Q", data[vbase + 64:vbase + 72])[0]
                principal_sy = struct.unpack("<Q", data[vbase + 72:vbase + 80])[0]
                # principal_share_supply Number (32B U256) @ vbase+80 — natural = U256/1e12
                share_supply = int.from_bytes(data[vbase + 80:vbase + 112], "little") / 1e12
                ticks.append({
                    "slot": i + 1, "apy_bp": apy_bp,
                    "spot_price": spot, "liquidity_gross": liq_gross,
                    "fg_outside_pt": fg_pt, "fg_outside_sy": fg_sy,
                    "principal_pt": principal_pt, "principal_sy": principal_sy,
                    "principal_share_supply": share_supply,
                })
            off += NODE_TOTAL
        # Footer
        off_footer = 8 + TICKS_TREE_HEADER + TICKS_MAX_NODES * NODE_TOTAL
        off_footer += 32  # market pubkey
        fg_g_pt = int.from_bytes(data[off_footer:off_footer + 16], "little")
        fg_g_sy = int.from_bytes(data[off_footer + 16:off_footer + 32], "little")
        cur_spot = struct.unpack("<d", data[off_footer + 40:off_footer + 48])[0]
        cur_tick_slot = struct.unpack("<I", data[off_footer + 48:off_footer + 52])[0]
        return {
            "ticks": ticks,
            "fee_growth_global_pt": fg_g_pt,
            "fee_growth_global_sy": fg_g_sy,
            "current_tick_slot": cur_tick_slot,
            "current_spot_price": cur_spot,
        }
    except Exception:
        return None


async def run() -> dict:
    if not RPC_ENDPOINTS:
        raise RuntimeError("No SOLANA_RPC_URLS configured in .env")
    snapshot_date = datetime.now(timezone.utc).date()
    rprint(f"[cyan]Snapshotting MarketThree + Ticks on {snapshot_date}…[/cyan]")

    async with SolanaRpcClient(RPC_ENDPOINTS) as client:
        # 1) Fetch all MarketThree accounts.
        m_accts = await client.get_program_accounts(
            EXPONENT_CLMM_PROGRAM,
            filters=[{"memcmp": {"offset": 0, "bytes": base58.b58encode(MARKET_THREE_DISC).decode()}}],
        )
        markets: list[tuple[str, dict]] = []
        for a in m_accts:
            d = a["account"]["data"]
            decoded = _decode_market_three(d[0] if isinstance(d, list) else d)
            if decoded:
                markets.append((a["pubkey"], decoded))
        rprint(f"  decoded {len(markets)} MarketThree accounts")

        # 2) Fetch each market's Ticks PDA.
        market_rows: list[tuple] = []
        global_rows: list[tuple] = []
        tick_rows:   list[tuple] = []
        for clmm_market, m in markets:
            ticks_resp = await client.get_account_info(m["ticks"])
            decoded_ticks = None
            if ticks_resp and ticks_resp.get("data"):
                d = ticks_resp["data"]
                decoded_ticks = _decode_ticks(d[0] if isinstance(d, list) else d)
            market_rows.append((
                snapshot_date, clmm_market,
                m["mint_pt"], m["mint_sy"], m["mint_yt"], m["vault"], m["ticks"],
                m["liquidity_balance"],
                m["lp_farm_last_ts"], m["lp_farm_rate"], m["lp_farm_expiry"],
                m["lp_farm_index"], m["lp_farm_mint"],
            ))
            if decoded_ticks:
                global_rows.append((
                    snapshot_date, clmm_market,
                    decoded_ticks["fee_growth_global_pt"],
                    decoded_ticks["fee_growth_global_sy"],
                    decoded_ticks["current_tick_slot"],
                    decoded_ticks["current_spot_price"],
                ))
                for t in decoded_ticks["ticks"]:
                    tick_rows.append((
                        snapshot_date, clmm_market, t["slot"],
                        t["apy_bp"], t["spot_price"], t["liquidity_gross"],
                        t["fg_outside_pt"], t["fg_outside_sy"],
                        t["principal_pt"], t["principal_sy"], t["principal_share_supply"],
                    ))

    rprint(f"  decoded {len(global_rows)} Ticks PDAs / {len(tick_rows)} active ticks")

    with warehouse() as con:
        con.execute("DELETE FROM raw_market_three_states WHERE snapshot_date = ?", [snapshot_date])
        con.execute("DELETE FROM raw_clmm_market_globals  WHERE snapshot_date = ?", [snapshot_date])
        con.execute("DELETE FROM raw_clmm_ticks           WHERE snapshot_date = ?", [snapshot_date])
        if market_rows:
            con.executemany(
                """INSERT INTO raw_market_three_states
                   (snapshot_date, clmm_market, mint_pt, mint_sy, mint_yt, vault, ticks_account,
                    liquidity_balance, lp_farm_last_ts, lp_farm_token_rate, lp_farm_expiry_ts,
                    lp_farm_index, lp_farm_mint)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                market_rows,
            )
        if global_rows:
            con.executemany(
                """INSERT INTO raw_clmm_market_globals
                   (snapshot_date, clmm_market, fee_growth_global_pt, fee_growth_global_sy,
                    current_tick_slot, current_spot_price)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                global_rows,
            )
        if tick_rows:
            con.executemany(
                """INSERT INTO raw_clmm_ticks
                   (snapshot_date, clmm_market, tick_slot, apy_bp, spot_price,
                    liquidity_gross, fee_growth_outside_pt, fee_growth_outside_sy,
                    principal_pt, principal_sy, principal_share_supply)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                tick_rows,
            )

    rprint(f"[green]Wrote {len(market_rows)} markets, {len(global_rows)} globals, {len(tick_rows)} ticks[/green]")
    return {
        "markets": len(market_rows),
        "tick_pdas": len(global_rows),
        "ticks": len(tick_rows),
        "snapshot_date": str(snapshot_date),
    }


def main() -> None:
    started = datetime.now(timezone.utc)
    rprint(f"[bold]extract_market_three_states[/bold]  started {started.isoformat()}")
    asyncio.run(run())
    rprint(f"[bold]done[/bold]  elapsed {datetime.now(timezone.utc) - started}")


if __name__ == "__main__":
    main()
