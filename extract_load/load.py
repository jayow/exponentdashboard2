"""DuckDB connection + idempotent upsert helpers.

The warehouse is a single .duckdb file. Raw tables hold full JSON payloads;
all parsing happens later in dbt models. This module only knows how to:
  - open/close the warehouse
  - ensure raw table DDL exists
  - upsert rows by primary key (signature, address, etc.)
"""
from __future__ import annotations
import duckdb
from contextlib import contextmanager
from .config import WAREHOUSE_PATH


RAW_DDL = {
    "raw_helius_tx": """
        CREATE TABLE IF NOT EXISTS raw_helius_tx (
            signature   VARCHAR PRIMARY KEY,
            block_time  BIGINT,
            slot        BIGINT,
            fetched_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            payload     JSON NOT NULL
        )
    """,
    "raw_signatures": """
        CREATE TABLE IF NOT EXISTS raw_signatures (
            signature       VARCHAR PRIMARY KEY,
            address         VARCHAR NOT NULL,
            discovered_via  VARCHAR,   -- 'core_program' | 'clmm_program' | 'vault' | 'pool' | 'pt' | 'yt'
            block_time      BIGINT,
            slot            BIGINT,
            err             JSON,
            fetched_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """,
    "raw_markets": """
        CREATE TABLE IF NOT EXISTS raw_markets (
            market_key   VARCHAR NOT NULL,
            source       VARCHAR NOT NULL,   -- 'api' | 'onchain'
            fetched_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            payload      JSON NOT NULL,
            PRIMARY KEY (market_key, source)
        )
    """,
    "raw_prices": """
        CREATE TABLE IF NOT EXISTS raw_prices (
            price_key   VARCHAR NOT NULL,
            date        DATE NOT NULL,
            price_usd   DOUBLE,
            source      VARCHAR,
            fetched_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (price_key, date)
        )
    """,
    "raw_holders": """
        CREATE TABLE IF NOT EXISTS raw_holders (
            snapshot_date DATE NOT NULL,
            mint          VARCHAR NOT NULL,
            owner         VARCHAR NOT NULL,
            amount        DOUBLE,
            PRIMARY KEY (snapshot_date, mint, owner)
        )
    """,
    "raw_tvl_snapshots": """
        CREATE TABLE IF NOT EXISTS raw_tvl_snapshots (
            snapshot_date DATE NOT NULL,
            market_key    VARCHAR NOT NULL,
            slot          BIGINT,
            underlying_balance DOUBLE,    -- raw token amount (UI decimals)
            fetched_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (snapshot_date, market_key)
        )
    """,
    "raw_pool_state": """
        CREATE TABLE IF NOT EXISTS raw_pool_state (
            snapshot_date DATE NOT NULL,
            market_key    VARCHAR NOT NULL,
            slot          BIGINT,
            sy_reserve    DOUBLE,
            pt_reserve    DOUBLE,
            lp_supply     DOUBLE,
            fetched_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (snapshot_date, market_key)
        )
    """,
    "raw_lst_rates": """
        CREATE TABLE IF NOT EXISTS raw_lst_rates (
            snapshot_date DATE NOT NULL,
            lst_mint      VARCHAR NOT NULL,
            base_mint     VARCHAR NOT NULL,
            lst_per_base  DOUBLE,    -- 1 base = lst_per_base LST tokens
            slot          BIGINT,
            fetched_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (snapshot_date, lst_mint)
        )
    """,
    # Internal bookkeeping — not a source.
    "scan_state": """
        CREATE TABLE IF NOT EXISTS scan_state (
            scope                VARCHAR NOT NULL,   -- e.g. 'signatures'
            address              VARCHAR NOT NULL,
            is_fully_backfilled  BOOLEAN DEFAULT FALSE,
            newest_sig           VARCHAR,
            oldest_sig           VARCHAR,
            last_run_at          TIMESTAMP,
            PRIMARY KEY (scope, address)
        )
    """,
}


# Lightweight ALTER migrations for columns that we added after the initial
# DDL shipped. Each entry is (table, column, type_sql). Skipped if the column
# already exists. Removes the need to drop and recreate the warehouse.
COLUMN_MIGRATIONS: list[tuple[str, str, str]] = [
    ("raw_signatures", "discovered_via", "VARCHAR"),
]


def _apply_column_migrations(con: duckdb.DuckDBPyConnection) -> None:
    for table, column, type_sql in COLUMN_MIGRATIONS:
        existing = {r[1] for r in con.execute(f"PRAGMA table_info('{table}')").fetchall()}
        if column not in existing:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {type_sql}")


@contextmanager
def warehouse():
    """Yield a DuckDB connection with raw schema ensured."""
    con = duckdb.connect(str(WAREHOUSE_PATH))
    try:
        for ddl in RAW_DDL.values():
            con.execute(ddl)
        _apply_column_migrations(con)
        yield con
    finally:
        con.close()


def ensure_schema() -> None:
    with warehouse():
        pass
