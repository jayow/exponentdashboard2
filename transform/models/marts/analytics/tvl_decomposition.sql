-- Current-snapshot TVL decomposition per SY mint (i.e. per asset/vault).
--
-- Splits the headline SY-TVL (int_sy_tvl_daily) into four NON-OVERLAPPING
-- buckets that sum exactly back to it:
--
--   sy_total = principal (PT) + farm (YT) + AMM liquidity (SY leg) + idle
--
--   • principal (PT) / farm (YT): the tokenized portion — SY that was split
--     into PT+YT. Taken from the per-market decomposition (tvl_daily), summed
--     up to the SY mint. principal_pt_usd + farm_yt_usd = PT_supply × price.
--   • AMM liquidity (SY leg): SY sitting in the pools as liquidity. From
--     int_pool_reserves_daily, SY leg ONLY — the pool's PT leg is already
--     counted inside principal, so counting it here would double-count.
--   • idle: the remainder — SY that was minted (underlying wrapped in Exponent)
--     but is neither split into PT/YT nor providing pool liquidity. Held in
--     wallets, a treasury, or deployed OUTSIDE Exponent. Computed as a residual
--     (sy_total − PT − YT − AMM), so it also absorbs any small pricing/timing
--     skew in the measured buckets.
--
-- Grain: one row per sy_mint, CURRENT snapshot only — pool reserves
-- (int_pool_reserves_daily) exist for the latest date only, so the AMM/idle
-- split is point-in-time. The headline SY-TVL timeseries stays in tvl_daily.
{{ config(materialized='table') }}

with sy as (
    select *
    from {{ ref('int_sy_tvl_daily') }}
    where date = (select max(date) from {{ ref('int_sy_tvl_daily') }})
),
tokenized as (
    -- PT and YT value per SY mint (summed across the vault's maturity markets).
    -- principal_pt_usd / farm_yt_usd are PT_supply-based (direct, not the
    -- SY-attributed headline), so summing them gives the vault's true tokenized
    -- value.
    select
        m.sy_mint,
        sum(t.principal_pt_usd) as principal_pt_usd,
        sum(t.farm_yt_usd)      as farm_yt_usd
    from {{ ref('tvl_daily') }} t
    join {{ ref('stg_markets') }} m on m.market_key = t.market_key
    where t.date = (select max(date) from {{ ref('tvl_daily') }})
    group by 1
),
amm as (
    -- AMM SY-side liquidity per SY mint (summed across the vault's pools).
    select mint_sy as sy_mint, sum(sy_leg_usd) as amm_liquidity_usd
    from {{ ref('int_pool_reserves_daily') }}
    group by 1
),
names as (
    select sy_mint, any_value(ticker) as ticker, any_value(platform) as platform
    from {{ ref('stg_markets') }}
    where sy_mint is not null
    group by 1
),
base as (
    select
        sy.date,
        sy.sy_mint,
        n.ticker,
        n.platform,
        sy.underlying_mint,
        sy.tvl_usd                           as sy_total_usd,
        coalesce(tk.principal_pt_usd, 0)     as principal_pt_usd,
        coalesce(tk.farm_yt_usd, 0)          as farm_yt_usd,
        coalesce(a.amm_liquidity_usd, 0)     as amm_raw_usd,
        sy.underlying_price_usd,
        sy.price_source
    from sy
    left join tokenized tk on tk.sy_mint = sy.sy_mint
    left join amm        a  on a.sy_mint  = sy.sy_mint
    left join names      n  on n.sy_mint  = sy.sy_mint
)
-- Non-tokenized SY = sy_total − PT − YT. AMM liquidity can't logically exceed
-- it, so cap it there: measured AMM occasionally lands a hair above the
-- residual (pool vs supply snapshot taken moments apart), which would push idle
-- negative. Capping attributes that tiny skew to AMM being marginally
-- overstated and keeps idle ≥ 0. The four buckets still sum to sy_total exactly.
select
    date,
    sy_mint,
    ticker,
    platform,
    underlying_mint,
    sy_total_usd,
    principal_pt_usd,
    farm_yt_usd,
    least(amm_raw_usd,
          greatest(sy_total_usd - principal_pt_usd - farm_yt_usd, 0.0)) as amm_liquidity_usd,
    (sy_total_usd - principal_pt_usd - farm_yt_usd)
      - least(amm_raw_usd,
              greatest(sy_total_usd - principal_pt_usd - farm_yt_usd, 0.0)) as idle_sy_usd,
    underlying_price_usd,
    price_source
from base
