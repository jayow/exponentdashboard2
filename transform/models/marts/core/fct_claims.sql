-- Yield + emission claims. Used for organic-vs-incentivized and claim-efficiency.
--
-- Placeholder: emits the count of claims per tx but doesn't yet attribute
-- markets/wallets/amounts. Proper claim modeling is a follow-up phase.
{{ config(materialized='table') }}

select
    signature,
    block_time,
    to_timestamp(block_time)::date as date,
    cast(null as varchar) as market_key,
    cast(null as varchar) as wallet,
    cast(null as double) as amount,
    cast(null as varchar) as token_mint,
    cast(null as boolean) as is_emission
from {{ ref('int_classified_events') }}
where action = 'claimYield'
