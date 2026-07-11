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
    "raw_tranche_return_curves": """
        -- Piecewise-linear return-share curve for one tranche vault, decoded
        -- from the ExponentTranchingMarketReturnModel account (8173 bytes,
        -- disc 9cfb62964e7fc44d) pointed to by raw_tranche_states.return_model_storage.
        --
        -- Curve definition: 50 (x, y) breakpoints, both in u256/1e12 [0..1].
        -- Anchor stores 100 × 80-byte slots; slot[i].limb[0] holds x[i] for
        -- i in 0..50, slot[i].limb[9] holds y[i] for i in 50..99 (workflow
        -- finding from byte-level decoder).
        --
        -- We DENORMALIZE: one row per (snapshot_date, vault, breakpoint_idx)
        -- so dbt can interpolate cleanly via window functions.
        CREATE TABLE IF NOT EXISTS raw_tranche_return_curves (
            snapshot_date     DATE NOT NULL,
            tranche_vault     VARCHAR NOT NULL,
            breakpoint_idx    SMALLINT NOT NULL,
            x_value           DOUBLE,
            y_value           DOUBLE,
            PRIMARY KEY (snapshot_date, tranche_vault, breakpoint_idx)
        )
    """,
    "raw_tranche_lp_actions": """
        -- Per-tx deposits / withdrawals against a tranche vault, decoded
        -- from WrapperDeposit / WrapperWithdraw / Deposit / Withdraw txs.
        -- Powers wallet-level position history.
        --
        -- One row per (signature, wallet, leg). LP amount + SY amount are
        -- raw atoms (9 decimals for both sides today). Sign convention:
        -- positive = into-vault (deposit), negative = out-of-vault (withdraw).
        --
        -- Populated by extract_load/extract_tranche_actions.py via a
        -- watermarked getSignaturesForAddress sweep on each tranche vault PDA.
        CREATE TABLE IF NOT EXISTS raw_tranche_lp_actions (
            signature         VARCHAR NOT NULL,
            tranche_vault     VARCHAR NOT NULL,
            block_time        BIGINT NOT NULL,
            slot              BIGINT,
            ix_name           VARCHAR,
            wallet            VARCHAR NOT NULL,
            leg               VARCHAR NOT NULL, -- 'SR' or 'JR'
            lp_mint           VARCHAR,
            lp_amount_raw     HUGEINT,
            sy_amount_raw     HUGEINT,
            fetched_at        TIMESTAMP DEFAULT now(),
            PRIMARY KEY (signature, wallet, leg)
        )
    """,
    "raw_tranche_states": """
        -- Per-snapshot tranche market state, decoded from the
        -- ExponentTranchingMarket account (program XPTrnchoawi…). One row
        -- per (snapshot_date, tranche_vault). USD/ratio fields are scaled
        -- already: NAV is u256/1e21; utilization/coverage/jr_return_share
        -- are u256/1e12 (natural fractions). LP supply + sy amounts are
        -- raw atom u64s — caller divides by 10^lp_decimals / 10^sy_decimals.
        --
        -- Populated by extract_load/extract_tranche_states.py.
        -- Layout decoded against Anchor IDL @exponent-labs/exponent-tranching-idl
        -- (see docs/ONYC_TRANCHES_PLAN.md).
        CREATE TABLE IF NOT EXISTS raw_tranche_states (
            snapshot_date                  DATE NOT NULL,
            tranche_vault                  VARCHAR NOT NULL,
            seed_id                        VARCHAR,
            sy_mint                        VARCHAR,
            sy_program                     VARCHAR,
            token_sy_escrow                VARCHAR,
            mint_lp_senior                 VARCHAR,
            mint_lp_junior                 VARCHAR,
            return_model_storage           VARCHAR,
            address_lookup_table           VARCHAR,
            status_flags                   SMALLINT,
            market_state                   SMALLINT,
            sr_raw_nav_usd                 DOUBLE,
            jr_raw_nav_usd                 DOUBLE,
            sr_effective_nav_usd           DOUBLE,
            jr_effective_nav_usd           DOUBLE,
            sr_impermanent_loss            DOUBLE,
            jr_impermanent_loss            DOUBLE,
            utilization                    DOUBLE,
            current_junior_return_share    DOUBLE,
            tw_junior_return_share_accrued DOUBLE,
            last_sync_ts                   BIGINT,
            last_distribution_ts           BIGINT,
            fixed_term_end_ts              BIGINT,
            total_sr_lp_supply             UBIGINT,
            total_jr_lp_supply             UBIGINT,
            max_sr_lp_supply               UBIGINT,
            max_jr_lp_supply               UBIGINT,
            sr_sy_amount                   UBIGINT,
            jr_sy_amount                   UBIGINT,
            min_coverage_ratio             DOUBLE,
            fixed_term_duration_sec        INTEGER,
            min_deposit_amount             UBIGINT,
            PRIMARY KEY (snapshot_date, tranche_vault)
        )
    """,
    "raw_strategy_vault_states": """
        -- Per-snapshot Exponent Strategy Vault state, decoded from the
        -- StrategyVault account (program sVau1tXv…). One row per
        -- (vault, snapshot_date). Squads-governed managed vault holding a
        -- mix of underlying + strategy positions; LP mint tracks share of
        -- AUM.
        --
        -- Complex nested fields (token_entries, strategy_positions) are
        -- stored as JSON strings for downstream dbt parsing; nav_cb_state
        -- is kept as raw hex.
        CREATE TABLE IF NOT EXISTS raw_strategy_vault_states (
            snapshot_date                        DATE,
            vault                                VARCHAR,
            slot                                 BIGINT,
            snapshot_ts                          TIMESTAMP,
            squads_settings                      VARCHAR,
            squads_vault                         VARCHAR,
            underlying_mint                      VARCHAR,
            mint_lp                              VARCHAR,
            token_lp_escrow                      VARCHAR,
            fee_treasury                         VARCHAR,
            self_address                         VARCHAR,
            normal_withdrawal_cut_bp             INTEGER,
            signer_bump                          INTEGER,
            status_flags                         INTEGER,
            lp_balance                           BIGINT,
            aum_in_base                          BIGINT,
            aum_in_base_in_positions             BIGINT,
            pending_management_fee_lp            BIGINT,
            last_management_fee_accrued_at       BIGINT,
            pending_withdrawal_backlog_due_at    BIGINT,
            instant_withdrawal_window_end        BIGINT,
            instant_withdrawal_window_cap_aum    BIGINT,
            instant_withdrawn_aum_in_window      BIGINT,
            total_lp_staked_in_votes             BIGINT,
            total_lp_pending_withdrawals         BIGINT,
            max_aum_supply                       BIGINT,
            seed_id                              VARCHAR,
            management_fee_bps                   BIGINT,
            fast_withdrawal_cut_bp               INTEGER,
            reserves_share_bp                    BIGINT,
            instant_withdrawal_window_limit_bp   BIGINT,
            withdrawal_cancel_cooldown_seconds   BIGINT,
            max_swap_slippage_bp                 BIGINT,
            nav_aum_change_threshold_bps         BIGINT,
            token_entries_json                   VARCHAR,
            strategy_positions_json              VARCHAR,
            nav_cb_state_hex                     VARCHAR,
            PRIMARY KEY (vault, snapshot_date)
        )
    """,
    "raw_mint_supplies": """
        -- Authoritative on-chain token supply per mint, snapshotted daily.
        -- Replaces the fragile tx-delta reconstruction in
        -- int_mint_supplies_daily (which drifts whenever a mint/burn tx is
        -- missed — USX SY was 31% understated). supply_raw is decoded
        -- directly from the SPL Mint account (offset 36, u64); pt_supply
        -- cross-checked against the core Vault.pt_supply field. One row per
        -- (mint, snapshot_date). leg ∈ {SY, PT, YT}; vault = the core
        -- Exponent Vault this mint belongs to (SY mints dedup to any one).
        CREATE TABLE IF NOT EXISTS raw_mint_supplies (
            snapshot_date  DATE,
            mint           VARCHAR,
            leg            VARCHAR,
            vault          VARCHAR,
            supply_raw     HUGEINT,
            decimals       INTEGER,
            supply_ui      DOUBLE,
            snapshot_ts    TIMESTAMP,
            PRIMARY KEY (mint, snapshot_date)
        )
    """,
    "raw_sy_supply_history": """
        -- Derivable-correct daily SY supply, one-time backfill for the recent
        -- window where the tx-delta reconstruction drifted (June 2026+, from
        -- incomplete SY mint/burn coverage). Derived from COMPLETE
        -- mint-referencing tx coverage: per-tx net mint balance delta
        -- (Σpost − Σpre; transfers cancel within a tx), walked backward from
        -- today's authoritative getTokenSupply. Validated to 0.02% vs on-chain.
        -- The model coalesces raw_mint_supplies (current) > this (recent
        -- window) > reconstruction (correct pre-drift history).
        CREATE TABLE IF NOT EXISTS raw_sy_supply_history (
            mint         VARCHAR,
            date         DATE,
            supply_ui    DOUBLE,
            snapshot_ts  TIMESTAMP,
            PRIMARY KEY (mint, date)
        )
    """,
    "raw_strategy_vault_signatures": """
        -- Watermarked getSignaturesForAddress sweep against the Strategy
        -- Vault program (or per-vault PDA). One row per signature.
        CREATE TABLE IF NOT EXISTS raw_strategy_vault_signatures (
            program_id   VARCHAR,
            signature    VARCHAR PRIMARY KEY,
            slot         BIGINT,
            block_time   BIGINT,
            err          VARCHAR,
            fetched_at   TIMESTAMP
        )
    """,
    "raw_strategy_vault_actions": """
        -- Per-instruction decoded actions against a strategy vault
        -- (deposit/withdraw/manager ops). One row per (signature, ix_index).
        -- lp_delta / base_delta are signed atom counts (positive = into vault).
        CREATE TABLE IF NOT EXISTS raw_strategy_vault_actions (
            signature             VARCHAR,
            ix_index              INTEGER,
            slot                  BIGINT,
            block_time            BIGINT,
            vault                 VARCHAR,
            ix_name               VARCHAR,
            ix_discriminator_hex  VARCHAR,
            actor                 VARCHAR,
            lp_delta              BIGINT,
            base_delta            BIGINT,
            program_return_hex    VARCHAR,
            args_json             VARCHAR,
            tx_success            BOOLEAN,
            PRIMARY KEY (signature, ix_index)
        )
    """,
    "raw_strategy_vault_holders": """
        -- Forward-only per-day holder snapshot for strategy-vault LP mints.
        -- One row per (vault, holder, snapshot_date).
        CREATE TABLE IF NOT EXISTS raw_strategy_vault_holders (
            vault         VARCHAR,
            mint_lp       VARCHAR,
            holder        VARCHAR,
            lp_amount     BIGINT,
            snapshot_date DATE,
            snapshot_ts   TIMESTAMP,
            slot          BIGINT,
            PRIMARY KEY (vault, holder, snapshot_date)
        )
    """,
    "raw_strategy_vault_obligations": """
        -- Kamino K-Lend obligations referenced by strategy-vault
        -- strategy_positions. One row per (vault, obligation, snapshot_date)
        -- with USD-quoted value summary (K-Lend Fraction fields ÷ 2^60).
        -- deposits_json / borrows_json hold per-reserve breakdowns.
        CREATE TABLE IF NOT EXISTS raw_strategy_vault_obligations (
            snapshot_date           DATE,
            vault                   VARCHAR,
            obligation              VARCHAR,
            lending_market          VARCHAR,
            collateral_value        DOUBLE,
            debt_value              DOUBLE,
            allowed_borrow_value    DOUBLE,
            unhealthy_borrow_value  DOUBLE,
            deposits_json           VARCHAR,
            borrows_json            VARCHAR,
            snapshot_ts             TIMESTAMP,
            PRIMARY KEY (vault, obligation, snapshot_date)
        )
    """,
    "raw_strategy_vault_proposals": """
        -- Current on-chain ActionProposal accounts (vault governance).
        -- Full-replace each run (proposals are lifecycle accounts). Actions
        -- are structural summaries; pubkey→symbol resolution happens in
        -- serve. parse_error marks header-only rows (older action layouts).
        CREATE TABLE IF NOT EXISTS raw_strategy_vault_proposals (
            account             VARCHAR,
            vault               VARCHAR,
            proposal_id         BIGINT,
            proposer            VARCHAR,
            status              VARCHAR,
            created_at          BIGINT,
            voting_ends_at      BIGINT,
            timelock_seconds    BIGINT,
            executable_at       BIGINT,
            reject_votes        BIGINT,
            opt_out_votes       BIGINT,
            lp_supply_snapshot  BIGINT,
            n_actions           INTEGER,
            actions_json        VARCHAR,
            parse_error         VARCHAR,
            fetch_ts            TIMESTAMP,
            PRIMARY KEY (vault, proposal_id)
        )
    """,
    "raw_strategy_vault_executions": """
        -- One row per policy-gated execution tx (identified via
        -- validate_interaction_hook): manager's actual DeFi legs. types are
        -- instruction-discriminator verbs; asset/amount is the squads
        -- vault's largest token delta; deltas_json keeps all mint deltas.
        CREATE TABLE IF NOT EXISTS raw_strategy_vault_executions (
            signature    VARCHAR PRIMARY KEY,
            vault        VARCHAR,
            block_time   BIGINT,
            types        VARCHAR,
            program_ids  VARCHAR,
            asset_mint   VARCHAR,
            amount_ui    DOUBLE,
            deltas_json  VARCHAR,
            fetch_ts     TIMESTAMP
        )
    """,
    "raw_strategy_vault_withdrawals": """
        -- Current on-chain withdrawal queue (WithdrawalAccount PDAs), one
        -- row per open request. Full-replace each run (accounts close on
        -- execute). Payout timing comes from vault state's
        -- pending_withdrawal_backlog_due_at, not from these rows.
        CREATE TABLE IF NOT EXISTS raw_strategy_vault_withdrawals (
            account              VARCHAR PRIMARY KEY,
            vault                VARCHAR,
            owner                VARCHAR,
            lp_amount_requested  BIGINT,
            lp_amount_remaining  BIGINT,
            n_fills              INTEGER,
            created_at           BIGINT,
            updated_at           BIGINT,
            fills_json           VARCHAR,
            fetch_ts             TIMESTAMP
        )
    """,
    "raw_strategy_vault_registry": """
        -- Exponent's managed-strategy registry (api.exponent.finance
        -- /strategies/managed): curated names, strategist, deposit/quote
        -- tokens, capacity, fees, and Exponent's own windowed APYs from
        -- their lpPrice history. One row per (address, fetch_date);
        -- payload_json keeps the full API object.
        CREATE TABLE IF NOT EXISTS raw_strategy_vault_registry (
            fetch_date       DATE,
            address          VARCHAR,
            name             VARCHAR,
            strategist       VARCHAR,
            profile          VARCHAR,
            description      VARCHAR,
            deposit_mint     VARCHAR,
            deposit_ticker   VARCHAR,
            quote_ticker     VARCHAR,
            apy_3d           DOUBLE,
            apy_7d           DOUBLE,
            apy_30d          DOUBLE,
            apy_current      DOUBLE,
            pt_weight        DOUBLE,
            lp_price         DOUBLE,
            tvl              DOUBLE,
            exchange_rate    DOUBLE,
            capacity         DOUBLE,
            management_fee   DOUBLE,
            performance_fee  DOUBLE,
            payload_json     VARCHAR,
            fetch_ts         TIMESTAMP,
            PRIMARY KEY (address, fetch_date)
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
    # Pool SY exchange rate (decoded from XP1BRL AMM pool account, u64 LE at
    # byte offset 97..105 / 1e12). Powers underlying APY derivation when
    # multiple snapshots accumulate.
    ("raw_pool_state", "current_sy_exchange_rate",   "DOUBLE"),
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
