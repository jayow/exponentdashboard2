-- LP add/remove. Separate from fct_swaps — not trading volume.
--
-- Placeholder: emits the count of LP actions per tx but doesn't yet attribute
-- markets. Proper LP-event modeling is a follow-up phase.
{{ config(materialized='table') }}

select
    signature,
    block_time,
    to_timestamp(block_time)::date as date,
    cast(null as varchar)          as market_key,
    cast(null as varchar)          as wallet,
    action,                         -- 'addLiq' | 'removeLiq'
    cast(null as double)            as lp_minted,
    cast(null as double)            as lp_burned,
    cast(null as double)            as notional_underlying
from {{ ref('int_classified_events') }}
where action in ('addLiq', 'removeLiq')
