"""Orchestrator: run the four strategy-vault extractors sequentially.

Order:
    1. signatures (paginated program-signature crawl)
    2. actions    (decode instructions from raw tx JSON)
    3. states     (snapshot on-chain vault accounts)
    4. holders    (snapshot LP-mint holders)

Each stage is a call to the extractor's `run()` coroutine. This module is
"glue only" — it owns no RPC/DB logic beyond post-run row-count queries for
the final summary.
"""
from __future__ import annotations

import argparse
import asyncio

from rich import print as rprint

from .config import EXPONENT_STRATEGY_VAULT_PROGRAM
from .load import warehouse
from . import (
    extract_strategy_vault_actions,
    extract_strategy_vault_holders,
    extract_strategy_vault_signatures,
    extract_strategy_vault_states,
)


def _table_count(con, table: str, where: str | None = None) -> int:
    sql = f"SELECT COUNT(*) FROM {table}"
    if where:
        sql += f" WHERE {where}"
    return con.execute(sql).fetchone()[0]


async def _main(args: argparse.Namespace) -> None:
    rprint("== Signatures ==")
    sig_result = await extract_strategy_vault_signatures.run(
        full=args.full, limit=args.sig_limit
    )
    rprint(sig_result)

    rprint("== Actions ==")
    action_result = await extract_strategy_vault_actions.run(limit=args.action_limit)
    rprint(action_result)

    rprint("== States ==")
    state_result = await extract_strategy_vault_states.run()
    rprint(state_result)

    rprint("== Holders ==")
    holder_result = await extract_strategy_vault_holders.run()
    rprint(holder_result)

    # Summary
    program_id = EXPONENT_STRATEGY_VAULT_PROGRAM
    with warehouse() as con:
        total_sigs = _table_count(
            con,
            "raw_strategy_vault_signatures",
            where=f"program_id = '{program_id}'",
        )
        total_actions = _table_count(con, "raw_strategy_vault_actions")
        total_states = _table_count(con, "raw_strategy_vault_states")
        total_holders = _table_count(con, "raw_strategy_vault_holders")

    rprint("== Summary ==")
    rprint(f"  raw_strategy_vault_signatures : {total_sigs:>12,}")
    rprint(f"  raw_strategy_vault_actions    : {total_actions:>12,}")
    rprint(f"  raw_strategy_vault_states     : {total_states:>12,}")
    rprint(f"  raw_strategy_vault_holders    : {total_holders:>12,}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill/refresh all strategy-vault tables sequentially."
    )
    parser.add_argument(
        "--sig-limit",
        type=int,
        default=None,
        help="Cap on new signatures to fetch this run (None = uncapped).",
    )
    parser.add_argument(
        "--action-limit",
        type=int,
        default=None,
        help="Cap on signatures to decode into actions this run (None = uncapped).",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Full signature crawl (ignore watermark). Default: incremental.",
    )
    args = parser.parse_args()
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
