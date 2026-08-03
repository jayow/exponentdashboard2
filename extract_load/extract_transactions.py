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

from .config import RPC_ENDPOINTS, EXTRACT_BATCH_SIZE, EXTRACT_CONCURRENCY
from .solana_rpc_client import SolanaRpcClient, TransientHTTPError
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

                # Fetch WAVE_SIZE batches concurrently, then insert the wave's
                # rows serially. Until 2026-08-03 this loop awaited one batch at
                # a time, which left the whole client idle: SolanaRpcClient holds
                # 2 semaphore slots per endpoint (4 concurrent across a 2-key
                # pool) and a 15 rps token budget, and a sequential caller used
                # 1 slot and ~0.23 rps — 1.5% of the budget. A measured batch is
                # ~2.5s on the wire, so the 45,659-sig SY-coverage backfill took
                # 4h+ when the network work alone was ~30 min. EXTRACT_CONCURRENCY
                # already existed in config for exactly this and was never wired
                # up here.
                #
                # Waves rather than one gather over all batches: results are held
                # in memory until inserted, and 45k txs at ~33 KB each is >1 GB.
                # A wave caps that at WAVE_SIZE × 15 × 33 KB.
                #
                # Inserts stay serial and on the main coroutine — the DuckDB
                # connection is not safe to use concurrently, and asyncio does
                # not protect it (the awaits above are the only yield points).
                WAVE_SIZE = max(1, EXTRACT_CONCURRENCY * 2)

                async def _fetch(idx: int, batch: list) -> tuple:
                    """Return (idx, batch, txs, error) — never raises, so one bad
                    batch cannot cancel its whole wave via gather.

                    Catches TransientHTTPError as well as HTTPStatusError. The
                    sequential loop only caught the latter, which was survivable
                    only because it was slow enough never to exhaust the retry
                    budget; concurrency made 429s routine on free-tier keys and
                    the uncaught TransientHTTPError killed an entire run at the
                    first exhausted batch. Skipping is safe — extract_transactions
                    anti-joins raw_signatures against raw_helius_tx, so anything
                    dropped here is simply refetched next run."""
                    try:
                        return idx, batch, await client.get_transactions([s for s, _ in batch]), None
                    except (httpx.HTTPStatusError, TransientHTTPError) as e:
                        return idx, batch, None, e

                starts = list(range(0, total, EXTRACT_BATCH_SIZE))
                for w in range(0, len(starts), WAVE_SIZE):
                    wave = [(i, targets[i : i + EXTRACT_BATCH_SIZE]) for i in starts[w : w + WAVE_SIZE]]
                    results = await asyncio.gather(*(_fetch(i, b) for i, b in wave))

                    for i, batch, txs, err in results:
                        if err is not None:
                            # Error after retries exhausted: log + skip this batch,
                            # keep going. Re-running retries it via the same
                            # anti-join. TransientHTTPError carries no .response
                            # (unlike HTTPStatusError), so read it defensively —
                            # blindly touching .response turns a skippable batch
                            # into an AttributeError that kills the run.
                            resp = getattr(err, "response", None)
                            if resp is not None:
                                detail = (f"status={resp.status_code} "
                                          f"body={(resp.text or '')[:200].replace(chr(10), ' ')!r}")
                            else:
                                detail = f"{type(err).__name__}: {str(err)[:200]}"
                            rprint(
                                f"[yellow]skip batch[/yellow] sigs[{i}..{i+len(batch)}] {detail}"
                            )
                            failed_batches += 1
                            elapsed = (datetime.now(timezone.utc) - started).total_seconds() or 0.001
                            bar.update(task, advance=len(batch), rate=fetched / elapsed)
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
                        bar.update(task, advance=len(batch), rate=fetched / elapsed)

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
