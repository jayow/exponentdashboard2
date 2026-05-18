-- The single source of truth for "trading volume" on the dashboard.
-- Sum of AMM pool underlying flow, valued in USD at the day's price.
-- Bucketed by user-intent (PT-side vs YT-side) so totals don't double-count.
{{ config(materialized='table') }}

with priced as (
    select
        s.date,
        s.market_key,
        m.platform,
        m.ticker,
        s.side,
        s.notional_underlying * coalesce(p.price_usd, 0) as notional_usd
    from {{ ref('fct_swaps') }} s
    left join {{ ref('dim_markets') }} m using (market_key)
    left join {{ ref('stg_prices') }} p
        on p.price_key = m.underlying_mint and p.date = s.date
)
select
    date,
    market_key,
    platform,
    ticker,
    side,
    sum(notional_usd) as volume_usd,
    count(*)          as trade_count
from priced
group by 1, 2, 3, 4, 5
