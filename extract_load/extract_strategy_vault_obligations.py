"""Snapshot Kamino K-Lend obligations held by Exponent Strategy Vaults.

Reads KaminoObligation entries out of the latest raw_strategy_vault_states
strategy_positions_json, fetches each obligation account, and decodes its
value summary. K-Lend market values are USD-quoted; Fraction fields are
scaled by 2^60.

Obligation layout (verified against live accounts of program
KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD, account size 3344):
  [0..8)     anchor discriminator
  [8..16)    tag u64
  [16..32)   last_update
  [32..64)   lending_market pubkey
  [64..96)   owner pubkey
  @96        deposits: 8 x 136B { reserve pk, deposited_amount u64,
                                  market_value_sf u128, padding }
  @1184      lowest_reserve_deposit_liquidation_ltv u64
  @1192      deposited_value_sf u128
  @1208      borrows: 5 x 200B { reserve pk, cumulative_borrow_rate 48B,
                                 padding u64, borrowed_amount_sf u128,
                                 market_value_sf u128, ... }
  @2208      borrow_factor_adjusted_debt_value_sf u128
  @2224      borrowed_assets_market_value_sf u128
  @2240      allowed_borrow_value_sf u128
  @2256      unhealthy_borrow_value_sf u128

Idempotent per snapshot_date: today's rows are deleted before insert.

Usage:
    python -m extract_load.extract_strategy_vault_obligations
"""
from __future__ import annotations
import asyncio
import base64
import json
import struct
from datetime import datetime, timezone

import base58
from rich import print as rprint

from .config import RPC_ENDPOINTS
from .solana_rpc_client import SolanaRpcClient
from .load import warehouse


SF = 2 ** 60
DEPOSITS_OFF, DEPOSIT_SIZE, N_DEPOSITS = 96, 136, 8
BORROWS_OFF, BORROW_SIZE, N_BORROWS = 1208, 200, 5
DEPOSITED_VALUE_OFF = 1192
SUMMARY_OFF = BORROWS_OFF + N_BORROWS * BORROW_SIZE  # 2208
MIN_LEN = SUMMARY_OFF + 64


def _u64(buf: bytes, off: int) -> int:
    return struct.unpack_from("<Q", buf, off)[0]


def _u128(buf: bytes, off: int) -> int:
    return int.from_bytes(buf[off:off + 16], "little")


def _pk(buf: bytes, off: int) -> str:
    return base58.b58encode(buf[off:off + 32]).decode()


def decode_obligation(raw: bytes) -> dict:
    if len(raw) < MIN_LEN:
        raise ValueError(f"obligation account too short: {len(raw)} < {MIN_LEN}")

    deposits = []
    for i in range(N_DEPOSITS):
        off = DEPOSITS_OFF + i * DEPOSIT_SIZE
        if raw[off:off + 32] == b"\x00" * 32:
            continue
        deposits.append({
            "reserve": _pk(raw, off),
            "deposited_amount": _u64(raw, off + 32),
            "market_value": _u128(raw, off + 40) / SF,
        })

    borrows = []
    for i in range(N_BORROWS):
        off = BORROWS_OFF + i * BORROW_SIZE
        if raw[off:off + 32] == b"\x00" * 32:
            continue
        borrows.append({
            "reserve": _pk(raw, off),
            "borrowed_amount": _u128(raw, off + 88) / SF,
            "market_value": _u128(raw, off + 104) / SF,
        })

    return {
        "lending_market":         _pk(raw, 32),
        "collateral_value":       _u128(raw, DEPOSITED_VALUE_OFF) / SF,
        "debt_value":             _u128(raw, SUMMARY_OFF + 16) / SF,
        "allowed_borrow_value":   _u128(raw, SUMMARY_OFF + 32) / SF,
        "unhealthy_borrow_value": _u128(raw, SUMMARY_OFF + 48) / SF,
        "deposits":               deposits,
        "borrows":                borrows,
    }


def _obligations_to_snapshot(con) -> list[tuple[str, str]]:
    """(vault, obligation) pairs from the latest states snapshot."""
    rows = con.execute("""
        SELECT vault, strategy_positions_json
        FROM raw_strategy_vault_states
        WHERE snapshot_date = (SELECT MAX(snapshot_date)
                               FROM raw_strategy_vault_states)
    """).fetchall()
    pairs: list[tuple[str, str]] = []
    for vault, positions_json in rows:
        if not positions_json:
            continue
        try:
            positions = json.loads(positions_json)
        except (TypeError, json.JSONDecodeError):
            continue
        for p in positions:
            if p.get("variant") == "KaminoObligation" and p.get("obligation"):
                pairs.append((vault, p["obligation"]))
    return pairs


async def _resolve_reserve_mints(reserves: list[str]) -> dict[str, str]:
    """reserve pubkey → liquidity mint (Kamino Reserve.liquidity.mint_pubkey
    at byte offset 128)."""
    if not reserves:
        return {}
    out: dict[str, str] = {}
    async with SolanaRpcClient(RPC_ENDPOINTS) as client:
        for i in range(0, len(reserves), 100):
            chunk = reserves[i:i + 100]
            accts = await client.get_multiple_accounts(chunk)
            for reserve, acct in zip(chunk, accts):
                if acct is None:
                    continue
                data_field = acct["data"]
                data_b64 = data_field[0] if isinstance(data_field, list) else data_field
                raw = base64.b64decode(data_b64)
                if len(raw) >= 160:
                    out[reserve] = base58.b58encode(raw[128:160]).decode()
    return out


async def run() -> dict:
    if not RPC_ENDPOINTS:
        raise RuntimeError("No SOLANA_RPC_URLS configured in .env")

    snapshot_ts = datetime.now(timezone.utc)
    snapshot_date = snapshot_ts.date()

    with warehouse() as con:
        pairs = _obligations_to_snapshot(con)
    rprint(
        f"[cyan]Snapshotting {len(pairs)} strategy-vault obligation(s) "
        f"on {snapshot_date}…[/cyan]"
    )
    if not pairs:
        return {"obligations": 0, "decoded": 0}

    addresses = [obligation for _, obligation in pairs]
    accounts: list[dict | None] = []
    async with SolanaRpcClient(RPC_ENDPOINTS) as client:
        for i in range(0, len(addresses), 100):
            accounts.extend(await client.get_multiple_accounts(addresses[i:i + 100]))

    # Decode obligations first so we know every reserve referenced, then
    # resolve reserve → liquidity mint (Kamino Reserve.liquidity.mint_pubkey
    # at offset 128) so per-leg deposits/borrows can be labeled by asset.
    decoded_pairs = []
    reserve_set: set[str] = set()
    for (vault, obligation), acct in zip(pairs, accounts):
        if acct is None:
            rprint(f"  [yellow]warn[/yellow] obligation {obligation[:8]}… not found")
            continue
        data_field = acct["data"]
        data_b64 = data_field[0] if isinstance(data_field, list) else data_field
        try:
            d = decode_obligation(base64.b64decode(data_b64))
        except Exception as e:
            rprint(f"  [yellow]warn[/yellow] obligation {obligation[:8]}… decode failed: {e}")
            continue
        for leg in d["deposits"] + d["borrows"]:
            reserve_set.add(leg["reserve"])
        decoded_pairs.append((vault, obligation, d))

    reserve_mint = await _resolve_reserve_mints(sorted(reserve_set))
    for d_all in (d for _, _, d in decoded_pairs):
        for leg in d_all["deposits"] + d_all["borrows"]:
            leg["mint"] = reserve_mint.get(leg["reserve"])

    out_rows = []
    for vault, obligation, d in decoded_pairs:
        out_rows.append((
            snapshot_date, vault, obligation, d["lending_market"],
            d["collateral_value"], d["debt_value"],
            d["allowed_borrow_value"], d["unhealthy_borrow_value"],
            json.dumps(d["deposits"], separators=(",", ":")),
            json.dumps(d["borrows"], separators=(",", ":")),
            snapshot_ts,
        ))

    with warehouse() as con:
        con.execute(
            "DELETE FROM raw_strategy_vault_obligations WHERE snapshot_date = ?",
            [snapshot_date],
        )
        if out_rows:
            con.executemany(
                "INSERT INTO raw_strategy_vault_obligations VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                out_rows,
            )

    rprint(f"Wrote {len(out_rows)} obligation row(s)")
    return {"obligations": len(pairs), "decoded": len(out_rows)}


def main() -> None:
    started = datetime.now(timezone.utc)
    rprint(f"extract_strategy_vault_obligations  started {started.isoformat()}")
    asyncio.run(run())
    rprint(f"done  elapsed {datetime.now(timezone.utc) - started}")


if __name__ == "__main__":
    main()
