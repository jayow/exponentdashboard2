-- Top swap events by USD notional for the "Whale Activity" sub-view.
-- Top 500 swaps with notional >= $10k, sorted by USD desc.
{{ config(materialized='table') }}

select
    signature,
    block_time,
    date,
    signer,
    market_key,
    ticker,
    platform,
    action,
    direction,
    notional_usd,
    notional_underlying,
    underlying_price_usd
from {{ ref('int_amm_swaps') }}
where signer is not null
  and notional_usd is not null
  and notional_usd >= 10000
order by notional_usd desc
limit 500
