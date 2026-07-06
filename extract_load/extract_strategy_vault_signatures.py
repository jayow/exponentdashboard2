"""Extract every tx signature under the Exponent Strategy Vault program.

Watermarked getSignaturesForAddress sweep against
EXPONENT_STRATEGY_VAULT_PROGRAM. One row per signature into
raw_strategy_vault_signatures (signature PK, INSERT OR IGNORE).

Modes:
  - Incremental (default): read max-slot signature from
    raw_strategy_vault_signatures WHERE program_id = <strategy vault program>
    and pass it as `until` so getSignaturesForAddress stops paginating once
    it reaches a sig we already have.
  - Full (--full): no `until`; paginate newest→oldest until the RPC
    returns an empty page (or hits genesis).
  - --limit N: cap the number of signatures processed this run (smoke test).

Usage:
    python -m extract_load.extract_strategy_vault_signatures            # incremental
    python -m extract_load.extract_strategy_vault_signatures --full     # full walk to genesis
    python -m extract_load.extract_strategy_vault_signatures --limit 50 # smoke test
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

from .config import RPC_ENDPOINTS, EXPONENT_STRATEGY_VAULT_PROGRAM
from .solana_rpc_client import SolanaRpcClient
from .load import warehouse


INSERT_CHUNK = 1000
PAGE_SIZE = 1000


def _serialize_err(err: Any) -> str | None:
    if err is None:
        return None
    if isinstance(err, (dict, list)):
        return json.dumps(err)
    return str(err)


def _newest_known_sig(con: duckdb.DuckDBPyConnection, program_id: str) -> str | None:
    """Return the signature with the highest slot for this program_id, or None."""
    row = con.execute(
        """
        SELECT signature
        FROM raw_strategy_vault_signatures
        WHERE program_id = ?
        ORDER BY slot DESC NULLS LAST
        LIMIT 1
        """,
        [program_id],
    ).fetchone()
    return row[0] if row else None


def _flush(con: duckdb.DuckDBPyConnection, rows: list[tuple]) -> None:
    if not rows:
        return
    con.executemany(
        """
        INSERT INTO raw_strategy_vault_signatures
            (program_id, signature, slot, block_time, err, fetched_at)
        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT (signature) DO NOTHING
        """,
        rows,
    )


async def run(full: bool = False, limit: int | None = None) -> dict:
    if not RPC_ENDPOINTS:
        raise RuntimeError("No SOLANA_RPC_URLS configured in .env")

    program_id = EXPONENT_STRATEGY_VAULT_PROGRAM
    processed = 0
    inserted_batches = 0
    newest_this_run: str | None = None
    oldest_this_run: str | None = None

    with warehouse() as con:
        until = None if full else _newest_known_sig(con, program_id)
        mode = "full" if full else ("incremental" if until else "full (no watermark yet)")
        rprint(
            f"[bold]extract_strategy_vault_signatures[/bold]  program={program_id}"
            f"  mode={mode}"
            f"  until={(until or '')[:16]}{'...' if until else ''}"
            f"  limit={limit}"
        )

        pending: list[tuple] = []
        stop = False

        async with SolanaRpcClient(RPC_ENDPOINTS) as client:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                TextColumn("• {task.fields[count]} sigs"),
                TimeElapsedColumn(),
                transient=True,
            ) as bar:
                task = bar.add_task(f"scanning {program_id[:12]}…", count=0)

                before: str | None = None
                while not stop:
                    page = await client.get_signatures_for_address(
                        program_id,
                        before=before,
                        until=until,
                        limit=PAGE_SIZE,
                    )
                    if not page:
                        break

                    for sig in page:
                        sig_str = sig["signature"]
                        if newest_this_run is None:
                            newest_this_run = sig_str
                        oldest_this_run = sig_str

                        pending.append((
                            program_id,
                            sig_str,
                            sig.get("slot"),
                            sig.get("blockTime"),
                            _serialize_err(sig.get("err")),
                        ))
                        processed += 1

                        if len(pending) >= INSERT_CHUNK:
                            _flush(con, pending)
                            inserted_batches += 1
                            pending = []
                            bar.update(task, count=processed)

                        if limit is not None and processed >= limit:
                            stop = True
                            break

                    if stop:
                        break
                    if len(page) < PAGE_SIZE:
                        break
                    before = page[-1]["signature"]

                _flush(con, pending)
                bar.update(task, count=processed)

    rprint(
        f"[green]done[/green]  processed={processed:,}"
        f"  newest={((newest_this_run or '')[:16]) + ('...' if newest_this_run else '')}"
        f"  oldest={((oldest_this_run or '')[:16]) + ('...' if oldest_this_run else '')}"
    )
    return {
        "program_id": program_id,
        "mode": mode,
        "processed": processed,
        "newest_sig": newest_this_run,
        "oldest_sig": oldest_this_run,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract Exponent Strategy Vault program signatures.",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Ignore watermark; walk newest→oldest to genesis.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap number of signatures processed this run (smoke test).",
    )
    args = parser.parse_args()

    started = datetime.now(timezone.utc)
    rprint(f"[bold]extract_strategy_vault_signatures[/bold]  started {started.isoformat()}")
    asyncio.run(run(full=args.full, limit=args.limit))
    rprint(f"[bold]done[/bold]  elapsed {datetime.now(timezone.utc) - started}")


if __name__ == "__main__":
    main()
