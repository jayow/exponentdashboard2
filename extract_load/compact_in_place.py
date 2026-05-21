"""Strip raw_helius_tx.payload values from the working warehouse in place.

After a refresh cycle (extract → dbt → serve), downstream models have
consumed every new payload they need. The 23-GB-of-JSON payload column
is dead weight from that point on — re-fetched on the next cycle for
any newly-seen tx. This script NULLs them all out so the warehouse
file stays small enough to fit in GHA's 10 GB cache.

Unlike build_slim_db.py (which copies selected tables to a NEW database),
this operates on the warehouse the cloud loop just refreshed. Faster:
no Parquet round-trip, no copy.

Typical cloud cycle:
  1. Restore data/warehouse.duckdb from GHA cache
  2. make extract (extract_signatures, _transactions, _holders, etc — adds
     new tx with full payloads to raw_helius_tx)
  3. make transform (dbt incremental — stg_token_changes + others consume
     the new payloads)
  4. python -m extract_load.extract_anchor_events (incremental — consumes
     new payloads into raw_anchor_position_events)
  5. make serve (build_web_data → web/public/*.json)
  6. python -m extract_load.compact_in_place ← THIS — strips payloads
  7. Save data/warehouse.duckdb back to GHA cache + git push JSON

Reduces a 39 GB warehouse to ~1 GB in seconds.
"""
from __future__ import annotations
from pathlib import Path
import duckdb
from rich import print as rprint

from .config import WAREHOUSE_PATH


def main() -> None:
    db_path = Path(WAREHOUSE_PATH)
    if not db_path.exists():
        raise SystemExit(f"Warehouse not found: {db_path}")

    before_mb = db_path.stat().st_size / (1024 * 1024)
    rprint(f"[cyan]Compacting {db_path} (before: {before_mb:,.0f} MB)…[/cyan]")

    con = duckdb.connect(str(db_path))

    # Find rows with non-NULL payloads.
    n_before = con.execute(
        "SELECT COUNT(*) FROM raw_helius_tx WHERE payload IS NOT NULL"
    ).fetchone()[0]
    n_total = con.execute("SELECT COUNT(*) FROM raw_helius_tx").fetchone()[0]
    rprint(f"  {n_before:,} of {n_total:,} rows have payload — stripping")

    if n_before == 0:
        rprint("[green]Already compact, nothing to do.[/green]")
        con.close()
        return

    # NULLing in place doesn't reclaim disk space in DuckDB. We need to:
    #   1. CREATE TABLE _new AS SELECT (with payload NULL) FROM raw_helius_tx
    #   2. DROP TABLE raw_helius_tx
    #   3. ALTER TABLE _new RENAME TO raw_helius_tx
    #   4. CHECKPOINT (forces compaction of freed blocks)
    rprint("  rebuilding table without payloads…")
    con.execute("""
        CREATE TABLE raw_helius_tx__compact AS
        SELECT signature, block_time, slot, fetched_at, NULL::JSON AS payload
        FROM raw_helius_tx
    """)
    con.execute("DROP TABLE raw_helius_tx")
    con.execute("ALTER TABLE raw_helius_tx__compact RENAME TO raw_helius_tx")
    # Recreate primary key (would have been on the original).
    try:
        con.execute("ALTER TABLE raw_helius_tx ADD PRIMARY KEY (signature)")
    except Exception:
        pass  # Already there from CTAS preserving constraints, or DuckDB version diff.

    # CHECKPOINT to release freed blocks back to the OS.
    con.execute("CHECKPOINT")
    con.close()

    after_mb = db_path.stat().st_size / (1024 * 1024)
    saved_mb = before_mb - after_mb
    pct = (after_mb / before_mb * 100) if before_mb > 0 else 0
    rprint(
        f"[green]Done.[/green] {db_path} now {after_mb:,.0f} MB "
        f"({pct:.1f}% of original, saved {saved_mb:,.0f} MB)"
    )


if __name__ == "__main__":
    main()
