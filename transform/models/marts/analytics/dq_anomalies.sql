-- Data-quality anomalies. One row per detected issue so the build CLI
-- can surface them after every refresh.
--
-- Each row: (category, severity, date_or_window, entity, detail).
-- Categories:
--   tvl_jump         — protocol or platform TVL moved >50% day-over-day
--   tvl_negative     — any negative TVL value (should never happen)
--   tvl_orphan       — market has tvl_usd > 0 but PT supply = 0 (the
--                      attribution bug class)
--   price_gap        — mint has supply > 0 but no USD price for ≥ 1 day
--   sum_mismatch     — sum(byPlatform) doesn't match int_sy_tvl_daily total
--                      within tolerance
--   volume_outlier   — daily volume > 50× the trailing-30d median
--
-- Materialize as view so it's always fresh.
{{ config(materialized='view') }}

with proto as (
    select date, sum(tvl_usd) as protocol_tvl
    from {{ ref('int_sy_tvl_daily') }}
    group by date
),
proto_jumps as (
    select
        'tvl_jump' as category,
        case
            when ratio > 5 then 'critical'
            when ratio > 2 then 'high'
            else 'medium'
        end as severity,
        date as anomaly_date,
        cast(null as varchar) as entity,
        'Protocol TVL ' || round(prev_tvl/1e6, 2) || 'M → ' ||
        round(protocol_tvl/1e6, 2) || 'M (' || round((ratio-1)*100, 0) || '%)' as detail
    from (
        select
            date, protocol_tvl,
            lag(protocol_tvl) over (order by date) as prev_tvl,
            protocol_tvl / nullif(lag(protocol_tvl) over (order by date), 0) as ratio
        from proto
    )
    where ratio > 1.5 and prev_tvl > 100000  -- only flag once protocol > $100K to skip noise
),
neg_tvl as (
    select
        'tvl_negative' as category,
        'critical' as severity,
        date as anomaly_date,
        market_key as entity,
        'tvl_usd = ' || tvl_usd as detail
    from {{ ref('tvl_daily') }}
    where tvl_usd < 0
),
orphan_tvl as (
    select
        'tvl_orphan' as category,
        'high' as severity,
        date as anomaly_date,
        market_key as entity,
        'PT_supply=0 but tvl_usd=$' || round(tvl_usd, 0) as detail
    from {{ ref('tvl_daily') }}
    where tvl_usd > 1 and (pt_supply is null or pt_supply = 0)
),
price_gaps as (
    -- A mint had supply > 0 on a date but stg_prices didn't return a row
    -- for it on that date (despite stg_prices' forward/backward fill).
    select
        'price_gap' as category,
        'medium' as severity,
        s.date as anomaly_date,
        s.mint as entity,
        'supply=' || round(s.supply_ui, 2) || ' but no price' as detail
    from {{ ref('int_mint_supplies_daily') }} s
    left join {{ ref('stg_prices') }} p
        on p.mint = s.underlying_mint and p.date = s.date
    where s.leg = 'SY' and s.supply_ui > 1
      and s.underlying_mint is not null
      and p.price_usd is null
),
vol_outliers as (
    select
        'volume_outlier' as category,
        'medium' as severity,
        date as anomaly_date,
        cast(null as varchar) as entity,
        'daily volume $' || round(daily_vol/1e6, 2) || 'M is ' ||
        round(daily_vol / nullif(median_30d, 0), 1) || '× the trailing 30d median' as detail
    from (
        select
            date,
            sum(volume_usd) as daily_vol,
            median(sum(volume_usd)) over (order by date rows between 30 preceding and 1 preceding) as median_30d
        from {{ ref('trading_volume_daily') }}
        group by date
    )
    where daily_vol > 50 * median_30d and median_30d > 1000
),
-- Per-market sum vs protocol total: when sum across markets is way off,
-- the per-market view is incomplete. Catches the "unsplit SY has no
-- per-market identity" class of bug.
sum_mismatch as (
    select
        'sum_mismatch' as category,
        case when ratio < 0.5 then 'high' when ratio < 0.8 then 'medium' else 'low' end as severity,
        date as anomaly_date,
        cast(null as varchar) as entity,
        'sum(byMarket)=$' || round(market_sum/1e6, 2) || 'M only ' ||
        round(ratio*100, 0) || '% of protocol total $' || round(proto/1e6, 2) || 'M' as detail
    from (
        select
            p.date,
            p.protocol_tvl as proto,
            coalesce(m.market_sum, 0) as market_sum,
            coalesce(m.market_sum, 0) / nullif(p.protocol_tvl, 0) as ratio
        from proto p
        left join (
            select date, sum(tvl_usd) as market_sum
            from {{ ref('tvl_daily') }} group by date
        ) m using (date)
        where p.protocol_tvl > 100000
    )
    where ratio < 0.9
)
select * from proto_jumps
union all select * from neg_tvl
union all select * from orphan_tvl
union all select * from price_gaps
union all select * from vol_outliers
union all select * from sum_mismatch
