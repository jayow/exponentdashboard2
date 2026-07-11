"""Snapshot Exponent Strategy Vault state.

Fetches every ExponentStrategyVault account (Anchor disc 62e427c974d2270b in
EXPONENT_STRATEGY_VAULT_PROGRAM), decodes with strategy_vault_decode, and
writes one row per (vault, snapshot_date) to raw_strategy_vault_states.

Complex nested fields (token_entries, strategy_positions) are stored as JSON
strings for downstream dbt parsing; nav_cb_state stays as raw hex.

Idempotent per snapshot_date: existing rows for today's snapshot_date are
deleted before insert.
"""
from __future__ import annotations
import asyncio
import base64
import json
from datetime import datetime, timezone

import base58
import httpx
from rich import print as rprint
from rich.markup import escape

from .config import RPC_ENDPOINTS, EXPONENT_STRATEGY_VAULT_PROGRAM
from .solana_rpc_client import SolanaRpcClient, TransientHTTPError
from .load import warehouse
from .strategy_vault_decode import (
    STRATEGY_VAULT_DISCRIMINATOR,
    decode_strategy_vault,
)


U64_MAX = 2**64 - 1
INT64_MAX = 2**63 - 1


def _bigint(v: int | None) -> int | None:
    """Map u64 → DuckDB BIGINT (signed 64-bit).

    u64::MAX is used on-chain as an 'unlimited' sentinel (seen in
    max_aum_supply for uncapped vaults). Represent that as NULL. Any other
    value exceeding INT64_MAX is unexpected — clamp to NULL rather than
    crash so a single weird vault doesn't kill the snapshot.
    """
    if v is None:
        return None
    if v == U64_MAX or v > INT64_MAX:
        return None
    return v


async def run() -> dict:
    if not RPC_ENDPOINTS:
        raise RuntimeError("No SOLANA_RPC_URLS configured in .env")
    snapshot_ts = datetime.now(timezone.utc)
    snapshot_date = snapshot_ts.date()
    rprint(f"[cyan]Snapshotting strategy vaults on {snapshot_date}…[/cyan]")

    disc_b58 = base58.b58encode(STRATEGY_VAULT_DISCRIMINATOR).decode()

    async with SolanaRpcClient(RPC_ENDPOINTS) as client:
        try:
            accts = await client.get_program_accounts(
                EXPONENT_STRATEGY_VAULT_PROGRAM,
                filters=[{"memcmp": {"offset": 0, "bytes": disc_b58}}],
            )
        except (TransientHTTPError, httpx.HTTPError) as gpa_err:
            # getProgramAccounts is the one call class Helius blanket-429s
            # from cloud hosts (2026-07-09..), and backup providers plan-gate
            # it — but plain RPC still works. Refresh the KNOWN vaults via
            # getMultipleAccounts instead; only new-vault discovery degrades,
            # and that self-heals on the next successful gPA run (e.g. a
            # residential/local refresh).
            with warehouse() as con:
                known = [
                    r[0]
                    for r in con.execute(
                        "SELECT DISTINCT vault FROM raw_strategy_vault_states"
                    ).fetchall()
                ]
            if not known:
                # Nothing to fall back on — crash loudly, as before.
                raise
            rprint(
                f"  [yellow]gPA failed ({escape(str(gpa_err))}); falling back "
                f"to getMultipleAccounts over {len(known)} known vaults[/yellow]"
            )
            accts = []
            for i in range(0, len(known), 100):  # 100 = getMultipleAccounts limit
                chunk = known[i : i + 100]
                infos = await client.get_multiple_accounts(chunk)
                for pk, info in zip(chunk, infos):
                    if info is None:
                        continue  # account closed since last snapshot
                    data_field = info["data"]
                    data_b64 = data_field[0] if isinstance(data_field, list) else data_field
                    if not base64.b64decode(data_b64).startswith(STRATEGY_VAULT_DISCRIMINATOR):
                        continue  # safety filter gPA's memcmp used to provide
                    accts.append({"pubkey": pk, "account": info})

    rprint(f"  fetched {len(accts)} candidate vault accounts")

    # Slot isn't surfaced by get_program_accounts (returns result list, not
    # the {context, value} wrapper). Leave NULL per DDL — matches the
    # tranche extractor pattern.
    slot = None

    rows: list[tuple] = []
    failures: list[tuple[str, str]] = []

    for a in accts:
        pubkey = a["pubkey"]
        data_field = a["account"]["data"]
        data_b64 = data_field[0] if isinstance(data_field, list) else data_field
        raw = base64.b64decode(data_b64)

        try:
            d = decode_strategy_vault(raw)
        except Exception as e:
            failures.append((pubkey, str(e)))
            rprint(f"  [yellow]decode failed[/yellow] {pubkey}: {e}")
            continue

        fin = d["financials"]
        rc = d["reserves_config"]

        rows.append((
            snapshot_date,
            pubkey,
            slot,
            snapshot_ts,
            d["squads_settings"],
            d["squads_vault"],
            d["underlying_mint"],
            d["mint_lp"],
            d["token_lp_escrow"],
            d["fee_treasury"],
            d["self_address"],
            d["normal_withdrawal_cut_bp"],
            d["signer_bump"],
            d["status_flags"],
            _bigint(fin["lp_balance"]),
            _bigint(fin["aum_in_base"]),
            _bigint(fin["aum_in_base_in_positions"]),
            _bigint(fin["pending_management_fee_lp"]),
            _bigint(fin["last_management_fee_accrued_at"]),
            _bigint(fin["pending_withdrawal_backlog_due_at"]),
            _bigint(fin["instant_withdrawal_window_end"]),
            _bigint(fin["instant_withdrawal_window_cap_aum"]),
            _bigint(fin["instant_withdrawn_aum_in_window"]),
            _bigint(fin["total_lp_staked_in_votes"]),
            _bigint(fin["total_lp_pending_withdrawals"]),
            _bigint(d["max_aum_supply"]),
            d["seed_id_hex"],
            _bigint(rc["management_fee_bps"]),
            rc["fast_withdrawal_cut_bp"],
            _bigint(rc["reserves_share_bp"]),
            _bigint(rc["instant_withdrawal_window_limit_bp"]),
            _bigint(rc["withdrawal_cancel_cooldown_seconds"]),
            _bigint(rc["max_swap_slippage_bp"]),
            _bigint(rc["nav_aum_change_threshold_bps"]),
            json.dumps(d["token_entries"]),
            json.dumps(d["strategy_positions"]),
            d["nav_cb_state_hex"],
        ))

    rprint(f"  decoded {len(rows)} vaults ok, {len(failures)} failed")

    with warehouse() as con:
        con.execute(
            "DELETE FROM raw_strategy_vault_states WHERE snapshot_date = ?",
            [snapshot_date],
        )
        if rows:
            con.executemany(
                """INSERT INTO raw_strategy_vault_states (
                    snapshot_date, vault, slot, snapshot_ts,
                    squads_settings, squads_vault,
                    underlying_mint, mint_lp, token_lp_escrow, fee_treasury, self_address,
                    normal_withdrawal_cut_bp, signer_bump, status_flags,
                    lp_balance, aum_in_base, aum_in_base_in_positions,
                    pending_management_fee_lp, last_management_fee_accrued_at,
                    pending_withdrawal_backlog_due_at, instant_withdrawal_window_end,
                    instant_withdrawal_window_cap_aum, instant_withdrawn_aum_in_window,
                    total_lp_staked_in_votes, total_lp_pending_withdrawals,
                    max_aum_supply, seed_id,
                    management_fee_bps, fast_withdrawal_cut_bp, reserves_share_bp,
                    instant_withdrawal_window_limit_bp, withdrawal_cancel_cooldown_seconds,
                    max_swap_slippage_bp, nav_aum_change_threshold_bps,
                    token_entries_json, strategy_positions_json, nav_cb_state_hex
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )

    rprint(f"[green]Wrote {len(rows)} strategy vault state rows[/green]")
    return {
        "vaults": len(rows),
        "failed": len(failures),
        "snapshot_date": str(snapshot_date),
        "failures": failures,
    }


def main() -> None:
    started = datetime.now(timezone.utc)
    rprint(f"[bold]extract_strategy_vault_states[/bold]  started {started.isoformat()}")
    result = asyncio.run(run())
    rprint(f"[bold]done[/bold]  elapsed {datetime.now(timezone.utc) - started}")
    if result.get("failures"):
        rprint(f"[yellow]{len(result['failures'])} vault(s) failed to decode:[/yellow]")
        for pk, msg in result["failures"]:
            rprint(f"  {pk}  ->  {msg}")


if __name__ == "__main__":
    main()
