-- Daily open interest per market in USD.
--
-- Decomposed by leg ∈ {PT, YT, LP}:
--   PT_OI = PT_supply × underlying_USD     (principal claims outstanding)
--   YT_OI = YT_supply × underlying_USD     (yield claims outstanding)
--   LP_OI = LP_supply × underlying_USD     (AMM liquidity position notional)
--
-- LP approximation: 1 LP token ≈ 1 underlying notional. The "right" answer
-- requires on-chain pool reserves (PT-in-pool, SY-in-pool from each market's
-- AMM account) — follow-up via extract_pool_state. The current proxy is
-- conservative for active markets and is the standard Pendle-dashboard
-- approach when pool state isn't indexed.
--
-- Per-leg rows let the dashboard stack PT/YT/LP separately.
{{ config(materialized='table') }}

select
    s.date,
    s.market_key,
    s.ticker,
    s.platform,
    s.leg,
    s.underlying_mint,
    s.supply_ui                              as supply,
    s.supply_ui * p.price_usd                as oi_usd,
    p.price_usd                              as underlying_price_usd,
    p.source                                 as price_source
from {{ ref('int_mint_supplies_daily') }} s
left join {{ ref('stg_prices') }} p
    on p.mint = s.underlying_mint
   and p.date = s.date
where s.leg in ('PT', 'YT', 'LP')
  and s.supply_ui > 0
