"""Snapshot Exponent tranche-market vault state (sr/jr risk tranches).

Each ExponentTranchingMarket account (Anchor disc 7726787a3c183aa0 in
EXPONENT_TRANCHING_PROGRAM) holds the financials, supply, risk config and
fee config for one tranche vault (one per underlying + maturity epoch).

Layout decoded against @exponent-labs/exponent-tranching-idl@0.9.19
(committed at exponent_tranching_idl.json):

  [0..8)         discriminator
  [8..40)        address_lookup_table   (pubkey)
  [40..72)       sy_mint                (pubkey)
  [72..104)      sy_program             (pubkey)
  [104..136)     token_sy_escrow        (pubkey)
  [136..168)     mint_lp_senior         (pubkey)
  [168..200)     mint_lp_junior         (pubkey)
  [200..232)     self_address           (pubkey)
  [232..264)     return_model_storage   (pubkey)
  [264..265)     signer_bump            (u8 inside [u8;1])
  [265..266)     return_model_storage_bump (u8 inside [u8;1])
  [266..274)     seed_id                ([u8;8] — ASCII tag, e.g. "onycC101")
  [274..275)     status_flags           (u8)
  [275..276)     market_state           (enum tag u8)
  ─ financials (TranchingMarketFinancials) ─
  [276..308)     sr_raw_net_asset             (Number=u256/1e21 for USD)
  [308..340)     jr_raw_net_asset             (Number)
  [340..372)     sr_effective_net_asset       (Number)
  [372..404)     jr_effective_net_asset       (Number)
  [404..436)     sr_impermanent_loss          (Number)
  [436..468)     jr_impermanent_loss          (Number)
  [468..500)     utilization                  (Number=u256/1e12 for ratio)
  [500..532)     current_junior_return_share  (Number=u256/1e12)
  [532..564)     tw_junior_return_share_accrued (Number, cumulative)
  [564..572)     last_sync_ts                 (i64)
  [572..580)     last_distribution_ts         (i64)
  [580..588)     fixed_term_end_ts            (i64)
  ─ tranche_supply_state (10× u64) ─
  [588..596)     total_senior_lp_supply       (u64)
  [596..604)     total_junior_lp_supply       (u64)
  [604..612)     max_senior_lp_supply         (u64)
  [612..620)     max_junior_lp_supply         (u64)
  [620..628..]   pending fees (6× u64) — sr_fee, jr_fee, sr_dep_fee,
                 jr_dep_fee, sr_wd_fee, jr_wd_fee
  ─ tranche_asset_state (2× u64) ─
  [668..676)     senior_sy_amount            (u64 atoms)
  [676..684)     junior_sy_amount            (u64 atoms)
  ─ risk_config (TranchingRiskConfig) ─
  [684..716)     min_coverage_ratio          (Number=u256/1e12)
  [716..748)     beta                        (Number)
  [748..780)     liquidation_utilization     (Number)
  [780..784)     fixed_term_duration_sec     (u32)
  [784..792)     min_deposit_amount          (u64)
  …additional config follows (fee config, return model, roles, CPI accounts).

NAV USD scale: u256 / 1e21 — verified against API srRawNetAssetValue.
Ratio scale: u256 / 1e12 — verified against API coverageRatio,
                              utilization, currentJuniorReturnShare.

We don't yet decode the full risk_config / fee_config / return_model tail
since those rarely change and aren't needed for the dashboard hero stats.
Phase 2 will add the return-model decoder for piecewise APY math.
"""
from __future__ import annotations
import asyncio
import base64
import struct
from datetime import datetime, timezone

import base58
from rich import print as rprint

from .config import RPC_ENDPOINTS, EXPONENT_TRANCHING_PROGRAM
from .solana_rpc_client import SolanaRpcClient
from .load import warehouse


# sha256("account:ExponentTranchingMarket")[:8] per the IDL
MARKET_DISCRIMINATOR = bytes([119, 38, 120, 122, 60, 24, 58, 160])
# sha256("account:ExponentTranchingMarketReturnModel")[:8]
RETURN_MODEL_DISCRIMINATOR = bytes.fromhex("9cfb62964e7fc44d")

CURVE_BREAKPOINTS = 100  # 100 Y values at implicit X = i/100 (spans util [0, 0.99])

# Scale constants verified against api.exponent.finance/tranching-markets:
#   srRawNetAssetValue = u256(off=276) / 1e21
#   coverageRatio = jr_eff_nav / (jr_eff_nav + sr_eff_nav)  (both u256/1e21)
#   utilization = u256(off=468) / 1e12
NAV_SCALE   = 1e21
RATIO_SCALE = 1e12


def _u256(buf: bytes, off: int) -> int:
    return int.from_bytes(buf[off:off + 32], "little")


def _pk(buf: bytes, off: int) -> str:
    return base58.b58encode(buf[off:off + 32]).decode()


def decode_tranche_market(raw: bytes) -> dict | None:
    """Decode an ExponentTranchingMarket account into a flat dict.

    Returns None if the account is the wrong size or has a mismatched
    discriminator (some accounts owned by the same program could be the
    ReturnModelStorage variant — caller should pre-filter on disc).
    """
    if len(raw) < 792 or raw[:8] != MARKET_DISCRIMINATOR:
        return None

    # Pubkeys
    out = {
        "address_lookup_table": _pk(raw, 8),
        "sy_mint":              _pk(raw, 40),
        "sy_program":           _pk(raw, 72),
        "token_sy_escrow":      _pk(raw, 104),
        "mint_lp_senior":       _pk(raw, 136),
        "mint_lp_junior":       _pk(raw, 168),
        "self_address":         _pk(raw, 200),
        "return_model_storage": _pk(raw, 232),
    }

    # Header (after pubkeys + 2 bump arrays)
    out["seed_id"]      = raw[266:274].rstrip(b"\x00").decode("latin1")
    out["status_flags"] = raw[274]
    out["market_state"] = raw[275]

    # Financials (offsets 276-588)
    out["sr_raw_nav_usd"]               = _u256(raw, 276) / NAV_SCALE
    out["jr_raw_nav_usd"]               = _u256(raw, 308) / NAV_SCALE
    out["sr_effective_nav_usd"]         = _u256(raw, 340) / NAV_SCALE
    out["jr_effective_nav_usd"]         = _u256(raw, 372) / NAV_SCALE
    out["sr_impermanent_loss"]          = _u256(raw, 404) / NAV_SCALE
    out["jr_impermanent_loss"]          = _u256(raw, 436) / NAV_SCALE
    out["utilization"]                  = _u256(raw, 468) / RATIO_SCALE
    out["current_junior_return_share"]  = _u256(raw, 500) / RATIO_SCALE
    out["tw_junior_return_share_accrued"] = _u256(raw, 532) / RATIO_SCALE
    out["last_sync_ts"]         = struct.unpack_from("<q", raw, 564)[0]
    out["last_distribution_ts"] = struct.unpack_from("<q", raw, 572)[0]
    out["fixed_term_end_ts"]    = struct.unpack_from("<q", raw, 580)[0]

    # Supply state (10× u64 at 588..668)
    sup = struct.unpack_from("<10Q", raw, 588)
    (out["total_sr_lp_supply"],
     out["total_jr_lp_supply"],
     out["max_sr_lp_supply"],
     out["max_jr_lp_supply"],
     *_pending_fees) = sup
    # _pending_fees: 6× u64 of pending fee accumulators — captured but not
    # written to a column today; phase 2 mart can decode if needed.

    # Tranche asset state (2× u64)
    out["sr_sy_amount"] = struct.unpack_from("<Q", raw, 668)[0]
    out["jr_sy_amount"] = struct.unpack_from("<Q", raw, 676)[0]

    # Risk config (first 3 Number + 2 primitive fields we care about)
    out["min_coverage_ratio"]       = _u256(raw, 684) / RATIO_SCALE
    out["fixed_term_duration_sec"]  = struct.unpack_from("<I", raw, 780)[0]
    out["min_deposit_amount"]       = struct.unpack_from("<Q", raw, 784)[0]

    return out


def decode_return_model_curve(raw: bytes) -> list[tuple[float, float]] | None:
    """Decode the piecewise-linear curve from ExponentTranchingMarketReturnModel.

    Layout (verified end-to-end against the live ONyc account via the
    Anchor IDL + SDK codec — see workflow 2026-06-27):
      [0..8)    discriminator 9cfb62964e7fc44d
      [8..40)   market pubkey
      [40..41)  enum tag (0x01 = PiecewiseLinearCurve)
      [41..45)  Vec<u64> length prefix u32 = 1000
      [45..8045) 1000 u64 LE words = 100 Y values at 10-u64 stride
      [8045..8173) reserved [u8; 128]

    The 100 Y values are jr_return_share at uniformly-spaced UTILIZATION
    breakpoints with implicit X = i/100. Each Y sits at a different limb
    within its 10-u64 slot: limb[0] for slots 0..49, limb[9] for slots
    50..99 (presumably an in-struct padding artifact in the Rust source).

    Empirically reproduces live current_junior_return_share to ~5e-5.
    """
    if len(raw) < 8045 or raw[:8] != RETURN_MODEL_DISCRIMINATOR:
        return None
    # Variant tag is at offset 40; only the PiecewiseLinearCurve variant is
    # supported here. UtilizationGuidedCurve has a different layout.
    if raw[40] != 0x01:
        return None
    n = int.from_bytes(raw[41:45], "little")
    if n != 1000:
        return None

    u64s = struct.unpack_from("<1000Q", raw, 45)
    ys = [(u64s[i * 10] if i < 50 else u64s[i * 10 + 9]) / 1e12 for i in range(CURVE_BREAKPOINTS)]
    xs = [i / 100.0 for i in range(CURVE_BREAKPOINTS)]
    return list(zip(xs, ys))


async def run() -> dict:
    if not RPC_ENDPOINTS:
        raise RuntimeError("No SOLANA_RPC_URLS configured in .env")
    snapshot_date = datetime.now(timezone.utc).date()
    rprint(f"[cyan]Snapshotting tranching markets on {snapshot_date}…[/cyan]")

    async with SolanaRpcClient(RPC_ENDPOINTS) as client:
        accts = await client.get_program_accounts(
            EXPONENT_TRANCHING_PROGRAM,
            filters=[
                {"memcmp": {"offset": 0, "bytes": base58.b58encode(MARKET_DISCRIMINATOR).decode()}}
            ],
        )

        # Pre-decode markets so we know their return_model_storage pubkeys;
        # then batch-fetch those accounts in a second call.
        market_decoded: list[tuple[str, dict]] = []  # (vault_pubkey, decoded)
        for a in accts:
            data_field = a["account"]["data"]
            data_b64 = data_field[0] if isinstance(data_field, list) else data_field
            decoded = decode_tranche_market(base64.b64decode(data_b64))
            if decoded is not None:
                market_decoded.append((a["pubkey"], decoded))

        return_model_pks = [d["return_model_storage"] for _, d in market_decoded]
        curves: dict[str, list[tuple[float, float]]] = {}
        if return_model_pks:
            rm_accounts = await client.get_multiple_accounts(return_model_pks)
            for pk, acct_info in zip(return_model_pks, rm_accounts):
                if not acct_info:
                    continue
                data_field = acct_info["data"]
                data_b64 = data_field[0] if isinstance(data_field, list) else data_field
                curve = decode_return_model_curve(base64.b64decode(data_b64))
                if curve:
                    curves[pk] = curve

    rows: list[tuple] = []
    curve_rows: list[tuple] = []
    for vault_pk, decoded in market_decoded:
        curve = curves.get(decoded["return_model_storage"]) or []
        for i, (x, y) in enumerate(curve):
            curve_rows.append((snapshot_date, vault_pk, i, x, y))
        rows.append((
            snapshot_date,
            vault_pk,
            decoded["seed_id"],
            decoded["sy_mint"],
            decoded["sy_program"],
            decoded["token_sy_escrow"],
            decoded["mint_lp_senior"],
            decoded["mint_lp_junior"],
            decoded["return_model_storage"],
            decoded["address_lookup_table"],
            decoded["status_flags"],
            decoded["market_state"],
            decoded["sr_raw_nav_usd"],
            decoded["jr_raw_nav_usd"],
            decoded["sr_effective_nav_usd"],
            decoded["jr_effective_nav_usd"],
            decoded["sr_impermanent_loss"],
            decoded["jr_impermanent_loss"],
            decoded["utilization"],
            decoded["current_junior_return_share"],
            decoded["tw_junior_return_share_accrued"],
            decoded["last_sync_ts"],
            decoded["last_distribution_ts"],
            decoded["fixed_term_end_ts"],
            decoded["total_sr_lp_supply"],
            decoded["total_jr_lp_supply"],
            decoded["max_sr_lp_supply"],
            decoded["max_jr_lp_supply"],
            decoded["sr_sy_amount"],
            decoded["jr_sy_amount"],
            decoded["min_coverage_ratio"],
            decoded["fixed_term_duration_sec"],
            decoded["min_deposit_amount"],
        ))

    rprint(f"  decoded {len(rows)} tranching markets + {len(curve_rows)} curve breakpoints")

    with warehouse() as con:
        con.execute("DELETE FROM raw_tranche_states WHERE snapshot_date = ?", [snapshot_date])
        con.execute("DELETE FROM raw_tranche_return_curves WHERE snapshot_date = ?", [snapshot_date])
        if rows:
            con.executemany(
                """INSERT INTO raw_tranche_states (
                    snapshot_date, tranche_vault, seed_id, sy_mint, sy_program,
                    token_sy_escrow, mint_lp_senior, mint_lp_junior,
                    return_model_storage, address_lookup_table, status_flags, market_state,
                    sr_raw_nav_usd, jr_raw_nav_usd, sr_effective_nav_usd, jr_effective_nav_usd,
                    sr_impermanent_loss, jr_impermanent_loss, utilization,
                    current_junior_return_share, tw_junior_return_share_accrued,
                    last_sync_ts, last_distribution_ts, fixed_term_end_ts,
                    total_sr_lp_supply, total_jr_lp_supply, max_sr_lp_supply, max_jr_lp_supply,
                    sr_sy_amount, jr_sy_amount, min_coverage_ratio,
                    fixed_term_duration_sec, min_deposit_amount
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
        if curve_rows:
            con.executemany(
                """INSERT INTO raw_tranche_return_curves
                   (snapshot_date, tranche_vault, breakpoint_idx, x_value, y_value)
                   VALUES (?, ?, ?, ?, ?)""",
                curve_rows,
            )

    rprint(f"[green]Wrote {len(rows)} state rows + {len(curve_rows)} curve breakpoints[/green]")
    return {"tranches": len(rows), "curve_breakpoints": len(curve_rows), "snapshot_date": str(snapshot_date)}


def main() -> None:
    started = datetime.now(timezone.utc)
    rprint(f"[bold]extract_tranche_states[/bold]  started {started.isoformat()}")
    asyncio.run(run())
    rprint(f"[bold]done[/bold]  elapsed {datetime.now(timezone.utc) - started}")


if __name__ == "__main__":
    main()
