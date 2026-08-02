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
),
recon as (
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
        sum(f.daily_delta_ui) over (partition by f.market_key, f.leg order by f.date) as recon_supply_ui
    from filled f
    left join {{ ref('dim_markets') }} m using (market_key)
)
-- Authoritative overlay. The cumulative-delta reconstruction above misses
-- mint/burn events it never saw a tx for, and the drift is severe and
-- one-sided: on 2026-08-02, 34 YT legs reconstructed to 0 against real
-- on-chain supply (one 39.5M YT), and USX SY came out 33% low — which put
-- principal above SY-TVL and tripped C13b (an "impossible" state). Prefer
-- the on-chain SPL mint supply snapshot (extract_mint_supplies) on any date
-- we have one; fall back to reconstruction elsewhere. Same precedence
-- int_sy_tvl_daily already applied for SY — this extends it to PT/YT so the
-- decomposition and the PT+YT attribution weights in tvl_daily stop being
-- computed off drifted supplies. raw_mint_supplies is unique on
-- (mint, leg, snapshot_date), so this join cannot fan out. It carries no LP
-- leg, so LP stays reconstruction-only.
select
    r.market_key,
    r.leg,
    r.mint,
    r.date,
    r.ticker,
    r.platform,
    r.underlying_mint,
    r.underlying_decimals,
    r.daily_delta_ui,
    coalesce(a.supply_ui, r.recon_supply_ui) as supply_ui
from recon r
left join {{ source('raw', 'raw_mint_supplies') }} a
    on a.mint = r.mint and a.leg = r.leg and a.snapshot_date = r.date
order by r.market_key, r.leg, r.date
