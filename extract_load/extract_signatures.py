"""Extract every Exponent-related tx signature.

Scans `getSignaturesForAddress` on both the Exponent core and CLMM programs.
Anything that ever touched Exponent was a top-level invoke of one of these
(or an inner ix from another program — but the outer tx still appears here).

Modes:
  - First run (or fully_backfilled=False): full walk to genesis. Crash-safe
    via idempotent INSERT ... ON CONFLICT DO NOTHING. Sets the
    fully_backfilled flag only after the walk reaches the end naturally.
  - Subsequent runs: incremental — pass `until=<newest_known_sig>` so Helius
    stops paginating once it reaches a sig we already have.

Usage:
    python -m extract_load.extract_signatures              # incremental
    python -m extract_load.extract_signatures --rescan     # ignore state, full re-walk
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

from .config import HELIUS_KEYS, EXPONENT_PROGRAMS
from .helius_client import HeliusClient
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
        INSERT INTO raw_signatures (signature, address, block_time, slot, err)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (signature) DO NOTHING
        """,
        rows,
    )


async def scan_address(
    client: HeliusClient,
    con: duckdb.DuckDBPyConnection,
    address: str,
    *,
    rescan: bool = False,
) -> dict[str, Any]:
    state = _state(con, address) or {}
    fully = state.get("is_fully_backfilled", False)
    newest_known = state.get("newest_sig")

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


async def run(rescan: bool = False) -> list[dict]:
    if not HELIUS_KEYS:
        raise RuntimeError("No HELIUS_KEY_* configured in .env")

    results: list[dict] = []
    with warehouse() as con:
        async with HeliusClient(HELIUS_KEYS) as client:
            for addr in EXPONENT_PROGRAMS:
                r = await scan_address(client, con, addr, rescan=rescan)
                results.append(r)
                rprint(
                    f"  → [green]{r['new']:>7,}[/green] new sigs"
                    f"  (fully_backfilled={r['fully']})"
                )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract Exponent-related signatures.")
    parser.add_argument(
        "--rescan",
        action="store_true",
        help="Ignore scan_state.newest_sig; do a full walk to genesis.",
    )
    args = parser.parse_args()
    started = datetime.now(timezone.utc)
    rprint(f"[bold]extract_signatures[/bold]  started {started.isoformat()}")
    asyncio.run(run(rescan=args.rescan))
    rprint(f"[bold]done[/bold]  elapsed {datetime.now(timezone.utc) - started}")


if __name__ == "__main__":
    main()
