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
        -- Daily USD price candles per (mint, date).
        -- source: 'jupiter' (datapi.jup.ag/v2/charts) | 'pyth' (benchmarks.pyth.network) | 'stable' (1.0 fallback)
        -- OHLC kept so we can sample any time of day; close is the canonical "daily price".
        CREATE TABLE IF NOT EXISTS raw_prices (
            mint        VARCHAR NOT NULL,
            date        DATE NOT NULL,
            close_usd   DOUBLE,
            open_usd    DOUBLE,
            high_usd    DOUBLE,
            low_usd     DOUBLE,
            volume_usd  DOUBLE,
            source      VARCHAR NOT NULL,
            fetched_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (mint, date)
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
    "raw_positions": """
        -- YT / LP Anchor positions. leg ∈ {'YT', 'LP', 'LP_CLMM'}:
        --   YT       — YieldTokenPosition, core program. vault = SY vault.
        --   LP       — LpPosition, core AMM. vault = MarketTwo (amm_pool).
        --   LP_CLMM  — LpPosition, CLMM program. vault = CLMM market account
        --              (different struct, longer; balance at offset 104).
        -- Owner is always the user wallet at offset 8.
        -- staged_raw: only populated for YT positions — the staged-yield
        --   field at offset 112..120 in YieldTokenPosition.interest.staged.
        --   Used to add unclaimed-but-accrued yield to YT USD value.
        CREATE TABLE IF NOT EXISTS raw_positions (
            snapshot_date    DATE NOT NULL,
            leg              VARCHAR NOT NULL,
            position_account VARCHAR NOT NULL,
            owner            VARCHAR NOT NULL,
            vault            VARCHAR NOT NULL,
            amount_raw       UBIGINT,
            staged_raw       UBIGINT,
            PRIMARY KEY (snapshot_date, position_account)
        )
    """,
    "raw_anchor_position_events": """
        -- Anchor YT/LP position-mutating events parsed from raw_helius_tx logs.
        -- One row per (signature, log_index) where instruction ∈ a closed
        -- vocabulary of position-affecting Anchor instructions. leg ∈ {YT, LP},
        -- action_sign ∈ {+1, -1, 0} (mint / burn / init-only).
        -- Populated by extract_load/extract_anchor_events.py (full refresh).
        CREATE TABLE IF NOT EXISTS raw_anchor_position_events (
            signature        VARCHAR NOT NULL,
            block_time       BIGINT  NOT NULL,
            signer           VARCHAR,
            log_index        INTEGER NOT NULL,
            program_id       VARCHAR NOT NULL,
            instruction_name VARCHAR NOT NULL,
            leg              VARCHAR NOT NULL,
            action_sign      INTEGER NOT NULL,
            market_key       VARCHAR,
            PRIMARY KEY (signature, log_index)
        )
    """,
    "raw_clmm_markets": """
        -- CLMM market accounts (disc f2f01a0f94bab9cd) under EXPONENT_CLMM_PROGRAM.
        -- Maps each clmm_market account to its pt_mint, so int_holders_current
        -- can resolve CLMM LP positions back to a market_key via dim_markets.
        CREATE TABLE IF NOT EXISTS raw_clmm_markets (
            snapshot_date DATE NOT NULL,
            clmm_market   VARCHAR NOT NULL,
            pt_mint       VARCHAR NOT NULL,
            PRIMARY KEY (snapshot_date, clmm_market)
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
    "raw_market_two_pools": """
        -- Every distinct MarketTwo account on-chain (no dedup by market_key).
        -- Several markets have multiple MarketTwo pools (v1 + migrated v2);
        -- LP holders attach to a SPECIFIC pool via their LpPosition.vault.
        -- This table is the source-of-truth for per-pool reserves so the
        -- LP wallet-value calc can match the user's actual pool.
        --
        -- mint_pt / mint_sy / vault are the same across sibling pools of a
        -- market, but mint_lp DIFFERS (one LP mint per pool). lp_supply is
        -- the SPL mint's total supply (atom units).
        --
        -- ptBalance / syBalance / lastLnImpliedRate are decoded straight from
        -- MarketTwo.financials offsets (372, 380, 396).
        CREATE TABLE IF NOT EXISTS raw_market_two_pools (
            snapshot_date    DATE NOT NULL,
            pool_account     VARCHAR NOT NULL,
            mint_pt          VARCHAR,
            mint_sy          VARCHAR,
            mint_lp          VARCHAR,
            vault            VARCHAR,
            expiration_ts    BIGINT,
            pt_balance_raw   UBIGINT,
            sy_balance_raw   UBIGINT,
            last_ln_implied_rate DOUBLE,
            raw_size         INTEGER,
            fetched_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (snapshot_date, pool_account)
        )
    """,
    "raw_pool_state": """
        -- One row per Exponent AMM pool (= per SY mint, not per market).
        -- pool_account is the XP1BRLn8 program account that authorities the
        -- SY-reserves token account. Reserve balances themselves are derived
        -- in dbt from stg_token_changes — no need to snapshot them here.
        CREATE TABLE IF NOT EXISTS raw_pool_state (
            snapshot_date DATE    NOT NULL,
            pool_account  VARCHAR NOT NULL,
            sy_mint       VARCHAR NOT NULL,
            PRIMARY KEY (snapshot_date, pool_account)
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
    "raw_token_metadata": """
        CREATE TABLE IF NOT EXISTS raw_token_metadata (
            mint        VARCHAR PRIMARY KEY,
            name        VARCHAR,
            symbol      VARCHAR,
            decimals    INT,
            source      VARCHAR NOT NULL,   -- 'das' (Helius DAS) | 'api' (exponent /tokens)
            fetched_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            payload     JSON
        )
    """,
    "raw_exponent_tokens": """
        -- Exponent's known underlying-token universe (from /tokens endpoint)
        CREATE TABLE IF NOT EXISTS raw_exponent_tokens (
            mint        VARCHAR PRIMARY KEY,
            name        VARCHAR,
            symbol      VARCHAR,
            decimals    INT,
            fetched_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            payload     JSON NOT NULL
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


# Lightweight ALTER migrations for columns added after initial DDL shipped.
# Each entry is (table, column, type_sql). Skipped if column already exists.
COLUMN_MIGRATIONS: list[tuple[str, str, str]] = [
    ("raw_signatures", "discovered_via", "VARCHAR"),
    ("raw_positions",  "staged_raw",     "UBIGINT"),
]

# Tables that had breaking schema changes — drop + recreate (data loss is OK
# because these are extract-rebuildable). Each entry is (table, sentinel_column)
# — if sentinel_column doesn't exist on the table, drop and recreate.
TABLE_REBUILDS: list[tuple[str, str]] = [
    ("raw_prices", "mint"),  # used to have 'price_key', renamed to 'mint' for clarity
]


def _apply_column_migrations(con: duckdb.DuckDBPyConnection) -> None:
    for table, column, type_sql in COLUMN_MIGRATIONS:
        existing = {r[1] for r in con.execute(f"PRAGMA table_info('{table}')").fetchall()}
        if column not in existing:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {type_sql}")
    for table, sentinel in TABLE_REBUILDS:
        existing = {r[1] for r in con.execute(f"PRAGMA table_info('{table}')").fetchall()}
        if existing and sentinel not in existing:
            con.execute(f"DROP TABLE {table}")
            con.execute(RAW_DDL[table])


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
