-- Latest-snapshot leaderboard row per strategy vault.
--
-- Powers the "Strategy Vaults" table view in the dashboard — one row per
-- vault carrying its most recent AUM, NAV, utilization, and fee params.
-- Manager surfaces the Squads multisig vault PDA that controls this
-- strategy vault (kept as raw string; UI decides how to render).
{{ config(materialized='table') }}

select
    d.vault,
    d.underlying_mint,
    d.mint_lp,
    d.squads_vault as manager,

    -- Headline size
    d.aum_in_base,
    d.aum_in_base_in_positions,
    d.total_aum,
    d.lp_balance,
    d.nav_per_share,

    -- Utilization / capacity
    d.pct_full,
    d.deployed_ratio,
    d.idle_ratio,
    d.fast_exit_utilization,
    d.pending_withdraw_ratio,

    -- Fees
    d.management_fee_bps,
    d.normal_withdrawal_cut_bp,
    d.fast_withdrawal_cut_bp,

    d.snapshot_date,
    d.snapshot_ts
from {{ ref('int_strategy_vault_daily') }} d
where d.recency_rank = 1
