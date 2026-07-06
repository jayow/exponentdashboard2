"""Snapshot the strategy-vault withdrawal queue (WithdrawalAccount PDAs).

One account per queued withdrawal request: owner, LP requested/remaining,
fill history, timestamps. Decoded via the program IDL (generic decoder in
strategy_vault_proposal_decode). Payout timing lives on the vault state
(pending_withdrawal_backlog_due_at) — the queue only carries amounts.

Queue accounts churn (created on queue, closed on execute), so each run
replaces the whole table with current on-chain state.

Usage:
    python -m extract_load.extract_strategy_vault_withdrawals
"""
from __future__ import annotations
import asyncio
import base64
import json
from datetime import datetime, timezone

import base58
from rich import print as rprint

from .config import RPC_ENDPOINTS, EXPONENT_STRATEGY_VAULT_PROGRAM
from .solana_rpc_client import SolanaRpcClient
from .load import warehouse
from .strategy_vault_proposal_decode import get_decoder


WITHDRAWAL_ACCOUNT_DISCRIMINATOR = bytes.fromhex("63aeee9ab0cc14f6")


def decode_withdrawal(raw: bytes) -> dict:
    if raw[:8] != WITHDRAWAL_ACCOUNT_DISCRIMINATOR:
        raise ValueError("not a WithdrawalAccount")
    d = get_decoder()
    v, _ = d.decode({"defined": {"name": "WithdrawalAccount"}}, raw, 8)
    return v


async def run() -> dict:
    if not RPC_ENDPOINTS:
        raise RuntimeError("No SOLANA_RPC_URLS configured in .env")
    fetch_ts = datetime.now(timezone.utc)

    disc_b58 = base58.b58encode(WITHDRAWAL_ACCOUNT_DISCRIMINATOR).decode()
    async with SolanaRpcClient(RPC_ENDPOINTS) as client:
        accts = await client.get_program_accounts(
            EXPONENT_STRATEGY_VAULT_PROGRAM,
            filters=[{"memcmp": {"offset": 0, "bytes": disc_b58}}],
        )
    rprint(f"[cyan]Fetched {len(accts)} WithdrawalAccount(s)[/cyan]")

    rows = []
    for a in accts:
        data_field = a["account"]["data"]
        data_b64 = data_field[0] if isinstance(data_field, list) else data_field
        raw = base64.b64decode(data_b64)
        try:
            w = decode_withdrawal(raw)
        except Exception as e:
            rprint(f"  [yellow]warn[/yellow] {a['pubkey'][:8]}… decode failed: {e}")
            continue
        rows.append((
            a["pubkey"], w["vault"], w["owner"],
            int(w["lp_amount_requested"]), int(w["lp_amount_remaining"]),
            len(w.get("fills") or []),
            int(w["created_at"]), int(w["updated_at"]),
            json.dumps(w.get("fills") or [], separators=(",", ":")),
            fetch_ts,
        ))

    with warehouse() as con:
        con.execute("DELETE FROM raw_strategy_vault_withdrawals")
        if rows:
            con.executemany(
                "INSERT INTO raw_strategy_vault_withdrawals VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
    rprint(f"Wrote {len(rows)} withdrawal row(s)")
    return {"withdrawals": len(rows)}


def main() -> None:
    started = datetime.now(timezone.utc)
    rprint(f"extract_strategy_vault_withdrawals  started {started.isoformat()}")
    asyncio.run(run())
    rprint(f"done  elapsed {datetime.now(timezone.utc) - started}")


if __name__ == "__main__":
    main()
