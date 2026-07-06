-- Daily aggregate of user-initiated strategy-vault flows.
-- One row per (vault, action_date). Restricted to successful user ixs
-- (is_user_action = true AND tx_success = true).
--
-- Counts / sums:
--   deposits_*      -> ix_name = 'deposit_liquidity' (lp_delta > 0)
--   queue_*         -> ix_name = 'queue_withdrawal'  (from args.lp_amount)
--   execute_*       -> ix_name in execute_withdrawal + execute_withdrawal_from_reserves
--                      (lp_delta < 0, reported as absolute value)
--   fast_withdraw_* -> ix_name = 'execute_withdrawal_from_reserves'
--                      (from args.lp_amount)
--   net_flow_lp     = deposits_lp_delta - abs(execute_lp_delta)
--   unique_actors   = count(distinct actor) across all user ixs
{{ config(materialized='view') }}

with actions as (
    select *
    from {{ ref('stg_strategy_vault_actions') }}
    where is_user_action = true
      and tx_success     = true
)
select
    vault,
    action_date,

    -- Deposits
    count(*) filter (where ix_name = 'deposit_liquidity')                as deposits_count,
    coalesce(sum(
        case when ix_name = 'deposit_liquidity' and lp_delta > 0
             then lp_delta end
    ), 0)                                                                as deposits_lp_delta,

    -- Queued withdrawals (intent, not yet settled)
    count(*) filter (where ix_name = 'queue_withdrawal')                 as queue_count,
    coalesce(sum(
        case when ix_name = 'queue_withdrawal'
             then queue_lp_amount end
    ), 0)                                                                as queue_lp_amount,

    -- Executed withdrawals (normal + fast-exit both burn LP)
    count(*) filter (where ix_name in (
        'execute_withdrawal', 'execute_withdrawal_from_reserves'
    ))                                                                   as execute_count,
    coalesce(sum(
        case when ix_name in (
                'execute_withdrawal', 'execute_withdrawal_from_reserves'
             ) and lp_delta < 0
             then abs(lp_delta) end
    ), 0)                                                                as execute_lp_delta,

    -- Fast-exit specifically
    count(*) filter (where ix_name = 'execute_withdrawal_from_reserves') as fast_withdraw_count,
    coalesce(sum(
        case when ix_name = 'execute_withdrawal_from_reserves'
             then fast_withdraw_lp_amount end
    ), 0)                                                                as fast_withdraw_lp_amount,

    -- Net LP flow (deposits in - withdrawals out)
    coalesce(sum(
        case when ix_name = 'deposit_liquidity' and lp_delta > 0
             then lp_delta end
    ), 0)
    - coalesce(sum(
        case when ix_name in (
                'execute_withdrawal', 'execute_withdrawal_from_reserves'
             ) and lp_delta < 0
             then abs(lp_delta) end
    ), 0)                                                                as net_flow_lp,

    count(distinct actor)                                                as unique_actors
from actions
group by 1, 2
