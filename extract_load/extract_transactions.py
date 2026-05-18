"""Fetch full tx payloads for every sig in raw_signatures, write to raw_helius_tx.

Idempotent + resumable by design:
  - Anti-joins raw_signatures against raw_helius_tx to find missing sigs
  - Batches 100 sigs per JSON-RPC call (1 HTTP, N results, same credit cost)
  - Re-aligns batch results by id (Helius can return out-of-order)
  - Chunked INSERT ... ON CONFLICT DO NOTHING

Re-running the script picks up exactly where it stopped — no bookkeeping needed.

Usage:
    python -m extract_load.extract_transactions              # full backfill
    python -m extract_load.extract_transactions --limit 1000 # only first 1k missing
    python -m extract_load.extract_transactions --order asc  # oldest first (default: newest first)
"""
from __future__ import annotations
import argparse
import asyncio
import json
from datetime import datetime, timezone

import duckdb
import httpx
from rich import print as rprint
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

from .config import RPC_ENDPOINTS, EXTRACT_BATCH_SIZE
from .solana_rpc_client import SolanaRpcClient
from .load import warehouse


def missing_sigs(
    con: duckdb.DuckDBPyConnection,
    *,
    limit: int | None = None,
    order: str = "desc",
) -> list[tuple[str, int | None]]:
    """Return (signature, block_time) tuples for sigs in raw_signatures but not raw_helius_tx.

    Skip failed txs (err is not null) — they have no useful tx body anyway.
    Default order is newest-first so fresh data populates earliest.
    """
    direction = "DESC" if order.lower() == "desc" else "ASC"
    sql = f"""
        SELECT r.signature, r.block_time
        FROM raw_signatures r
        LEFT JOIN raw_helius_tx t USING (signature)
        WHERE t.signature IS NULL
          AND r.err IS NULL
        ORDER BY r.block_time {direction} NULLS LAST
    """
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return con.execute(sql).fetchall()


def _insert_chunk(con: duckdb.DuckDBPyConnection, rows: list[tuple]) -> int:
    """Insert (signature, block_time, slot, payload_json) rows. Returns rows actually inserted."""
    if not rows:
        return 0
    before = con.execute("SELECT COUNT(*) FROM raw_helius_tx").fetchone()[0]
    con.executemany(
        """
        INSERT INTO raw_helius_tx (signature, block_time, slot, payload)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (signature) DO NOTHING
        """,
        rows,
    )
    after = con.execute("SELECT COUNT(*) FROM raw_helius_tx").fetchone()[0]
    return after - before


async def run(*, limit: int | None = None, order: str = "desc") -> dict:
    if not RPC_ENDPOINTS:
        raise RuntimeError("No SOLANA_RPC_URLS configured in .env")

    with warehouse() as con:
        targets = missing_sigs(con, limit=limit, order=order)
        total = len(targets)
        if total == 0:
            rprint("[green]Nothing to do — raw_helius_tx is up to date with raw_signatures[/green]")
            return {"total": 0, "fetched": 0, "missing": 0}

        rprint(
            f"[bold]extract_transactions[/bold]  {total:,} sigs to fetch  "
            f"(batch={EXTRACT_BATCH_SIZE}, endpoints={len(RPC_ENDPOINTS)}, order={order})"
        )

        fetched = 0
        missing = 0
        failed_batches = 0
        async with SolanaRpcClient(RPC_ENDPOINTS) as client:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                MofNCompleteColumn(),
                TextColumn("• {task.fields[rate]:.0f} tx/s"),
                TimeElapsedColumn(),
                TimeRemainingColumn(),
            ) as bar:
                task = bar.add_task("fetching", total=total, rate=0.0)
                started = datetime.now(timezone.utc)

                for i in range(0, total, EXTRACT_BATCH_SIZE):
                    batch = targets[i : i + EXTRACT_BATCH_SIZE]
                    sigs = [s for s, _ in batch]
                    try:
                        txs = await client.get_transactions(sigs)
                    except httpx.HTTPStatusError as e:
                        # Permanent (non-transient) error after retries exhausted:
                        # log + skip this batch, keep going. Re-running will retry
                        # via the same anti-join.
                        body_preview = (e.response.text or "")[:200].replace("\n", " ")
                        rprint(
                            f"[yellow]skip batch[/yellow] sigs[{i}..{i+len(batch)}] "
                            f"status={e.response.status_code} body={body_preview!r}"
                        )
                        failed_batches += 1
                        elapsed = (datetime.now(timezone.utc) - started).total_seconds() or 0.001
                        rate = (i + len(batch)) / elapsed
                        bar.update(task, advance=len(batch), rate=rate)
                        continue

                    rows: list[tuple] = []
                    for (sig, _bt), tx in zip(batch, txs):
                        if tx is None:
                            missing += 1
                            continue
                        rows.append((
                            sig,
                            tx.get("blockTime"),
                            tx.get("slot"),
                            json.dumps(tx),
                        ))
                    _insert_chunk(con, rows)
                    fetched += len(rows)

                    elapsed = (datetime.now(timezone.utc) - started).total_seconds() or 0.001
                    rate = (i + len(batch)) / elapsed
                    bar.update(task, advance=len(batch), rate=rate)

        rprint(
            f"[green]Done[/green]  fetched={fetched:,}  missing_from_rpc={missing:,}  "
            f"failed_batches={failed_batches}  total_attempted={total:,}"
        )
        return {
            "total": total,
            "fetched": fetched,
            "missing": missing,
            "failed_batches": failed_batches,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch tx payloads for sigs in raw_signatures.")
    parser.add_argument("--limit", type=int, default=None, help="Stop after this many sigs.")
    parser.add_argument(
        "--order",
        choices=("desc", "asc"),
        default="desc",
        help="Fetch order by block_time (desc=newest first, default).",
    )
    args = parser.parse_args()
    started = datetime.now(timezone.utc)
    rprint(f"[bold]extract_transactions[/bold]  started {started.isoformat()}")
    asyncio.run(run(limit=args.limit, order=args.order))
    rprint(f"[bold]done[/bold]  elapsed {datetime.now(timezone.utc) - started}")


if __name__ == "__main__":
    main()
