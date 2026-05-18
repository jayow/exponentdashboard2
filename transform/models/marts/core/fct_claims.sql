-- Yield + emission claims. Used for organic-vs-incentivized and claim-efficiency.
{{ config(materialized='table') }}

select
    signature,
    block_time,
    to_timestamp(block_time)::date as date,
    market_key,
    signer as wallet,
    cast(null as double) as amount,
    cast(null as varchar) as token_mint,
    cast(null as boolean) as is_emission
from {{ ref('int_classified_events') }}
where action = 'claimYield'
