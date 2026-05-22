"""Build a slim, refresh-capable copy of the warehouse for cloud / GHA hosting.

The full warehouse.duckdb is ~38 GB. Almost all of that is
`raw_helius_tx.payload` — full JSON tx bodies (~32 KB each × 752k rows).
Once dbt has consumed the payloads into `stg_token_changes` (materialized),
the OLD payloads aren't needed anymore. dbt's incremental window for
stg_token_changes is `block_time >= max(this) - 86400` — only 1 day back.

This script produces `data/warehouse_slim.duckdb` containing:
  - Every raw_* table (full rows)
  - raw_helius_tx WITH payload set to NULL for tx older than PAYLOAD_KEEP_DAYS
    (default 7 days — safe buffer for dbt's 1-day incremental window)
  - scan_state (extract bookkeeping)
  - All materialized staging/intermediate/mart tables
  - VIEWS (stg_prices, unclaimed_yield) materialized as tables

Result: ~2-3 GB slim DB that supports the full `make all` cycle in cloud
(extract incrementally → dbt transform → build_web_data). Fits in GHA's
10 GB cache.

LIMITATIONS:
  - dbt `--full-refresh` will break for stg_token_changes (and any other
    incremental model derived from raw_helius_tx.payload) because old
    payloads are NULL. Cloud should always run incremental, never
    full-refresh. Local can full-refresh because the full warehouse has
    all payloads.
  - If GHA misses a daily run by more than ~6 days, the next run's
    incremental window may exceed what's retained in raw_helius_tx.payload.
    Either raise PAYLOAD_KEEP_DAYS or run more frequently.

Usage:
  python -m extract_load.build_slim_db
"""
from __future__ import annotations
import os
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
import duckdb
from rich import print as rprint

# Import the raw-table DDL so we can recreate raw tables WITH their
# PRIMARY KEY / NOT NULL constraints. Plain CTAS-from-Parquet drops
# constraints, which then breaks INSERT ... ON CONFLICT in extractors.
from .load import RAW_DDL


FULL_DB = Path("data/warehouse.duckdb")
SLIM_DB = Path("data/warehouse_slim.duckdb")

# Slim DB at rest holds ZERO payloads — they're only needed transiently
# during a refresh run (when extract_transactions fetches new tx). Once
# dbt has consumed them into stg_token_changes/stg_anchor_position_events,
# they can be discarded. This makes the slim DB stay tiny between runs.
# CAVEAT: extract_anchor_events.py currently does a FULL re-parse from
# scratch each run — would need to be made incremental for cloud refresh.
KEEP_PAYLOADS = False

# Raw tables: input data that drives the pipeline. All preserved in full.
RAW_TABLES = [
    "raw_signatures",
    "raw_markets",
    "raw_market_two_pools",
    "raw_pool_state",
    "raw_clmm_markets",
    "raw_holders",
    "raw_positions",
    "raw_anchor_position_events",
    "raw_prices",
    "raw_lst_rates",
    "raw_token_metadata",
    "raw_exponent_tokens",
    "raw_tvl_snapshots",
    "scan_state",
    "yt_mint_overrides",  # seed file
]

# Materialized dbt models that need to persist (so dbt incremental can
# pick up where it left off rather than rebuilding from raw_helius_tx).
PERSISTED_MODELS = [
    # main_staging — both are incremental tables now (stg_helius_tx was a
    # view but had to be persisted so its lifted scalar fields survive
    # after raw_helius_tx.payload is stripped).
    ("main_staging",      "stg_helius_tx"),
    ("main_staging",      "stg_token_changes"),
    # main_intermediate
    ("main_intermediate", "int_amm_swaps"),
    ("main_intermediate", "int_classified_events"),
    ("main_intermediate", "int_holders_current"),
    ("main_intermediate", "int_holders_per_market_daily"),
    ("main_intermediate", "int_implied_prices_daily"),
    ("main_intermediate", "int_mint_supplies_daily"),
    ("main_intermediate", "int_pool_reserves_daily"),
    ("main_intermediate", "int_pt_holders_daily"),
    ("main_intermediate", "int_sy_tvl_daily"),
    ("main_intermediate", "int_wallet_holdings_usd"),
    ("main_intermediate", "int_yt_lp_holders_daily"),
    # main_core
    ("main_core",         "dim_markets"),
    ("main_core",         "dim_users"),
    ("main_core",         "fct_claims"),
    ("main_core",         "fct_lp_events"),
    ("main_core",         "fct_swaps"),
    # main_analytics
    ("main_analytics",    "active_positions_daily"),
    ("main_analytics",    "holders_daily"),
    ("main_analytics",    "holders_snapshot"),
    ("main_analytics",    "market_holders_top"),
    ("main_analytics",    "market_share_daily"),
    ("main_analytics",    "retention_weekly"),
    ("main_analytics",    "trading_volume_daily"),
    ("main_analytics",    "tvl_daily"),
    ("main_analytics",    "user_growth_daily"),
    ("main_analytics",    "user_lifetime_stats"),
    ("main_analytics",    "wallet_events"),
    ("main_analytics",    "wallet_summary"),
    ("main_analytics",    "whale_events"),
    # Views — materialized as tables in slim
    ("main_staging",      "stg_prices"),
    ("main_analytics",    "unclaimed_yield"),
]


def _copy_via_parquet(src, slim, sch: str, tbl: str, tmpdir: str, where: str = "") -> int:
    """Stream a table from src→Parquet→slim. WHERE clause optional.

    For raw_* tables (sch == 'main'), creates the table from RAW_DDL FIRST
    so PRIMARY KEY / NOT NULL constraints are preserved (required for
    extractors that use INSERT ... ON CONFLICT). For non-raw tables (dbt
    models), plain CTAS — those don't have explicit PKs.
    """
    pq_path = Path(tmpdir) / f"{sch}_{tbl}.parquet"
    select = f'SELECT * FROM {sch}."{tbl}"'
    if where:
        select += f" WHERE {where}"
    src.execute(f"COPY ({select}) TO '{pq_path}' (FORMAT 'parquet')")
    if sch == "main" and tbl in RAW_DDL:
        # Create with proper DDL (PRIMARY KEY etc), then INSERT.
        slim.execute(RAW_DDL[tbl])
        slim.execute(f"INSERT INTO {sch}.\"{tbl}\" SELECT * FROM read_parquet('{pq_path}')")
    else:
        # dbt-built model — schema captured via CTAS is fine.
        slim.execute(f'CREATE TABLE {sch}."{tbl}" AS SELECT * FROM read_parquet(\'{pq_path}\')')
    n = slim.execute(f'SELECT COUNT(*) FROM {sch}."{tbl}"').fetchone()[0]
    pq_path.unlink()
    return n


def main() -> None:
    if not FULL_DB.exists():
        raise SystemExit(f"Source DB not found: {FULL_DB}")
    if SLIM_DB.exists():
        rprint(f"[yellow]Removing existing {SLIM_DB}[/yellow]")
        SLIM_DB.unlink()
        wal = SLIM_DB.with_suffix(".duckdb.wal")
        if wal.exists():
            wal.unlink()

    full_size_mb = FULL_DB.stat().st_size / (1024 * 1024)
    rprint(f"[cyan]Source: {FULL_DB} ({full_size_mb:,.0f} MB)[/cyan]")
    rprint(f"[cyan]Target: {SLIM_DB}  (payloads stripped to NULL)[/cyan]\n")

    src = duckdb.connect(str(FULL_DB), read_only=True)
    slim = duckdb.connect(str(SLIM_DB))

    # Create schemas
    for sch in {"main", "main_staging", "main_intermediate", "main_core", "main_analytics"}:
        slim.execute(f"CREATE SCHEMA IF NOT EXISTS {sch}")

    total_rows = 0

    # Use data/ as tempdir — /tmp on macOS often runs out of space when
    # exporting raw_helius_tx with payloads (1+ GB).
    Path("data/_slim_tmp").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir="data/_slim_tmp") as tmpdir:
        # ── 1. raw_helius_tx — payload stripped, only metadata kept ──
        # The slim DB at rest has NO payloads. Cloud refresh fetches new
        # tx (with fresh payloads), dbt incremental consumes them, payloads
        # are discarded at end of run. We only need signature/block_time/slot
        # in the slim so extract_transactions can dedupe.
        # Create with full DDL so PRIMARY KEY is preserved (raw_helius_tx
        # has PK(signature) — needed for INSERT ... ON CONFLICT in
        # extract_transactions).
        rprint(f"  [dim]raw_helius_tx: payload column stripped (NULL'd)[/dim]")
        try:
            pq = Path(tmpdir) / "raw_helius_tx.parquet"
            src.execute(f"""
                COPY (
                    SELECT signature, block_time, slot, fetched_at,
                           NULL::JSON AS payload
                    FROM main.raw_helius_tx
                ) TO '{pq}' (FORMAT 'parquet')
            """)
            slim.execute(RAW_DDL["raw_helius_tx"])
            slim.execute(f"INSERT INTO main.raw_helius_tx SELECT * FROM read_parquet('{pq}')")
            n = slim.execute("SELECT COUNT(*) FROM main.raw_helius_tx").fetchone()[0]
            total_rows += n
            pq.unlink()
            rprint(f"  [green]✓[/green] {'main.raw_helius_tx':50s} {n:>10,} rows  (no payloads)")
        except Exception as e:
            rprint(f"  [red]✗[/red] main.raw_helius_tx ERROR: {e}")

        # ── 2. Other raw_* tables — full copy ──
        for tbl in RAW_TABLES:
            try:
                n = _copy_via_parquet(src, slim, "main", tbl, tmpdir)
                total_rows += n
                rprint(f"  [green]✓[/green] {'main.' + tbl:50s} {n:>10,} rows")
            except Exception as e:
                rprint(f"  [red]✗[/red] main.{tbl:45s} ERROR: {e}")

        # ── 3. Materialized models ──
        for sch, tbl in PERSISTED_MODELS:
            try:
                n = _copy_via_parquet(src, slim, sch, tbl, tmpdir)
                total_rows += n
                rprint(f"  [green]✓[/green] {sch + '.' + tbl:50s} {n:>10,} rows")
            except Exception as e:
                rprint(f"  [red]✗[/red] {sch + '.' + tbl:45s} ERROR: {e}")

    slim.execute("CHECKPOINT")
    slim.close()
    src.close()

    slim_size_mb = SLIM_DB.stat().st_size / (1024 * 1024)
    pct = (slim_size_mb / full_size_mb) * 100
    rprint(
        f"\n[green]Done.[/green] {SLIM_DB} "
        f"= {slim_size_mb:,.0f} MB ({pct:.1f}% of full), {total_rows:,} rows total"
    )
    if slim_size_mb < 10000:
        rprint(f"[green]✓ Fits in GHA cache (10 GB limit)[/green]")
    else:
        rprint(f"[yellow]⚠ Larger than GHA cache 10 GB limit[/yellow]")


if __name__ == "__main__":
    main()
