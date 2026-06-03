"""Snapshot Exponent Vault account state for unstaged-yield computation.

Each `Vault` account (Anchor disc d308e82b02987577 in EXPONENT_CORE_PROGRAM)
tracks the SY:underlying exchange rate. Two rate fields matter for yield:
  - last_seen_sy_exchange_rate (offset 337..369)  ← current live rate
  - final_sy_exchange_rate     (offset 401..433)  ← THE rate used by
    state/yield_token_position.rs::earn_sy_interest as the comparison
    point against position.interest.last_seen_index. Stops updating
    after vault maturity.

Both are 256-bit Number fixed-point (4×u64 LE / 1e12 = natural). For
unstaged-yield projection we compute
    unstaged_atoms = floor((1/lsi − 1/final) × yt_balance)
per the canonical formula in calc_earned_sy.

We also extract mint_pt + mint_yt so dbt can join Vault rows back to
dim_markets (which holds the market_key).
"""
from __future__ import annotations
import asyncio
import base64
from datetime import datetime, timezone

import base58
from rich import print as rprint

from .config import RPC_ENDPOINTS, EXPONENT_CORE_PROGRAM
from .solana_rpc_client import SolanaRpcClient
from .load import warehouse


VAULT_DISCRIMINATOR = bytes.fromhex("d308e82b02987577")

# Field offsets verified against exponent-core IDL accounts.Vault layout:
#   [0..8)        discriminator
#   [8..40)       sy_program / [40..72) mint_sy / [72..104) mint_yt / [104..136) mint_pt
#   [136..200)    escrow_yt + escrow_sy
#   [200..232)    yield_position / [232..264) address_lookup_table
#   [264..272)    start_ts (u32) + duration (u32)
#   [272..336)    signer_seed (32) + authority (32)
#   [336..337)    signer_bump (u8)
#   [337..369)    last_seen_sy_exchange_rate    (Number, 32B LE)
#   [369..401)    all_time_high_sy_exchange_rate (Number, 32B LE)
#   [401..433)    final_sy_exchange_rate        (Number, 32B LE)
#   [433..441)    total_sy_in_escrow / [441..449) sy_for_pt / [449..457) pt_supply
#   [457..465)    treasury_sy / [465..473) uncollected_sy
#   [473..505)    treasury_sy_token_account (pubkey)
#   [505..507)    interest_bps_fee (u16)
#   [507..515)    min_op_size_strip (u64) / [515..523) min_op_size_merge (u64)
#   [523..524)    status (u8)
#   [524..528)    emissions vec length (u32) — N × EmissionInfo follows
#
# EmissionInfo (170B each):
#   [0..32)    token_account (pubkey)         ← maps to emission token mint via SPL
#   [32..64)   initial_index (Number)
#   [64..96)   last_seen_index (Number)       ← THE current global index per emission
#   [96..128)  final_index (Number)
#   [128..160) treasury_token_account (pubkey)
#   [160..162) fee_bps (u16)
#   [162..170) treasury_emission (u64)
OFFSET_MINT_YT = 72
OFFSET_MINT_PT = 104
OFFSET_LAST_SEEN_SY = 337
OFFSET_FINAL_SY = 401
OFFSET_EMISSIONS_VEC_LEN = 524
EMISSION_INFO_SIZE = 170


def _decode_vault(data_b64: str) -> tuple[str, str, float, float, list[tuple[str, float]]] | None:
    """Decode (mint_pt, mint_yt, last_seen_sy_rate, final_sy_rate, emissions).
    emissions is a list of (token_account, last_seen_index) per tracker, in vec order.
    """
    try:
        data = base64.b64decode(data_b64)
        if len(data) < OFFSET_FINAL_SY + 32:
            return None
        mint_yt = base58.b58encode(data[OFFSET_MINT_YT:OFFSET_MINT_YT + 32]).decode()
        mint_pt = base58.b58encode(data[OFFSET_MINT_PT:OFFSET_MINT_PT + 32]).decode()
        last_seen = int.from_bytes(data[OFFSET_LAST_SEEN_SY:OFFSET_LAST_SEEN_SY + 32], "little") / 1e12
        final     = int.from_bytes(data[OFFSET_FINAL_SY:OFFSET_FINAL_SY + 32], "little") / 1e12
        # Decode emissions vec
        emissions: list[tuple[str, float]] = []
        if len(data) >= OFFSET_EMISSIONS_VEC_LEN + 4:
            n = int.from_bytes(data[OFFSET_EMISSIONS_VEC_LEN:OFFSET_EMISSIONS_VEC_LEN + 4], "little")
            off = OFFSET_EMISSIONS_VEC_LEN + 4
            for _ in range(n):
                if off + EMISSION_INFO_SIZE > len(data):
                    break
                token_account = base58.b58encode(data[off:off + 32]).decode()
                # last_seen_index sits at +64 within the EmissionInfo
                lsi = int.from_bytes(data[off + 64:off + 96], "little") / 1e12
                emissions.append((token_account, lsi))
                off += EMISSION_INFO_SIZE
        return mint_pt, mint_yt, last_seen, final, emissions
    except Exception:
        return None


async def run() -> dict:
    if not RPC_ENDPOINTS:
        raise RuntimeError("No SOLANA_RPC_URLS configured in .env")
    snapshot_date = datetime.now(timezone.utc).date()
    rprint(f"[cyan]Snapshotting Vault accounts on {snapshot_date}…[/cyan]")

    async with SolanaRpcClient(RPC_ENDPOINTS) as client:
        accts = await client.get_program_accounts(
            EXPONENT_CORE_PROGRAM,
            filters=[
                {"memcmp": {"offset": 0, "bytes": base58.b58encode(VAULT_DISCRIMINATOR).decode()}}
            ],
        )

    rows: list[tuple] = []
    emission_rows: list[tuple] = []
    for a in accts:
        data_field = a["account"]["data"]
        data_b64 = data_field[0] if isinstance(data_field, list) else data_field
        decoded = _decode_vault(data_b64)
        if decoded is None:
            continue
        mint_pt, mint_yt, last_seen, final, emissions = decoded
        rows.append((snapshot_date, a["pubkey"], mint_pt, mint_yt, final, last_seen))
        for idx, (token_account, lsi) in enumerate(emissions):
            emission_rows.append((snapshot_date, a["pubkey"], idx, token_account, lsi))

    rprint(f"  decoded {len(rows)} vaults / {len(emission_rows)} emission trackers")

    with warehouse() as con:
        con.execute("DELETE FROM raw_vault_states WHERE snapshot_date = ?", [snapshot_date])
        con.execute("DELETE FROM raw_vault_emissions WHERE snapshot_date = ?", [snapshot_date])
        if rows:
            con.executemany(
                """INSERT INTO raw_vault_states
                   (snapshot_date, vault, mint_pt, mint_yt, final_sy_exchange_rate, last_seen_sy_rate)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                rows,
            )
        if emission_rows:
            con.executemany(
                """INSERT INTO raw_vault_emissions
                   (snapshot_date, vault, tracker_index, token_account, last_seen_index)
                   VALUES (?, ?, ?, ?, ?)""",
                emission_rows,
            )

    rprint(f"[green]Wrote {len(rows)} vault state rows + {len(emission_rows)} emission tracker rows[/green]")
    return {"vaults": len(rows), "emissions": len(emission_rows), "snapshot_date": str(snapshot_date)}


def main() -> None:
    started = datetime.now(timezone.utc)
    rprint(f"[bold]extract_vault_states[/bold]  started {started.isoformat()}")
    asyncio.run(run())
    rprint(f"[bold]done[/bold]  elapsed {datetime.now(timezone.utc) - started}")


if __name__ == "__main__":
    main()
