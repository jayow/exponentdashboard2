-- One row per (vault, snapshot_date) — pass-through of staging state
-- plus a recency_rank so downstream marts can pick the latest snapshot
-- per vault with a simple `where recency_rank = 1`.
{{ config(materialized='view') }}

select
    vault,
    snapshot_date,
    underlying_mint,
    mint_lp,

    -- AUM / LP fields
    aum_in_base,
    aum_in_base_in_positions,
    total_aum,
    lp_balance,
    max_aum_supply,
    instant_withdrawn_aum_in_window,
    instant_withdrawal_window_cap_aum,
    total_lp_pending_withdrawals,

    -- Fees + roles
    management_fee_bps,
    normal_withdrawal_cut_bp,
    fast_withdrawal_cut_bp,
    squads_vault,

    -- Ratios (from staging)
    nav_per_share,
    pct_full,
    deployed_ratio,
    idle_ratio,
    fast_exit_utilization,
    pending_withdraw_ratio,

    snapshot_ts,

    -- Latest snapshot per vault -> recency_rank = 1
    row_number() over (
        partition by vault
        order by snapshot_date desc
    ) as recency_rank
from {{ ref('stg_strategy_vault_states') }}
