-- LP add/remove. Separate from fct_swaps — not trading volume.
{{ config(materialized='table') }}

select
    signature,
    block_time,
    to_timestamp(block_time)::date as date,
    market_key,
    signer as wallet,
    action,   -- 'addLiq' | 'removeLiq'
    cast(null as double) as lp_minted,
    cast(null as double) as lp_burned,
    cast(null as double) as notional_underlying
from {{ ref('int_classified_events') }}
where action in ('addLiq', 'removeLiq')
