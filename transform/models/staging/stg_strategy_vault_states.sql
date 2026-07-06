-- Typed projection of raw_strategy_vault_states with light derived ratios.
--
-- Staging conventions:
--   - Cast numeric columns cleanly (u64/HUGEINT → BIGINT / DOUBLE where safe).
--   - Keep every raw field so downstream models don't need to re-scan raw.
--   - Derived ratios are dimensionless (native units / native units) so they
--     survive without knowing the vault's decimals; UI can scale if needed.
--
-- On-chain semantics (verified against deposit events: token_amount_in /
-- lp_minted ≈ nav): aum_in_base is only the IDLE base-token balance;
-- aum_in_base_in_positions is the value deployed into strategy positions
-- (net of any debt). Total NAV is the sum of the two.
--
-- Ratio definitions:
--   total_aum                = aum_in_base + aum_in_base_in_positions
--   nav_per_share            = total_aum / lp_balance
--   pct_full                 = total_aum / max_aum_supply
--   deployed_ratio           = aum_in_base_in_positions / total_aum
--   idle_ratio               = aum_in_base / total_aum
--   fast_exit_utilization    = instant_withdrawn_aum_in_window
--                              / instant_withdrawal_window_cap_aum
--   pending_withdraw_ratio   = total_lp_pending_withdrawals / lp_balance
{{ config(materialized='view') }}

with base as (
    select
        snapshot_date,
        vault,
        underlying_mint,
        mint_lp,
        cast(aum_in_base                       as double) as aum_in_base,
        cast(aum_in_base_in_positions          as double) as aum_in_base_in_positions,
        cast(lp_balance                        as double) as lp_balance,
        cast(max_aum_supply                    as double) as max_aum_supply,
        cast(instant_withdrawn_aum_in_window   as double) as instant_withdrawn_aum_in_window,
        cast(instant_withdrawal_window_cap_aum as double) as instant_withdrawal_window_cap_aum,
        cast(total_lp_pending_withdrawals      as double) as total_lp_pending_withdrawals,
        cast(management_fee_bps                as int)    as management_fee_bps,
        cast(normal_withdrawal_cut_bp          as int)    as normal_withdrawal_cut_bp,
        cast(fast_withdrawal_cut_bp            as int)    as fast_withdrawal_cut_bp,
        squads_vault,
        snapshot_ts
    from {{ source('raw', 'raw_strategy_vault_states') }}
),
with_total as (
    select
        *,
        coalesce(aum_in_base, 0) + coalesce(aum_in_base_in_positions, 0)
            as total_aum
    from base
)
select
    snapshot_date,
    vault,
    underlying_mint,
    mint_lp,

    -- Raw AUM / LP fields
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

    -- Derived ratios (dimensionless)
    total_aum / nullif(lp_balance, 0)                            as nav_per_share,
    total_aum / nullif(max_aum_supply, 0)                        as pct_full,
    aum_in_base_in_positions / nullif(total_aum, 0)              as deployed_ratio,
    aum_in_base / nullif(total_aum, 0)                           as idle_ratio,
    instant_withdrawn_aum_in_window
        / nullif(instant_withdrawal_window_cap_aum, 0)           as fast_exit_utilization,
    total_lp_pending_withdrawals / nullif(lp_balance, 0)         as pending_withdraw_ratio,

    snapshot_ts
from with_total
