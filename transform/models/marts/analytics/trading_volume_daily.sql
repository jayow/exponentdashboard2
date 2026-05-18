-- The single source of truth for "trading volume" on the dashboard.
-- One row per (date, market_key, side). Direction (buy/sell) is preserved
-- as count columns so the breakdown is available without re-aggregating.
{{ config(materialized='table') }}

select
    date,
    market_key,
    ticker,
    platform,
    side,
    sum(notional_underlying)                            as volume_underlying,
    count(*)                                            as trade_count,
    count(*) filter (where direction = 'buy')           as buy_count,
    count(*) filter (where direction = 'sell')          as sell_count,
    sum(notional_underlying) filter (where direction = 'buy')  as volume_buys,
    sum(notional_underlying) filter (where direction = 'sell') as volume_sells
from {{ ref('fct_swaps') }}
group by 1, 2, 3, 4, 5
