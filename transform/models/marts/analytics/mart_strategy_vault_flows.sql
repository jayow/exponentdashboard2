-- Per-tx activity feed for strategy vaults — user actions only.
--
-- Restricted to successful user ixs (is_user_action = true AND
-- tx_success = true). Ordered by block_time desc so the dashboard's
-- "recent activity" panel can select LIMIT N without a sort.
{{ config(materialized='table') }}

select
    signature,
    block_time,
    action_date,
    vault,
    actor,
    ix_name,
    lp_delta,
    base_delta,

    -- Typed arg extracts (NULL when ix_name doesn't match)
    deposit_token_amount_in,
    queue_lp_amount,
    fast_withdraw_lp_amount
from {{ ref('stg_strategy_vault_actions') }}
where is_user_action = true
  and tx_success     = true
order by block_time desc
