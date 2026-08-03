"""Extract every Exponent-related tx signature.

Watch addresses (deduped):
  - Exponent core program
  - Exponent CLMM program
  - Per-market addresses from raw_markets: vault, pool, ptMint, ytMint, syMint
    (catches anything an ALT might hide from the program-level scan).

Modes:
  - First run (or fully_backfilled=False per address): full walk to genesis.
    Crash-safe via idempotent INSERT ... ON CONFLICT DO NOTHING; the
    fully_backfilled flag is set only after a walk completes naturally.
  - Subsequent runs: incremental — pass `until=<newest_known_sig>` so Helius
    stops paginating once it reaches a sig we already have.

A new market discovered by extract_markets automatically gets a full
backfill on the next signatures run (no scan_state row → mode=full).

Usage:
    python -m extract_load.extract_signatures              # incremental
    python -m extract_load.extract_signatures --rescan     # ignore state, full re-walk
    python -m extract_load.extract_signatures --programs-only  # skip per-market addresses
"""
from __future__ import annotations
import argparse
import asyncio
import json
from datetime import datetime, timezone
from typing import Any

import duckdb
from rich import print as rprint
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from .config import RPC_ENDPOINTS, EXPONENT_PROGRAMS
from .solana_rpc_client import SolanaRpcClient
from .load import warehouse


SCOPE = "signatures"
INSERT_CHUNK = 1000


def _state(con: duckdb.DuckDBPyConnection, address: str) -> dict | None:
    row = con.execute(
        "SELECT is_fully_backfilled, newest_sig, oldest_sig FROM scan_state "
        "WHERE scope = ? AND address = ?",
        [SCOPE, address],
    ).fetchone()
    if not row:
        return None
    return {"is_fully_backfilled": row[0], "newest_sig": row[1], "oldest_sig": row[2]}


def _upsert_state(
    con: duckdb.DuckDBPyConnection,
    address: str,
    *,
    is_fully_backfilled: bool | None = None,
    newest_sig: str | None = None,
    oldest_sig: str | None = None,
) -> None:
    cur = _state(con, address) or {}
    payload = {
        "is_fully_backfilled": is_fully_backfilled
        if is_fully_backfilled is not None
        else cur.get("is_fully_backfilled", False),
        "newest_sig": newest_sig if newest_sig is not None else cur.get("newest_sig"),
        "oldest_sig": oldest_sig if oldest_sig is not None else cur.get("oldest_sig"),
    }
    con.execute(
        """
        INSERT INTO scan_state (scope, address, is_fully_backfilled, newest_sig, oldest_sig, last_run_at)
        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT (scope, address) DO UPDATE SET
            is_fully_backfilled = excluded.is_fully_backfilled,
            newest_sig          = excluded.newest_sig,
            oldest_sig          = excluded.oldest_sig,
            last_run_at         = excluded.last_run_at
        """,
        [SCOPE, address, payload["is_fully_backfilled"], payload["newest_sig"], payload["oldest_sig"]],
    )


def _flush(con: duckdb.DuckDBPyConnection, rows: list[tuple]) -> None:
    if not rows:
        return
    con.executemany(
        """
        INSERT INTO raw_signatures (signature, address, discovered_via, block_time, slot, err)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (signature) DO NOTHING
        """,
        rows,
    )


def _category_from_label(label: str) -> str:
    """Map watch-address label to discovered_via category."""
    if label in ("core_program", "clmm_program"):
        return label
    prefix = label.split(":", 1)[0]
    if prefix in ("vault", "pool", "pt", "yt"):
        return prefix
    return "unknown"


async def scan_address(
    client: SolanaRpcClient,
    con: duckdb.DuckDBPyConnection,
    address: str,
    *,
    label: str = "unknown",
    rescan: bool = False,
) -> dict[str, Any]:
    state = _state(con, address) or {}
    fully = state.get("is_fully_backfilled", False)
    newest_known = state.get("newest_sig")
    discovered_via = _category_from_label(label)

    # If we've fully backfilled before and not rescanning, stop at newest_known
    # to do an incremental update.
    until = newest_known if (fully and not rescan) else None
    mode = "incremental" if until else "full"

    rprint(f"  [cyan]{address}[/cyan]  mode={mode}  until={(until or '')[:16]}{'...' if until else ''}")

    pending: list[tuple] = []
    new_count = 0
    newest_this_run: str | None = None
    oldest_this_run: str | None = None

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TextColumn("• {task.fields[count]} sigs"),
        TimeElapsedColumn(),
        transient=True,
    ) as bar:
        task = bar.add_task(f"scanning {address[:12]}…", count=0)
        async for sig in client.iter_all_signatures(address, until=until):
            sig_str = sig["signature"]
            if newest_this_run is None:
                newest_this_run = sig_str  # first sig from head
            oldest_this_run = sig_str  # last sig seen (we walk newest→oldest)

            pending.append((
                sig_str,
                address,
                discovered_via,
                sig.get("blockTime"),
                sig.get("slot"),
                json.dumps(sig.get("err")) if sig.get("err") is not None else None,
            ))
            new_count += 1
            if len(pending) >= INSERT_CHUNK:
                _flush(con, pending)
                pending = []
                bar.update(task, count=new_count)
        _flush(con, pending)
        bar.update(task, count=new_count)

    # Update scan state.
    # Mark fully_backfilled=True only if this run reached the end naturally
    # (i.e. it was a full scan that completed) OR it was incremental from
    # an already-fully-backfilled state.
    new_fully = fully or until is None  # full scan that completed → mark
    _upsert_state(
        con,
        address,
        is_fully_backfilled=new_fully,
        newest_sig=newest_this_run or newest_known,
        oldest_sig=oldest_this_run or state.get("oldest_sig"),
    )
    return {"address": address, "mode": mode, "new": new_count, "fully": new_fully}


def watch_addresses(
    con: duckdb.DuckDBPyConnection, *, include_markets: bool = True
) -> list[tuple[str, str]]:
    """Returns deduped list of (address, label) to scan.

    Always includes the two Exponent programs. If include_markets and
    raw_markets has rows, also includes vault/pool/ptMint/ytMint per market.
    """
    out: list[tuple[str, str]] = [
        (EXPONENT_PROGRAMS[0], "core_program"),
        (EXPONENT_PROGRAMS[1], "clmm_program"),
    ]
    if include_markets:
        # Pull from API rows first (richer), fall back to onchain. Both shapes
        # carry vault + ammPool + clmmOrderbook + ptMint + ytMint where known.
        # Migration markets have both ammPool and clmmOrderbook — scan both.
        try:
            rows = con.execute(
                """
                SELECT market_key,
                       payload->>'$.vault'         as vault,
                       payload->>'$.ammPool'        as amm_pool,
                       payload->>'$.clmmOrderbook'  as clmm_orderbook,
                       payload->>'$.pool'           as pool,
                       payload->>'$.ptMint'         as pt_mint,
                       payload->>'$.ytMint'         as yt_mint,
                       payload->>'$.syMint'         as sy_mint
                FROM raw_markets
                """
            ).fetchall()
        except duckdb.Error:
            rows = []
        for (market_key, vault, amm_pool, clmm_orderbook, pool, pt_mint, yt_mint,
             sy_mint) in rows:
            if vault:
                out.append((vault, f"vault:{market_key}"))
            if amm_pool:
                out.append((amm_pool, f"pool:{market_key}"))
            if clmm_orderbook:
                out.append((clmm_orderbook, f"pool:{market_key}"))
            # Legacy on-chain rows may only have 'pool' (no ammPool/clmmOrderbook split)
            if pool and pool != amm_pool and pool != clmm_orderbook:
                out.append((pool, f"pool:{market_key}"))
            if pt_mint:
                out.append((pt_mint, f"pt:{market_key}"))
            if yt_mint:
                out.append((yt_mint, f"yt:{market_key}"))
            # SY mints were missing here until 2026-08-02, and that was the
            # root cause of the SY-supply drift — not a supply-math bug.
            # Wrapping underlying → SY hits the SY program and the SY mint but
            # need not touch the vault/pool/PT/YT addresses above, so those
            # txs never entered raw_signatures: measured 127 of the last 1000
            # USX SY-mint txs missing (12.7%). int_mint_supplies_daily sums
            # tx deltas, so the missing mint/burn legs made SY reconstruct 33%
            # low while PT (watched) stayed accurate to 0.04% — which put
            # principal above SY-TVL and tripped C13b. Watching the SY mint
            # closes the coverage gap at the source; the authoritative
            # overlay in int_mint_supplies_daily stays as a safety net, not
            # as the thing holding the numbers up.
            if sy_mint:
                out.append((sy_mint, f"sy:{market_key}"))
    # Dedup by address (preserve first-seen label)
    seen: set[str] = set()
    deduped: list[tuple[str, str]] = []
    for addr, label in out:
        if addr and addr not in seen:
            seen.add(addr)
            deduped.append((addr, label))
    return deduped


async def run(rescan: bool = False, include_markets: bool = True) -> list[dict]:
    if not RPC_ENDPOINTS:
        raise RuntimeError("No SOLANA_RPC_URLS configured in .env")

    results: list[dict] = []
    with warehouse() as con:
        addresses = watch_addresses(con, include_markets=include_markets)
        rprint(
            f"[bold]Scanning {len(addresses)} watch address(es)[/bold]"
            f" (include_markets={include_markets})"
        )
        async with SolanaRpcClient(RPC_ENDPOINTS) as client:
            for addr, label in addresses:
                rprint(f"[dim]{label}[/dim]")
                r = await scan_address(client, con, addr, label=label, rescan=rescan)
                results.append(r)
                rprint(
                    f"  → [green]{r['new']:>7,}[/green] new sigs"
                    f"  (fully_backfilled={r['fully']})"
                )
    total_new = sum(r["new"] for r in results)
    rprint(f"[bold green]Total new sigs across all addresses: {total_new:,}[/bold green]")
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract Exponent-related signatures.")
    parser.add_argument(
        "--rescan",
        action="store_true",
        help="Ignore scan_state.newest_sig; do a full walk to genesis.",
    )
    parser.add_argument(
        "--programs-only",
        action="store_true",
        help="Skip per-market addresses; scan only the two Exponent programs.",
    )
    args = parser.parse_args()
    started = datetime.now(timezone.utc)
    rprint(f"[bold]extract_signatures[/bold]  started {started.isoformat()}")
    asyncio.run(run(rescan=args.rescan, include_markets=not args.programs_only))
    rprint(f"[bold]done[/bold]  elapsed {datetime.now(timezone.utc) - started}")


if __name__ == "__main__":
    main()
