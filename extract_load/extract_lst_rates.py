"""Pull on-chain LST → base exchange rates for tokens Jupiter/Pyth can't price.

Generic provider registry — each provider knows how to read one LST's rate
from its issuing protocol's on-chain state. Adding a new LST means adding
one provider class to PROVIDERS.

Current providers:
  - SolsticeGlamVaultStSLX : stSLX (Solstice GLAM-managed staking vault)
  - KaminoSyKUSDG          : kUSDG (Kamino USDG reserve, Solstice market)
  - SanctumSplRkuSOL       : rkuSOL (Sanctum SPL stake pool)

Writes one row per (snapshot_date, lst_mint) to raw_lst_rates.

Direction: `lst_per_base` = LST / base. So `lst_usd = base_usd / lst_per_base`.
"""
from __future__ import annotations
import asyncio
import base64
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

import httpx
from rich import print as rprint

from .config import RPC_ENDPOINTS
from .load import warehouse


# SPL Token layouts (also valid for Token-2022 base header — extensions live past offset 82)
MINT_SUPPLY_OFFSET     = 36   # u64 LE
MINT_DECIMALS_OFFSET   = 44   # u8
TOKEN_ACCOUNT_AMOUNT_OFFSET = 64  # u64 LE
# Kamino reserve Fraction scale (Q60.60 fixed-point)
KAMINO_FRACTION = 2 ** 60


@dataclass
class RateRow:
    lst_mint: str
    base_mint: str
    lst_per_base: float
    slot: int
    provider: str


class Provider(Protocol):
    name: str
    sanity_lo: float
    sanity_hi: float
    async def fetch_rate(self, client: httpx.AsyncClient, rpc_url: str) -> RateRow: ...


def _read_u64_le(buf: bytes, off: int) -> int:
    return struct.unpack_from("<Q", buf, off)[0]


def _read_u128_le(buf: bytes, off: int) -> int:
    lo, hi = struct.unpack_from("<QQ", buf, off)
    return lo | (hi << 64)


async def _get_multi(client: httpx.AsyncClient, rpc_url: str, pubkeys: list[str]) -> tuple[list[bytes | None], int]:
    """getMultipleAccounts -> (list of decoded data bytes, context slot)."""
    resp = await client.post(rpc_url, json={
        "jsonrpc": "2.0", "id": 1, "method": "getMultipleAccounts",
        "params": [pubkeys, {"encoding": "base64"}]
    })
    resp.raise_for_status()
    body = resp.json()
    slot = body.get("result", {}).get("context", {}).get("slot", 0)
    values = body.get("result", {}).get("value") or []
    out: list[bytes | None] = []
    for v in values:
        if v is None:
            out.append(None)
        else:
            out.append(base64.b64decode(v["data"][0]))
    return out, slot


# ---------- Providers ----------

class SolsticeGlamVaultStSLX:
    """stSLX (Token-2022) → SLX via GLAM-managed staking vault.

    Math (ported from @exponent-labs/exponent-fetcher utils/solstice.js
    calculateSolsticeGlamVaultExchangeRate):
        redemption_rate (SLX-per-stSLX) =
            (vault_slx_amount × 10**stslx_decimals)
          / (stslx_supply       × 10**slx_decimals)
        lst_per_base = 1 / redemption_rate
        Fallback to 1.0 when either value is zero (uninitialised vault).

    VAULT_SLX_ATA derivation (pinned; do not re-derive at runtime):
        glam_program = GLAMpaME8wdTEzxtiYEAa5yD8fZbxZiz2hNtV58RZiEz
                       (owner of the GLAM state account; Solstice uses
                        this older deployment, not the newer gConFzx…)
        glam_state   = 5E2scHi8LyZAqZeVHnXLeFhwoePxD2CTdSruWmjgVEoB
                       (stSLX mint's Token-2022 TokenMetadata
                        additional_metadata['GlamState'])
        vault_pda    = find_program_address([b'vault', glam_state],
                                             glam_program)
                     = GMwdh2jTdTrrhA7dMR7Cc2zC6gV38UePzAXeoFHrXnfH
        VAULT_SLX_ATA = get_associated_token_address(
                           vault_pda, SLX_MINT, TOKEN_PROGRAM_ID)
                      = 7CssRFNePpnDiCzjRC5kPRDpEJn87JMeDG7s6Gww9CTf
    A prior version of this file used 8cuSaU2u…2zr — that is a Solstice
    treasury/distributor wallet (owner 42mfEQ7f…nsWZ, not the vault PDA)
    holding 367M SLX, which inflated redemption by ~95× and the
    downstream stSLX USD TVL with it.
    """
    name = "solstice_glam_vault"
    sanity_lo = 0.80     # stSLX is yield-bearing SLX; redemption ≈ 1.00–1.10
    sanity_hi = 1.10     # tight band so a wrong-ATA regression trips it
    STSLX_MINT    = "GxHksENo754dKj6kv5d2z7ey9KwE7YSRYgRCtoFYd2yq"
    SLX_MINT      = "SLXdx4BUt2v9uJQNzWqSfzTJ9UKLUDsvxHFMEEdrfgq"
    VAULT_SLX_ATA = "7CssRFNePpnDiCzjRC5kPRDpEJn87JMeDG7s6Gww9CTf"

    async def fetch_rate(self, client, rpc_url) -> RateRow:
        bufs, slot = await _get_multi(client, rpc_url, [self.VAULT_SLX_ATA, self.SLX_MINT, self.STSLX_MINT])
        if any(b is None for b in bufs):
            raise RuntimeError(f"{self.name}: one of the 3 accounts is missing")
        vault_amt = _read_u64_le(bufs[0], TOKEN_ACCOUNT_AMOUNT_OFFSET)
        slx_dec   = bufs[1][MINT_DECIMALS_OFFSET]
        stslx_sup = _read_u64_le(bufs[2], MINT_SUPPLY_OFFSET)
        stslx_dec = bufs[2][MINT_DECIMALS_OFFSET]
        if vault_amt == 0 or stslx_sup == 0:
            redemption = 1.0
        else:
            num = vault_amt * (10 ** stslx_dec)
            den = stslx_sup * (10 ** slx_dec)
            redemption = num / den
        return RateRow(
            lst_mint=self.STSLX_MINT, base_mint=self.SLX_MINT,
            lst_per_base=1.0 / redemption, slot=slot, provider=self.name,
        )


class KaminoSyKUSDG:
    """kUSDG → USDG via Kamino lending reserve.

    Math (ported from @exponent-labs/kamino-reserve-deserializer):
        total_supply = availableAmount
                     + borrowedAmountSf  / 2**60
                     - accProtoFeesSf    / 2**60
                     - accRefFeesSf      / 2**60
                     - pendRefFeesSf     / 2**60
        exchange_rate (USDG-per-kUSDG) = total_supply / mintTotalSupply
        lst_per_base = 1 / exchange_rate

    Layout offsets are relative to account start (include the 8-byte disc).
    """
    name = "kamino_sy_reserve"
    sanity_lo = 0.50
    sanity_hi = 1.50
    LST_MINT  = "H7spKUoJP5zdGNh18krqtonfQ8zNibZVQaN4NYEVbcrK"   # kUSDG
    BASE_MINT = "2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH"   # USDG
    RESERVE   = "34Bb1oLf9F7H4CAGefC56HFBsuJQ1tSJafmZnYkFCd83"
    OFF_AVAIL       = 224
    OFF_BORROWED_SF = 232
    OFF_ACC_PROTO   = 344
    OFF_ACC_REF     = 360
    OFF_PEND_REF    = 376
    OFF_MINT_TOTAL  = 2592

    async def fetch_rate(self, client, rpc_url) -> RateRow:
        bufs, slot = await _get_multi(client, rpc_url, [self.RESERVE])
        buf = bufs[0]
        if buf is None or len(buf) < self.OFF_MINT_TOTAL + 8:
            raise RuntimeError(f"{self.name}: reserve account missing or too small")
        avail     = _read_u64_le(buf, self.OFF_AVAIL)
        borrow_sf = _read_u128_le(buf, self.OFF_BORROWED_SF)
        proto_sf  = _read_u128_le(buf, self.OFF_ACC_PROTO)
        ref_sf    = _read_u128_le(buf, self.OFF_ACC_REF)
        pend_sf   = _read_u128_le(buf, self.OFF_PEND_REF)
        mint_tot  = _read_u64_le(buf, self.OFF_MINT_TOTAL)
        total_supply = avail + (borrow_sf - proto_sf - ref_sf - pend_sf) / KAMINO_FRACTION
        if total_supply <= 0 or mint_tot == 0:
            exchange = 1.0
        else:
            exchange = total_supply / mint_tot
        return RateRow(
            lst_mint=self.LST_MINT, base_mint=self.BASE_MINT,
            lst_per_base=1.0 / exchange, slot=slot, provider=self.name,
        )


class SanctumSplRkuSOL:
    """rkuSOL → wSOL via Sanctum-style SPL stake pool.

    Stake pool layout:
        total_lamports     = u64 LE @ 258
        pool_token_supply  = u64 LE @ 266
    exchange_rate (SOL-per-rkuSOL) = total_lamports / pool_token_supply
    (both 9 decimals so dimensionless), lst_per_base = 1 / exchange_rate.
    """
    name = "sanctum_spl_stake_pool"
    sanity_lo = 0.80
    sanity_hi = 1.20
    LST_MINT  = "rkubjTrZYioRSeXwDnhwGQzvW3qkcin72JSxUt3WMVp"
    BASE_MINT = "So11111111111111111111111111111111111111112"  # wSOL
    STAKE_POOL = "ERhozr6u9drmAANXGRNP1oh3quSqPKEwioKH5b8v9Kkt"
    OFF_TOTAL_LAMPORTS = 258
    OFF_POOL_SUPPLY    = 266

    async def fetch_rate(self, client, rpc_url) -> RateRow:
        bufs, slot = await _get_multi(client, rpc_url, [self.STAKE_POOL])
        buf = bufs[0]
        if buf is None or len(buf) < self.OFF_POOL_SUPPLY + 8:
            raise RuntimeError(f"{self.name}: stake pool account missing or short")
        total_lamports = _read_u64_le(buf, self.OFF_TOTAL_LAMPORTS)
        pool_supply    = _read_u64_le(buf, self.OFF_POOL_SUPPLY)
        if total_lamports == 0 or pool_supply == 0:
            exchange = 1.0
        else:
            exchange = total_lamports / pool_supply
        return RateRow(
            lst_mint=self.LST_MINT, base_mint=self.BASE_MINT,
            lst_per_base=1.0 / exchange, slot=slot, provider=self.name,
        )


PROVIDERS: list[Provider] = [
    SolsticeGlamVaultStSLX(),
    KaminoSyKUSDG(),
    SanctumSplRkuSOL(),
]


async def run() -> dict:
    if not RPC_ENDPOINTS:
        raise RuntimeError("No SOLANA_RPC_URLS configured in .env")
    snap = datetime.now(timezone.utc).date()
    rprint(f"[cyan]Pulling LST rates for {snap} from {len(PROVIDERS)} providers…[/cyan]")

    rows: list[RateRow] = []
    failures: list[str] = []
    rpc_url = RPC_ENDPOINTS[0]
    async with httpx.AsyncClient(timeout=30.0) as client:
        for p in PROVIDERS:
            try:
                row = await p.fetch_rate(client, rpc_url)
                if not (p.sanity_lo <= row.lst_per_base <= p.sanity_hi):
                    rprint(
                        f"  [yellow]warn[/yellow] {p.name}: lst_per_base={row.lst_per_base:.6f} "
                        f"outside sanity range [{p.sanity_lo}, {p.sanity_hi}] — keeping anyway"
                    )
                rprint(
                    f"  [green]✓[/green] {p.name:<28}  "
                    f"{row.lst_mint[:14]}…  rate={row.lst_per_base:.6f}  slot={row.slot}"
                )
                rows.append(row)
            except Exception as e:
                failures.append(p.name)
                rprint(f"  [red]✗[/red] {p.name}: {e}")

    now = datetime.now(timezone.utc)
    with warehouse() as con:
        if rows:
            con.executemany("""
                INSERT INTO raw_lst_rates
                    (snapshot_date, lst_mint, base_mint, lst_per_base, slot, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (snapshot_date, lst_mint) DO UPDATE SET
                    base_mint    = excluded.base_mint,
                    lst_per_base = excluded.lst_per_base,
                    slot         = excluded.slot,
                    fetched_at   = excluded.fetched_at
            """, [(snap, r.lst_mint, r.base_mint, r.lst_per_base, r.slot, now) for r in rows])

    rprint(f"[green]Wrote raw_lst_rates[/green]: {len(rows)} rows  ({len(failures)} failures)")
    return {"rows": len(rows), "failures": failures, "date": str(snap)}


def main() -> None:
    started = datetime.now(timezone.utc)
    rprint(f"[bold]extract_lst_rates[/bold]  started {started.isoformat()}")
    asyncio.run(run())
    rprint(f"[bold]done[/bold]  elapsed {datetime.now(timezone.utc) - started}")


if __name__ == "__main__":
    main()
