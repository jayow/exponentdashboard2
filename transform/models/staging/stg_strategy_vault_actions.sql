-- Typed projection of raw_strategy_vault_actions.
--
-- One row per (signature, ix_index) — a single tx can carry multiple
-- vault ixs. Classifies each ix into user / oracle-crank / governance
-- families so downstream flow/activity models can filter cheanly.
--
-- Common arg fields (deposit token_amount_in, queue lp_amount, fast-exit
-- lp_amount) are lifted out of args_json into typed columns so the
-- flows model doesn't re-parse JSON.
{{ config(materialized='view') }}

with base as (
    select
        signature,
        ix_index,
        block_time,
        slot,
        vault,
        actor,
        ix_name,
        cast(lp_delta   as double) as lp_delta,
        cast(base_delta as double) as base_delta,
        tx_success,
        args_json
    from {{ source('raw', 'raw_strategy_vault_actions') }}
)
select
    signature,
    ix_index,
    block_time,
    slot,
    date_trunc('day', to_timestamp(block_time))          as action_date,
    vault,
    actor,
    ix_name,
    lp_delta,
    base_delta,
    tx_success,
    args_json,

    -- Ix family classification
    ix_name in (
        'deposit_liquidity',
        'queue_withdrawal',
        'cancel_withdrawal',
        'execute_withdrawal',
        'execute_withdrawal_from_reserves',
        'fill_withdrawal',
        'wrapper_execute_withdrawal'
    ) as is_user_action,
    ix_name in ('update_price', 'update_twap_price')      as is_oracle_crank,
    ix_name in (
        'stake_vote',
        'unstake_vote',
        'init_proposal',
        'append_proposal_actions',
        'activate_proposal',
        'finalize_proposal',
        'execute_proposal',
        'cancel_proposal'
    ) as is_governance,

    -- Typed arg extracts (NULL when ix_name doesn't match)
    case ix_name
        when 'deposit_liquidity'
            then cast(json_extract_string(args_json, '$.token_amount_in') as double)
    end as deposit_token_amount_in,

    case ix_name
        when 'queue_withdrawal'
            then cast(json_extract_string(args_json, '$.lp_amount') as double)
    end as queue_lp_amount,

    case ix_name
        when 'execute_withdrawal_from_reserves'
            then cast(json_extract_string(args_json, '$.lp_amount') as double)
    end as fast_withdraw_lp_amount
from base
