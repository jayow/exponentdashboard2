-- Daily total supply per (market, leg) where leg ∈ {PT, YT, LP, SY}.
--
-- Net SUM(delta_raw) across all token accounts for a mint = net change in
-- TOTAL supply (transfers cancel; only mintTo and burn affect total supply).
-- Cumulative window sum per (mint, date) → supply at that date.
--
-- Matches Exponent's API "totalMarketSize" field exactly for PT supply
-- (sanity-checked manually on USX-01JUN26: $33M API vs 33,042,922 PT supply).
{{ config(materialized='table') }}

with mint_legs as (
    select market_key, pt_mint as mint, 'PT' as leg from {{ ref('stg_markets') }} where pt_mint is not null
    union all
    select market_key, yt_mint, 'YT' from {{ ref('stg_markets') }} where yt_mint is not null
    union all
    select market_key, lp_mint, 'LP' from {{ ref('stg_markets') }} where lp_mint is not null
    union all
    select market_key, sy_mint, 'SY' from {{ ref('stg_markets') }} where sy_mint is not null
),
daily_delta as (
    select
        m.market_key,
        m.leg,
        m.mint,
        to_timestamp(c.block_time)::date as date,
        sum(c.delta_ui) as daily_delta_ui
    from mint_legs m
    join {{ ref('stg_token_changes') }} c on c.mint = m.mint
    group by 1, 2, 3, 4
),
bounds as (
    select market_key, leg, mint, min(date) as first_date
    from daily_delta group by 1, 2, 3
),
date_axis as (
    select b.market_key, b.leg, b.mint, t.date::date as date
    from bounds b,
        unnest(generate_series(b.first_date, current_date, interval 1 day)) as t(date)
),
filled as (
    select
        d.market_key, d.leg, d.mint, d.date,
        coalesce(c.daily_delta_ui, 0) as daily_delta_ui
    from date_axis d
    left join daily_delta c
      on c.market_key = d.market_key and c.leg = d.leg and c.date = d.date
)
select
    f.market_key,
    f.leg,
    f.mint,
    f.date,
    -- Look up ticker/platform/underlying from dim_markets (avoids fill-forward gymnastics)
    m.ticker,
    m.platform,
    m.underlying_mint,
    m.underlying_decimals,
    f.daily_delta_ui,
    sum(f.daily_delta_ui) over (partition by f.market_key, f.leg order by f.date) as supply_ui
from filled f
left join {{ ref('dim_markets') }} m using (market_key)
order by f.market_key, f.leg, f.date
