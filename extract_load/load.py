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
            signature   VARCHAR PRIMARY KEY,
            address     VARCHAR NOT NULL,
            block_time  BIGINT,
            slot        BIGINT,
            err         JSON,
            fetched_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """,
    "raw_markets": """
        CREATE TABLE IF NOT EXISTS raw_markets (
            market_key   VARCHAR PRIMARY KEY,
            source       VARCHAR NOT NULL,   -- 'api' | 'onchain'
            fetched_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            payload      JSON NOT NULL
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
}


@contextmanager
def warehouse():
    """Yield a DuckDB connection with raw schema ensured."""
    con = duckdb.connect(str(WAREHOUSE_PATH))
    try:
        for ddl in RAW_DDL.values():
            con.execute(ddl)
        yield con
    finally:
        con.close()


def ensure_schema() -> None:
    with warehouse():
        pass
