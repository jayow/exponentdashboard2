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
        -- last_seen_index: YT-only, decoded from interest.lastSeenIndex
        --   (256-bit at 80..112, stored as DOUBLE = U256/1e12). Paired with
        --   raw_vault_states.final_sy_exchange_rate to compute unstaged
        --   yield via floor((1/lsi − 1/final) × yt_balance).
        -- clmm_*: LP_CLMM-only Uniswap V3 fee-accounting state. Paired with
        --   raw_clmm_ticks for fee_growth_inside computation. The u128 fields
        --   are stored as UHUGEINT (signed 128-bit) — Q64.64 reads are correct
        --   because we treat them as unsigned in dbt math.
        CREATE TABLE IF NOT EXISTS raw_positions (
            snapshot_date    DATE NOT NULL,
            leg              VARCHAR NOT NULL,
            position_account VARCHAR NOT NULL,
            owner            VARCHAR NOT NULL,
            vault            VARCHAR NOT NULL,
            amount_raw       UBIGINT,
            staged_raw       UBIGINT,
            last_seen_index  DOUBLE,
            clmm_fee_inside_last_pt  UHUGEINT,
            clmm_fee_inside_last_sy  UHUGEINT,
            clmm_tokens_owed_pt      UBIGINT,
            clmm_tokens_owed_sy      UBIGINT,
            clmm_lower_tick_idx      INTEGER,
            clmm_upper_tick_idx      INTEGER,
            clmm_farm_lsi            DOUBLE,
            clmm_farm_staged         UBIGINT,
            clmm_total_lp_share      DOUBLE,
            PRIMARY KEY (snapshot_date, position_account)
        )
    """,
    "raw_vault_states": """
        -- Daily snapshot of Exponent Vault.final_sy_exchange_rate (256-bit
        -- Number at offset 401..433, stored as DOUBLE = U256/1e12). Required
        -- to compute unstaged YT yield via
        --   unstaged_atoms = floor((1/lsi − 1/final) × yt_balance)
        -- where lsi comes from raw_positions.last_seen_index.
        -- Populated by extract_load/extract_vault_states.py.
        CREATE TABLE IF NOT EXISTS raw_vault_states (
            snapshot_date            DATE NOT NULL,
            vault                    VARCHAR NOT NULL,
            mint_pt                  VARCHAR,
            mint_yt                  VARCHAR,
            final_sy_exchange_rate   DOUBLE,
            last_seen_sy_rate        DOUBLE,
            PRIMARY KEY (snapshot_date, vault)
        )
    """,
    "raw_market_three_states": """
        -- Per-CLMM-market state needed for unclaimed-yield computation:
        --   • ticks_account     — PDA holding the tick array (raw_clmm_ticks)
        --   • lp_farm_index     — current global farm index (matches first
        --     lp_farm.farm_emissions[].index; we only track #0 today)
        --   • lp_farm_last_ts   — last update timestamp for projection
        --   • lp_farm_token_rate — emission rate per second for projection
        --   • lp_farm_expiry_ts — emissions stop accruing past this
        --   • liquidity_balance — total LP supply in escrow (for projection)
        CREATE TABLE IF NOT EXISTS raw_market_three_states (
            snapshot_date         DATE NOT NULL,
            clmm_market           VARCHAR NOT NULL,
            mint_pt               VARCHAR,
            mint_sy               VARCHAR,
            mint_yt               VARCHAR,
            vault                 VARCHAR,
            ticks_account         VARCHAR,
            liquidity_balance     UBIGINT,
            lp_farm_last_ts       INTEGER,
            lp_farm_token_rate    UBIGINT,
            lp_farm_expiry_ts     INTEGER,
            lp_farm_index         DOUBLE,
            lp_farm_mint          VARCHAR,
            PRIMARY KEY (snapshot_date, clmm_market)
        )
    """,
    "raw_clmm_ticks": """
        -- Per-(market, tick-slot) Uniswap V3 fee_growth_outside snapshot.
        -- One row per active tick (apy_bp > 0) within each MarketThree's
        -- Ticks PDA. fee_growth_outside_pt/sy are u128 Q64.64 — stored as
        -- UHUGEINT and treated as unsigned in dbt math.
        -- principal_pt + principal_sy + principal_share_supply enable the
        -- canonical CLMM LP USD valuation via SDK withdrawLiquidity formula:
        --   token_out = lp_share × tick.principal_X / tick.principal_share_supply
        CREATE TABLE IF NOT EXISTS raw_clmm_ticks (
            snapshot_date            DATE NOT NULL,
            clmm_market              VARCHAR NOT NULL,
            tick_slot                INTEGER NOT NULL,
            apy_bp                   INTEGER,
            spot_price               DOUBLE,
            liquidity_gross          UBIGINT,
            fee_growth_outside_pt    UHUGEINT,
            fee_growth_outside_sy    UHUGEINT,
            principal_pt             UHUGEINT,
            principal_sy             UHUGEINT,
            principal_share_supply   DOUBLE,
            PRIMARY KEY (snapshot_date, clmm_market, tick_slot)
        )
    """,
    "raw_clmm_lp_share_trackers": """
        -- Per-(position, share-index) entry from CLMM LpPosition.share_trackers
        -- vec. Each PrincipalShare locks a fraction of the position's
        -- liquidity into a specific tick range; the canonical valuation
        -- sums (lp_share × tick.principal_X / tick.principal_share_supply)
        -- across all shares of the position.
        CREATE TABLE IF NOT EXISTS raw_clmm_lp_share_trackers (
            snapshot_date     DATE NOT NULL,
            position_account  VARCHAR NOT NULL,
            share_idx         INTEGER NOT NULL,
            tick_idx          INTEGER,
            right_tick_idx    INTEGER,
            lp_share          DOUBLE,
            PRIMARY KEY (snapshot_date, position_account, share_idx)
        )
    """,
    "raw_clmm_market_globals": """
        -- Per-(market, snapshot) global fee_growth state + current tick.
        -- Decoded from the Ticks PDA footer alongside raw_clmm_ticks.
        CREATE TABLE IF NOT EXISTS raw_clmm_market_globals (
            snapshot_date          DATE NOT NULL,
            clmm_market            VARCHAR NOT NULL,
            fee_growth_global_pt   UHUGEINT,
            fee_growth_global_sy   UHUGEINT,
            current_tick_slot      INTEGER,
            current_spot_price     DOUBLE,
            PRIMARY KEY (snapshot_date, clmm_market)
        )
    """,
    "raw_clmm_claim_events": """
        -- Empirical calibration corpus: captures every ClaimFarmEmissionsEvent
        -- and MarketCollectEmissionEvent emitted by CLMM transactions.
        -- The Anchor event includes amount_claimed (ground truth) alongside
        -- the full position state at claim time (lp_balance, tick range,
        -- farms tracker, share_trackers). Each row is one (state, amount)
        -- pair we can fit a formula against. After ~50-100 samples across
        -- diverse positions, the CLMM farm-reward formula can be derived
        -- statistically and validated before going live in the mart.
        --
        -- event_type:
        --   'ClaimFarmEmissions' — Anchor disc b80a9f9090c2a65c
        --   'MarketCollectEmission' — Anchor disc 7d810f8be8f45243
        CREATE TABLE IF NOT EXISTS raw_clmm_claim_events (
            signature        VARCHAR NOT NULL,
            log_index        INTEGER NOT NULL,
            block_time       BIGINT,
            event_type       VARCHAR NOT NULL,
            owner_address    VARCHAR,
            market_address   VARCHAR,
            lp_position      VARCHAR,
            mint             VARCHAR,
            farm_index       SMALLINT,
            amount_claimed   UBIGINT,
            remaining_staged UBIGINT,
            lp_balance       UBIGINT,
            lower_tick       INTEGER,
            upper_tick       INTEGER,
            tokens_owed_pt   UBIGINT,
            tokens_owed_sy   UBIGINT,
            farm_lsi         DOUBLE,
            farm_staged      UBIGINT,
            total_lp_share   DOUBLE,
            n_shares         INTEGER,
            PRIMARY KEY (signature, log_index)
        )
    """,
    "raw_vault_emissions": """
        -- Per-vault emission tracker indices (Vault.emissions vec). Each row
        -- = one EmissionInfo entry. tracker_index is the position in the vec
        -- (matches YT position's emissions tracker index by parallel order).
        -- last_seen_index = current global emission index used in
        -- calc_share_value(position_lsi, vault_lsi, yt_balance).
        CREATE TABLE IF NOT EXISTS raw_vault_emissions (
            snapshot_date    DATE NOT NULL,
            vault            VARCHAR NOT NULL,
            tracker_index    INTEGER NOT NULL,
            token_account    VARCHAR,
            last_seen_index  DOUBLE,
            PRIMARY KEY (snapshot_date, vault, tracker_index)
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
    "raw_v2_markets": """
        -- v2 (XPBook) market accounts — small accounts (disc 0e8bd6c1).
        -- One row per market, refreshed when extract_v2_markets runs.
        CREATE TABLE IF NOT EXISTS raw_v2_markets (
            market_account     VARCHAR PRIMARY KEY,
            book_account       VARCHAR NOT NULL,
            sy_mint            VARCHAR,
            pt_mint            VARCHAR,
            yt_mint            VARCHAR,
            underlying_ticker  VARCHAR,
            maturity_ts        BIGINT,
            market_key         VARCHAR,
            payload            JSON NOT NULL,
            fetched_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """,
    "raw_v2_book_snapshots": """
        -- One row per (market, day). Header + aggregate counts only — the
        -- per-order details go in raw_v2_orders_snapshots below.
        CREATE TABLE IF NOT EXISTS raw_v2_book_snapshots (
            snapshot_date  DATE        NOT NULL,
            book_account   VARCHAR     NOT NULL,
            fetched_at     TIMESTAMP   DEFAULT CURRENT_TIMESTAMP,
            header_json    JSON        NOT NULL,
            n_offers       INTEGER     NOT NULL,
            n_price_nodes  INTEGER     NOT NULL,
            n_escrows      INTEGER     NOT NULL,
            PRIMARY KEY (snapshot_date, book_account)
        )
    """,
    "raw_v2_orders_snapshots": """
        -- Flattened per-order rows: one row per resting offer per snapshot.
        -- Re-running extract_v2_books on the same date OVERWRITES that date's
        -- rows (delete-then-insert pattern, see extract_v2_books.py).
        CREATE TABLE IF NOT EXISTS raw_v2_orders_snapshots (
            snapshot_date   DATE     NOT NULL,
            book_account    VARCHAR  NOT NULL,
            offer_idx       INTEGER  NOT NULL,
            owner_wallet    VARCHAR  NOT NULL,
            side            VARCHAR(3) NOT NULL,  -- 'bid' | 'ask' | '?'
            apy_rate        DOUBLE   NOT NULL,    -- decimal, e.g. 0.1225
            size_sy_atomic  UHUGEINT  NOT NULL,    -- divide by 1e9 for SY units
            expiry_at       INTEGER  NOT NULL,
            created_at      INTEGER  NOT NULL,
            type_flag       SMALLINT NOT NULL,
            virtual_offer   SMALLINT NOT NULL,
            fok             SMALLINT NOT NULL,
            PRIMARY KEY (snapshot_date, book_account, offer_idx)
        )
    """,
}


# Lightweight ALTER migrations for columns added after initial DDL shipped.
# Each entry is (table, column, type_sql). Skipped if column already exists.
COLUMN_MIGRATIONS: list[tuple[str, str, str]] = [
    ("raw_signatures", "discovered_via",            "VARCHAR"),
    ("raw_positions",  "staged_raw",                "UBIGINT"),
    ("raw_positions",  "last_seen_index",           "DOUBLE"),
    ("raw_positions",  "clmm_fee_inside_last_pt",   "UHUGEINT"),
    ("raw_positions",  "clmm_fee_inside_last_sy",   "UHUGEINT"),
    ("raw_positions",  "clmm_tokens_owed_pt",       "UBIGINT"),
    ("raw_positions",  "clmm_tokens_owed_sy",       "UBIGINT"),
    ("raw_positions",  "clmm_lower_tick_idx",       "INTEGER"),
    ("raw_positions",  "clmm_upper_tick_idx",       "INTEGER"),
    ("raw_positions",  "clmm_farm_lsi",             "DOUBLE"),
    ("raw_positions",  "clmm_farm_staged",          "UBIGINT"),
    ("raw_positions",  "clmm_total_lp_share",       "DOUBLE"),
    ("raw_v2_markets", "sy_decimals",               "SMALLINT"),
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
